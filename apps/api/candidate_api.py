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
from packages.agents.exploration import ExplorationTrace
from packages.core.errors import ConflictError, NotFoundError, ValidationError
from packages.execution.models import (
    AssertionSpec,
    CleanupPolicy,
    OracleBinding,
    OracleSpec,
    TestIntent,
    TestStep,
)

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


def _candidate_from_dict(record: dict[str, Any]) -> CandidateTestCase:
    """Rebuild the dataclass so the compiler's own guards can re-run on approve."""
    return CandidateTestCase(
        candidate_id=str(record["candidate_id"]),
        case_id=str(record["case_id"]),
        title=str(record["title"]),
        intent=TestIntent(
            intent_id=str(record["intent"]["intent_id"]),
            description=str(record["intent"]["description"]),
            source_ref=str(record["intent"].get("source_ref", "")),
        ),
        steps=tuple(
            TestStep(
                step_id=str(s["step_id"]),
                action=str(s["action"]),
                target=str(s.get("target", "")),
                value=str(s.get("value", "")),
                expected=str(s.get("expected", "")),
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
    )


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": record.get("candidate_id"),
        "case_id": record.get("case_id"),
        "title": record.get("title"),
        "status": record.get("status", PENDING),
        "source_exploration_id": record.get("source_exploration_id"),
        "source_trace_hash": record.get("source_trace_hash"),
        "step_count": len(record.get("steps", [])),
        "assertion_count": len(record.get("assertions", [])),
        "uncertainty_notes": list(record.get("uncertainty_notes") or []),
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
    trace = _trace_from_exploration(request, exploration_id)
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
        trace, case_id=case_id, title=body.title or f"compiled from {exploration_id}"
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

    record: dict[str, Any] = {
        **candidate.to_dict(),
        "status": PENDING,
        "compiled_from": f"exploration:{exploration_id}",
        "case_file": "",
        "reviewed_at": "",
        "rejection_reason": "",
    }
    store.save(record)
    return _summary(record)


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(request: Request, candidate_id: str, body: ReviewRequest) -> dict[str, Any]:
    if not body.reviewer.strip():
        raise ValidationError("reviewer is required to approve a candidate")
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
        }
    )
    store.save(record)
    return _summary(record)


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(request: Request, candidate_id: str, body: ReviewRequest) -> dict[str, Any]:
    if not body.reviewer.strip():
        raise ValidationError("reviewer is required to reject a candidate")
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
            "rejection_reason": body.reason,
            "reviewed_at": datetime.now(UTC).isoformat(),
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


def _trace_from_exploration(request: Request, exploration_id: str) -> ExplorationTrace:
    """Rebuild a trace from a persisted exploration, refusing unusable ones."""
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
    return ExplorationTrace(
        exploration_id=str(record["exploration_id"]),
        test_intent_ref=str(record.get("test_intent_ref") or ""),
        application_version=str(record.get("application_version") or ""),
        browser_version=str(record.get("browser_version") or ""),
        agent_version=str(record.get("agent_version") or ""),
        prompt_version=str(record.get("prompt_version") or "none"),
        tool_version=str(record.get("tool_version") or ""),
        action_sequence=[dict(a) for a in actions],
        page_state_ref=str(record.get("page_state_ref") or ""),
        event_refs=list(record.get("event_refs") or []),
        evidence_refs=list(record.get("evidence_refs") or []),
        stop_reason=str(record.get("stop_reason") or ""),
        source_trace_hash=str(record.get("source_trace_hash") or ""),
        started_at=str(record.get("started_at") or ""),
        finished_at=str(record.get("finished_at") or ""),
    )


def register_candidates(app: Any, *, candidate_dir: Path) -> None:
    app.state.candidate_store = CandidateStore(candidate_dir)
    app.include_router(router)
