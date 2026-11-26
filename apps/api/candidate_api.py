"""Trace -> Compiler -> Candidate -> Human review -> formal TestCaseVersion.

The compiler in ``packages.agents.compiler`` is pure and already refuses to
pre-approve anything. What was missing was a place for candidates to live and a
route that enforces the review gate, so the Candidates page had nothing real to
show (it could only filter the formal case library).

Three routes, each one step of the pipeline:

  * ``POST /explorations/{id}/compile``  trace  -> candidate (PENDING_REVIEW)
  * ``GET  /candidates``                 review queue
  * ``POST /candidates/{id}/approve``    human review -> formal case file
  * ``POST /candidates/{id}/reject``     human review -> closed, never formal

Honesty boundaries:
  * Compilation never writes to the case library. Only an explicit approval
    (reviewer + approval record) writes a formal case file.
  * The compiled Oracle is UI-only and stays unapproved until a human signs off;
    approval is what flips ``approved`` on it, and ``TestCaseVersion.validate``
    would reject the file otherwise.
  * A re-compile of the same trace yields the same candidate id (trace hash),
    so the queue cannot fill with duplicates of one exploration.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from packages.agents.compiler import CandidateTestCase, TestCaseCompiler
from packages.agents.exploration import (
    ExplorationTrace,
    GoalStatus,
    evaluate_goal,
    verify_trace_record,
)
from packages.agents.goal_assertions import evaluate_assertions, evaluate_test_case_verdict
from packages.agents.scenarios import default_registry
from packages.core.errors import ConflictError, NotFoundError, ValidationError
from packages.execution.formal_lock import case_lock
from packages.execution.models import (
    AssertionSpec,
    CleanupPolicy,
    OracleBinding,
    OracleSpec,
    TestDataRef,
    TestIntent,
    TestStep,
)
from packages.execution.review_policy import is_self_approver

router = APIRouter(prefix="/api/v1", tags=["candidates"])

CANDIDATE_ID = re.compile(r"cand_[A-Za-z0-9]+")
CASE_ID = re.compile(r"TC-[A-Z0-9-]+")

PENDING = "PENDING_REVIEW"
APPROVED = "APPROVED"
REJECTED = "REJECTED"


# ----------------------------------------------------------------- storage


class CandidateStore:
    """Append-only JSON store, one file per candidate (status updated in place)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.corrupt_files: list[str] = []

    def _dir(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def save(self, record: dict[str, Any]) -> None:
        cid = str(record["candidate_id"])
        if not CANDIDATE_ID.fullmatch(cid):
            raise ValidationError(f"invalid candidate id: {cid}")
        path = self._dir() / f"{cid}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        if not CANDIDATE_ID.fullmatch(candidate_id):
            return None
        path = self.root / f"{candidate_id}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in self.root.glob("cand_*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                if str(path) not in self.corrupt_files:
                    self.corrupt_files.append(str(path))
                continue
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return out


# ------------------------------------------------------- dict <-> dataclass


def _spec_from_dict(raw: dict[str, Any]) -> OracleSpec:
    return OracleSpec(
        oracle_id=str(raw.get("oracle_id", "")),
        source_type=str(raw.get("source_type", "ui")),
        source_ref=str(raw.get("source_ref", "")),
        expected=raw.get("expected", "PASSED"),
        version=str(raw.get("version", "1.0.0")),
        source=str(raw.get("source", "")),
        approved_by=str(raw.get("approved_by", "")),
        approved=bool(raw.get("approved", False)),
        expected_value=raw.get("expected_value"),
    )


def _checklist_from_record(raw: Any) -> tuple[dict[str, Any], ...]:
    """Normalize checklist entries; legacy stores kept plain strings."""
    items: list[dict[str, Any]] = []
    for i, entry in enumerate(raw or (), start=1):
        if isinstance(entry, dict):
            items.append(
                {
                    "id": str(entry.get("id") or f"chk-{i}"),
                    "text": str(entry.get("text") or ""),
                    "required": bool(entry.get("required", True)),
                }
            )
        elif str(entry).strip():
            items.append({"id": f"chk-{i}", "text": str(entry), "required": True})
    return tuple(items)


def _candidate_from_dict(record: dict[str, Any]) -> CandidateTestCase:
    """Rebuild the dataclass so the compiler's own guards can re-run on approve."""
    intent_raw = record["intent"]
    return CandidateTestCase(
        candidate_id=str(record["candidate_id"]),
        case_id=str(record["case_id"]),
        title=str(record["title"]),
        intent=TestIntent(
            intent_id=str(intent_raw["intent_id"]),
            description=str(intent_raw["description"]),
            source_ref=str(intent_raw.get("source_ref", "")),
            goal=str(intent_raw.get("goal", "")),
            expected_outcomes=tuple(intent_raw.get("expected_outcomes") or ()),
            target=str(intent_raw.get("target", "")),
            required_assertions=tuple(intent_raw.get("required_assertions") or ()),
            constraints=tuple(intent_raw.get("constraints") or ()),
        ),
        steps=tuple(
            TestStep(
                step_id=str(s["step_id"]),
                action=str(s["action"]),
                target=str(s.get("target", "")),
                value=str(s.get("value", "")),
                expected=str(s.get("expected", "")),
                value_ref=str(s.get("value_ref", "")),
            )
            for s in record.get("steps", [])
        ),
        assertions=tuple(
            AssertionSpec(
                assertion_id=str(a["assertion_id"]),
                description=str(a.get("description", "")),
                required=bool(a.get("required", True)),
                source_ref=str(a.get("source_ref", "")),
                notes=tuple(a.get("notes") or ()),
            )
            for a in record.get("assertions", [])
        ),
        oracle_bindings=tuple(
            OracleBinding(
                assertion_id=str(b["assertion_id"]),
                primary=_spec_from_dict(b["primary"]),
                supporting=tuple(_spec_from_dict(s) for s in b.get("supporting") or ()),
                adjudication_rule=str(
                    b.get("adjudication_rule", "frozen_fallback_inconclusive_on_conflict")
                ),
            )
            for b in record.get("oracle_bindings", [])
        ),
        cleanup=CleanupPolicy(
            strategy=str((record.get("cleanup") or {}).get("strategy", "eval_reset")),
            reset_endpoint=str(
                (record.get("cleanup") or {}).get("reset_endpoint", "/__eval__/data/reset")
            ),
        ),
        source_exploration_id=str(record.get("source_exploration_id", "")),
        source_trace_hash=str(record.get("source_trace_hash", "")),
        compiler_version=str(record.get("compiler_version", "1.0.0")),
        prompt_version=str(record.get("prompt_version", "none")),
        model_version=str(record.get("model_version", "deterministic-rules-NOT-LLM")),
        locator_strategy_version=str(record.get("locator_strategy_version", "1.0.0")),
        reviewer=str(record.get("reviewer", "")),
        approval_record=str(record.get("approval_record", "")),
        uncertainty_notes=tuple(record.get("uncertainty_notes") or ()),
        created_at=str(record.get("created_at", "")),
        target=str(record.get("target", "")),
        observations=tuple(record.get("observations") or ()),
        data_refs=tuple(
            TestDataRef(
                ref_id=str(d["ref_id"]),
                locator=str(d.get("locator", "")),
                value=str(d.get("value", "")),
            )
            for d in record.get("data_refs") or ()
        ),
        review_checklist=_checklist_from_record(record.get("review_checklist")),
        goal_status=str(record.get("goal_status", "")),
        goal_checks=tuple(dict(c) for c in record.get("goal_checks") or ()),
        goal_reason=str(record.get("goal_reason", "")),
    )


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    intent = record.get("intent") or {}
    return {
        "candidate_id": record.get("candidate_id"),
        "case_id": record.get("case_id"),
        "title": record.get("title"),
        "status": record.get("status", PENDING),
        "goal": intent.get("goal") or intent.get("description", ""),
        "target": record.get("target", ""),
        "source_exploration_id": record.get("source_exploration_id"),
        "source_trace_hash": record.get("source_trace_hash"),
        "step_count": len(record.get("steps", [])),
        "assertion_count": len(record.get("assertions", [])),
        "assertions": [
            {
                "assertion_id": a.get("assertion_id"),
                "description": a.get("description"),
                "required": a.get("required", True),
            }
            for a in record.get("assertions", [])
        ],
        "oracle_sources": [
            {
                "assertion_id": b.get("assertion_id"),
                "source_type": (b.get("primary") or {}).get("source_type"),
                "source_ref": (b.get("primary") or {}).get("source_ref"),
                "source": (b.get("primary") or {}).get("source"),
                "expected_value": (b.get("primary") or {}).get("expected_value"),
            }
            for b in record.get("oracle_bindings", [])
        ],
        "observation_count": len(record.get("observations") or []),
        "data_refs": list(record.get("data_refs") or []),
        "review_checklist": list(record.get("review_checklist") or []),
        "uncertainty_notes": list(record.get("uncertainty_notes") or []),
        "goal_status": record.get("goal_status", ""),
        "model_version": record.get("model_version"),
        "is_llm": False,
        "created_at": record.get("created_at"),
        "reviewer": record.get("reviewer", ""),
        "approval_record": record.get("approval_record", ""),
        "case_file": record.get("case_file", ""),
    }


# ------------------------------------------------------------------ models


class CompileRequest(BaseModel):
    case_id: str = ""
    title: str = ""


class ReviewRequest(BaseModel):
    reviewer: str
    approval_record: str = ""
    reason: str = ""
    # R5-4.6: server-side review gate. Approving requires the ids of every
    # REQUIRED checklist item the reviewer explicitly confirmed, and an explicit
    # acknowledgement when the compiler recorded uncertainty notes.
    review_confirmed: list[str] = []
    uncertainty_acknowledged: bool = False


# ------------------------------------------------------------------ routes


@router.get("/candidates")
def list_candidates(request: Request) -> list[dict[str, Any]]:
    store: CandidateStore = request.app.state.candidate_store
    return [_summary(r) for r in store.list()]


@router.get("/candidates/{candidate_id}")
def get_candidate(request: Request, candidate_id: str) -> dict[str, Any]:
    store: CandidateStore = request.app.state.candidate_store
    record = store.get(candidate_id)
    if record is None:
        raise NotFoundError(f"candidate not found: {candidate_id}")
    return record


@router.post("/explorations/{exploration_id}/compile", status_code=201)
def compile_exploration(
    request: Request, exploration_id: str, body: CompileRequest
) -> dict[str, Any]:
    record = _exploration_record(request, exploration_id)
    trace = _trace_from_record(record)
    intent = _intent_from_record(record)
    compiler = TestCaseCompiler()
    case_id = body.case_id or f"TC-CAND-{trace.source_trace_hash[-10:].upper()}"
    if not CASE_ID.fullmatch(case_id):
        raise ValidationError(
            "case_id must look like TC-XXXX (upper case letters, digits, dashes)",
            details={"case_id": case_id},
        )
    case_dir = _case_dir(request)
    if (case_dir / f"{case_id}.json").exists():
        raise ConflictError(
            "a formal case with this id already exists",
            details={"case_id": case_id},
        )

    candidate = compiler.compile(
        trace,
        case_id=case_id,
        title=body.title or f"compiled from {exploration_id}",
        intent=intent,
    )
    store: CandidateStore = request.app.state.candidate_store
    existing = store.get(candidate.candidate_id)
    if existing is not None:
        # The candidate id is derived from the trace hash, so recompiling one
        # exploration maps to the same row. Overwriting it would silently erase
        # a review decision: a rejected candidate would reappear as pending and
        # an approved one would look unreviewed.
        if existing.get("status") != PENDING:
            raise ConflictError(
                "this trace was already reviewed; start a new exploration to retry",
                details={
                    "candidate_id": candidate.candidate_id,
                    "status": existing.get("status"),
                    "reviewer": existing.get("reviewer", ""),
                },
            )
        return _summary(existing)  # idempotent re-compile of a pending candidate

    payload: dict[str, Any] = {
        **candidate.to_dict(),
        "status": PENDING,
        "compiled_from": f"exploration:{exploration_id}",
        "case_file": "",
        "reviewed_at": "",
        "rejection_reason": "",
        # Snapshot of the admission evidence at compile time; the approve route
        # re-verifies all of it against the live exploration before promoting.
        "trace_integrity_verified": True,
        "reviewer_is_local_audit_field": True,
    }
    store.save(payload)
    return _summary(payload)


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(request: Request, candidate_id: str, body: ReviewRequest) -> dict[str, Any]:
    if not body.reviewer.strip():
        raise ValidationError("reviewer is required to approve a candidate")
    if is_self_approver(body.reviewer):
        raise ValidationError("candidate self-approval is refused")
    if not body.approval_record.strip():
        raise ValidationError("approval_record is required to approve a candidate")

    store: CandidateStore = request.app.state.candidate_store
    record = store.get(candidate_id)
    if record is None:
        raise NotFoundError(f"candidate not found: {candidate_id}")
    if record.get("status") != PENDING:
        raise ConflictError(
            "candidate is not pending review",
            details={"candidate_id": candidate_id, "status": record.get("status")},
        )

    candidate = _candidate_from_dict(record)

    # --- server-side review gate (R5-4.6 §3.5) -----------------------------
    # The checklist confirmation is NOT a front-end courtesy: approving without
    # explicitly confirming every required item is a 422, no matter what the UI
    # did or did not render. Uncertainty notes need their own acknowledgement,
    # so one click can never wave unresolved questions through.
    checklist = _checklist_from_record(record.get("review_checklist"))
    required_ids = [c["id"] for c in checklist if c.get("required", True)]
    confirmed = {str(x) for x in (body.review_confirmed or [])}
    missing = [i for i in required_ids if i not in confirmed]
    if missing:
        raise ValidationError(
            "review checklist is incomplete; every required item must be confirmed",
            details={
                "candidate_id": candidate_id,
                "missing_confirmations": missing,
                "required_count": len(required_ids),
            },
        )
    unknown = sorted(confirmed - {c["id"] for c in checklist})
    if unknown:
        raise ValidationError(
            "review confirmation references unknown checklist items",
            details={"candidate_id": candidate_id, "unknown": unknown},
        )
    uncertainties = [str(n) for n in (record.get("uncertainty_notes") or []) if str(n).strip()]
    if uncertainties and not body.uncertainty_acknowledged:
        raise ValidationError(
            "candidate has unresolved uncertainty notes; they must be explicitly acknowledged",
            details={
                "candidate_id": candidate_id,
                "uncertainty_count": len(uncertainties),
            },
        )

    # --- provenance re-verification ----------------------------------------
    # Approval re-runs the compile admission chain: the source exploration must
    # still exist, still verify against its hash, and still show a VERIFIED
    # goal — and the candidate must be bound to exactly that trace hash.
    exploration_id = str(record.get("source_exploration_id") or "")
    if not exploration_id:
        raise ConflictError("candidate has no source exploration; provenance is unknown")
    exploration = _exploration_record(request, exploration_id)
    if candidate.source_trace_hash != str(exploration.get("source_trace_hash") or ""):
        raise ConflictError(
            "candidate trace hash does not match its source exploration",
            details={
                "candidate_id": candidate_id,
                "candidate_hash": candidate.source_trace_hash,
                "exploration_hash": exploration.get("source_trace_hash", ""),
            },
        )

    # Compiler static validation re-runs on the rebuilt candidate (structural
    # integrity, business-oracle rule, data-ref bindings). TestCaseVersion
    # validation alone would be weaker: it never re-checks the Oracle sources.
    TestCaseCompiler().validate(candidate)

    # --- human approval flips the Oracles -----------------------------------
    # Human review is the act that approves the Oracle. The compiler explicitly
    # leaves it unapproved, and TestCaseVersion.validate would reject the file
    # if we promoted without this step.
    approved = replace(
        candidate,
        oracle_bindings=tuple(
            replace(
                binding,
                primary=replace(binding.primary, approved=True, approved_by=body.reviewer),
                supporting=tuple(
                    replace(spec, approved=True, approved_by=body.reviewer)
                    for spec in binding.supporting
                ),
            )
            for binding in candidate.oracle_bindings
        ),
    )
    formal = TestCaseCompiler().promote(
        approved, reviewer=body.reviewer, approval_record=body.approval_record
    )
    case_dir = _case_dir(request)
    target = case_dir / f"{formal.case_id}.json"
    with case_lock(case_dir, formal.case_id):
        if target.exists():
            raise ConflictError(
                "formal case file already exists; versions are append-only",
                details={"case_id": formal.case_id, "file": target.name},
            )
        case_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(formal.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    record.update(
        {
            "status": APPROVED,
            "reviewer": body.reviewer,
            "approval_record": body.approval_record,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "case_file": target.name,
            # Audit of WHAT was confirmed, not only WHO confirmed it.
            "review_confirmed": sorted(confirmed),
            "uncertainty_acknowledged": bool(body.uncertainty_acknowledged),
            # reviewer is a local audit field: no identity module exists yet,
            # so the record never claims authenticated identity.
            "reviewer_is_local_audit_field": True,
        }
    )
    store.save(record)
    return _summary(record)


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(request: Request, candidate_id: str, body: ReviewRequest) -> dict[str, Any]:
    if not body.reviewer.strip():
        raise ValidationError("reviewer is required to reject a candidate")
    if is_self_approver(body.reviewer):
        raise ValidationError("candidate self-rejection is refused")
    if not body.reason.strip():
        # A rejection without a reason is not reviewable later — the trace is
        # permanently closed by a reject, so the decision must be explainable.
        raise ValidationError("a rejection reason is required to reject a candidate")
    store: CandidateStore = request.app.state.candidate_store
    record = store.get(candidate_id)
    if record is None:
        raise NotFoundError(f"candidate not found: {candidate_id}")
    if record.get("status") != PENDING:
        raise ConflictError(
            "candidate is not pending review",
            details={"candidate_id": candidate_id, "status": record.get("status")},
        )
    record.update(
        {
            "status": REJECTED,
            "reviewer": body.reviewer,
            "rejection_reason": body.reason.strip(),
            "reviewed_at": datetime.now(UTC).isoformat(),
            "reviewer_is_local_audit_field": True,
        }
    )
    store.save(record)
    return _summary(record)


# ----------------------------------------------------------------- helpers


def _case_dir(request: Request) -> Path:
    """Case library root.

    ``register_workbench`` publishes it; the configured path is the fallback so
    the candidate routes still work when the workbench is disabled.
    """
    configured = getattr(request.app.state, "case_dir", None)
    if configured is not None:
        return Path(configured)
    return Path(request.app.state.settings.case_dir)


def _exploration_record(request: Request, exploration_id: str) -> dict[str, Any]:
    """Load a persisted exploration, refusing one that cannot be compiled.

    R5-4.6 compile admission chain, in order:

      Exploration -> integrity verification -> TestIntent restoration
      -> goal-status validation -> Compiler -> Candidate.

    Any failure here refuses the compile; none of them can be skipped by the
    caller because they all happen inside this loader.
    """
    store = request.app.state.exploration_store
    record = store.get(exploration_id)
    if record is None:
        raise NotFoundError(f"exploration not found: {exploration_id}")
    if record.get("status") != "COMPLETED":
        raise ConflictError(
            "only a completed exploration can be compiled",
            details={"exploration_id": exploration_id, "status": record.get("status")},
        )
    actions = record.get("action_sequence") or []
    if not any(not a.get("rejected") for a in actions):
        raise ValidationError(
            "exploration has no accepted action; nothing to compile",
            details={"exploration_id": exploration_id},
        )

    # 1. Trace integrity against the persisted bytes: a trace whose hash no
    #    longer matches its own content (or one carrying a stale hash) must
    #    never become a candidate.
    if not verify_trace_record(record):
        raise ConflictError(
            "exploration trace integrity check failed; refusing to compile",
            details={
                "exploration_id": exploration_id,
                "source_trace_hash": record.get("source_trace_hash", ""),
                "hint": "the trace was modified after it was written, or its hash is stale",
            },
        )

    # New scenario traces carry the scenario-level test oracle verdict. A
    # verified business goal alone cannot admit a negative-path trace whose
    # expected error was absent or inconclusive.
    recorded_case_verdict = record.get("test_case_verdict")
    path_spec = record.get("path_spec")
    if isinstance(path_spec, dict):
        observations = list(record.get("observations") or [])
        final_observation = dict((observations or [{}])[-1] or {})
        expect_report = evaluate_assertions(
            list(path_spec.get("expect") or []),
            final_observation,
            observations=observations,
        )
        recomputed_case_verdict = evaluate_test_case_verdict(
            goal_status=str(record.get("goal_status") or "NOT_EVALUATED"),
            expected_goal_status=str(path_spec.get("expected_goal_status") or ""),
            required_expect_verdicts=[
                result.verdict for result in expect_report.results if result.required
            ],
        ).value
        scenario = default_registry().get(
            str(record.get("scenario_id") or ""), str(record.get("scenario_version") or "")
        )
        if scenario is None or scenario.digest != record.get("scenario_digest"):
            raise ConflictError("scenario identity is unavailable or changed; refusing to compile")
        expected_path_spec = scenario.path_spec_for_path(str(record.get("scenario_path") or ""))
        if expected_path_spec != path_spec:
            raise ConflictError("scenario path specification changed; refusing to compile")
        if recorded_case_verdict != recomputed_case_verdict:
            raise ConflictError(
                "recorded scenario test-case verdict disagrees with its evidence",
                details={
                    "recorded": recorded_case_verdict,
                    "recomputed": recomputed_case_verdict,
                },
            )
        recorded_case_verdict = recomputed_case_verdict
    if recorded_case_verdict not in (None, "PASSED"):
        raise ConflictError(
            "scenario test-case verdict does not admit compilation",
            details={
                "exploration_id": exploration_id,
                "test_case_verdict": recorded_case_verdict,
            },
        )

    # 2. Goal status: recomputed here (not trusted from a stored field) from
    #    the trace's own observations and TestIntent. VERIFIED is the only
    #    state that admits compilation.
    intent = _intent_from_record(record)
    evaluation = evaluate_goal(intent, record.get("observations") or [])
    if evaluation.status != GoalStatus.VERIFIED:
        raise ConflictError(
            "business goal is not verified by the exploration trace; refusing to compile",
            details={
                "exploration_id": exploration_id,
                "goal_status": evaluation.status,
                "reason": evaluation.reason,
                "checks": [c.to_dict() for c in evaluation.checks],
            },
        )
    # Cross-check against the goal status recorded at exploration time: a
    # mismatch means the trace or its goal evidence changed after the fact.
    recorded_status = str(record.get("goal_status") or "")
    if recorded_status and recorded_status != evaluation.status:
        raise ConflictError(
            "recorded goal status disagrees with recomputed goal evidence",
            details={
                "exploration_id": exploration_id,
                "recorded": recorded_status,
                "recomputed": evaluation.status,
            },
        )
    return record


def _intent_from_record(record: dict[str, Any]) -> TestIntent:
    """The human-written goal stored with the exploration (R5-4.1).

    Legacy traces have no ``test_intent`` block; the intent then degrades to the
    intent_ref label, which is honest — the compiler will have no declared
    target and will say so in its checklist.
    """
    raw = record.get("test_intent") or {}
    ref = str(record.get("test_intent_ref") or "")
    return TestIntent(
        intent_id=str(raw.get("intent_id") or (ref or f"i-{record.get('exploration_id', '')}")),
        description=str(raw.get("description") or ref),
        source_ref=str(raw.get("source_ref") or ref),
        goal=str(raw.get("goal") or ""),
        expected_outcomes=tuple(raw.get("expected_outcomes") or ()),
        target=str(raw.get("target") or ""),
        required_assertions=tuple(raw.get("required_assertions") or ()),
        constraints=tuple(raw.get("constraints") or ()),
    )


def _trace_from_record(record: dict[str, Any]) -> ExplorationTrace:
    return ExplorationTrace(
        exploration_id=str(record["exploration_id"]),
        test_intent_ref=str(record.get("test_intent_ref") or ""),
        application_version=str(record.get("application_version") or ""),
        browser_version=str(record.get("browser_version") or ""),
        agent_version=str(record.get("agent_version") or ""),
        prompt_version=str(record.get("prompt_version") or "none"),
        tool_version=str(record.get("tool_version") or ""),
        test_intent=dict(record.get("test_intent") or {}),
        action_sequence=[dict(a) for a in (record.get("action_sequence") or [])],
        observations=[dict(o) for o in (record.get("observations") or [])],
        agent_decisions=[dict(d) for d in (record.get("agent_decisions") or [])],
        page_state_ref=str(record.get("page_state_ref") or ""),
        event_refs=list(record.get("event_refs") or []),
        evidence_refs=list(record.get("evidence_refs") or []),
        stop_reason=str(record.get("stop_reason") or ""),
        source_trace_hash=str(record.get("source_trace_hash") or ""),
        hash_version=int(record.get("hash_version") or 1),
        goal_status=str(record.get("goal_status") or ""),
        goal_checks=[dict(c) for c in (record.get("goal_checks") or [])],
        goal_reason=str(record.get("goal_reason") or ""),
        started_at=str(record.get("started_at") or ""),
        finished_at=str(record.get("finished_at") or ""),
    )


def _trace_from_exploration(request: Request, exploration_id: str) -> ExplorationTrace:
    """Rebuild a trace from a persisted exploration (kept for callers/tests)."""
    return _trace_from_record(_exploration_record(request, exploration_id))


def register_candidates(app: Any, *, candidate_dir: Path) -> None:
    app.state.candidate_store = CandidateStore(candidate_dir)
    app.include_router(router)
