"""Second-round workbench extensions: the read/decision endpoints the UI needs.

Why this module exists: the stage-3 router exposed Task/Run, but the workbench
pages for TestIntent, Diagnosis, Probe audit, Repair review and Benchmark had no
backend to read from. Every endpoint here is derived from real state — loaded
case files, the persisted run report, the probe catalogue, the repair state
machine, the benchmark managers — and returns an honest empty collection when
there is genuinely nothing yet. No endpoint fabricates sample rows.

Repair approval is deliberately gated: an approver of ``agent``/``ai``/``system``
is rejected, because the platform must never approve its own repair.
"""

from __future__ import annotations

import enum
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from benchmark.engine import DatasetManager, ExperimentManager, GroundTruthManager
from packages.agents.diagnosis import DiagnosisService
from packages.agents.probe_catalog import build_default_engine, specs_as_dicts
from packages.core.errors import ConflictError, NotFoundError, ValidationError
from packages.execution.models import (
    TestCaseVersion,
    case_from_dict,
    load_case,
    load_cases_dir,
    load_cases_dir_report,
)
from packages.execution.repair import DiffEntry, RepairService
from packages.execution.review_policy import SELF_APPROVER_IDENTIFIERS, is_self_approver
from packages.execution.sandbox import RepairSandbox

router = APIRouter(prefix="/api/v1", tags=["workbench-ext"])

SELF_APPROVERS = SELF_APPROVER_IDENTIFIERS


# --------------------------------------------------------------------- helpers


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _parse_ts(value: Any) -> datetime | None:
    """Parse both timestamp shapes the platform meets.

    The run report uses aware ISO strings (``...+00:00``); the eval plane stores
    naive local timestamps. Naive values are treated as UTC, which matches how the
    two are produced in the same process.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _in_window(
    event: dict[str, Any],
    started_at: str,
    finished_at: str,
    *,
    lookback_s: int = 120,
    slack_s: int = 60,
) -> bool:
    """Keep fault evidence related to THIS run.

    A lookback is required, not optional: faults are armed *before* the run starts,
    so a strict [start, finish] window would drop the ARMED entry and quietly
    demote every UI-fault diagnosis to PROBABLE.
    """
    at = _parse_ts(event.get("occurred_at"))
    start = _parse_ts(started_at)
    if at is None or start is None:
        return True  # cannot judge: keep rather than silently drop evidence
    finish = _parse_ts(finished_at)
    lower = start - timedelta(seconds=lookback_s)
    if at < lower:
        return False
    if finish is not None and at > finish + timedelta(seconds=slack_s):
        return False
    return True


def _case_dir(request: Request) -> Path:
    return Path(request.app.state.case_dir)


def _find_case(case_dir: Path, case_id: str) -> tuple[TestCaseVersion, str]:
    matches: list[tuple[TestCaseVersion, str]] = []
    for path in sorted(case_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(raw, dict) or "case_id" not in raw:
            continue
        if raw.get("case_id") == case_id:
            matches.append((load_case(path), path.name))
    if matches:
        return max(matches, key=lambda item: item[0].version)
    raise NotFoundError(f"unknown case: {case_id}")


def _fault_events(
    request: Request, *, window: tuple[str, str] | None = None
) -> list[dict[str, Any]]:
    """Diagnostic-only read from the eval plane. Never an Oracle input.

    ``window`` (started_at, finished_at) keeps historical fault records out of the
    bundle: the EventLog is global, and an old ARMED event would otherwise let a
    healthy run be diagnosed as a fault.
    """
    app = request.app
    base = getattr(app.state, "teamflow_base_url", "")
    token = getattr(app.state, "eval_token", "")
    if not base:
        return []
    try:
        with httpx.Client(base_url=base, timeout=3.0, trust_env=False) as client:
            resp = client.get("/__eval__/events", headers={"X-Eval-Token": token})
            if resp.status_code != 200:
                return []
            data = resp.json()
            events = data if isinstance(data, list) else list(data.get("events", []))
            if window is None:
                return events
            return [e for e in events if _in_window(e, *window)]
    except httpx.HTTPError:
        # Eval plane unreachable is not an error for diagnosis: it degrades to
        # "no authoritative evidence", which correctly yields INCONCLUSIVE.
        return []


# -------------------------------------------------------------------- overview


@router.get("/overview")
def overview(request: Request) -> dict[str, Any]:
    app = request.app
    orch = app.state.orchestrator
    tasks = orch.tasks.list()
    runs: list[dict[str, Any]] = []
    for task in tasks:
        runs.extend(v.to_dict() for v in orch.history(task["task_id"]))

    by_status: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    for run in runs:
        by_status[run["status"]] = by_status.get(run["status"], 0) + 1
        verdict = run.get("test_verdict") or "NOT_EVALUATED"
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1

    cases, invalid_cases = load_cases_dir_report(_case_dir(request))
    store = getattr(app.state, "event_store", None)
    manager = getattr(app.state, "evidence_manager", None)
    events = store.query(limit=1000) if store else []
    evidence = manager.query(limit=1000) if manager else []

    # 14-day run trend. Buckets are fixed (one per day, zero-filled) so the
    # sparkline's x-axis is time, not "index of days that happened to have runs".
    today = datetime.now(UTC).date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    daily = {d.isoformat(): 0 for d in days}
    for run in runs:
        parsed = _parse_ts(run.get("created_at"))
        if parsed is None:
            continue
        key = parsed.astimezone(UTC).date().isoformat()
        if key in daily:
            daily[key] += 1
    runs_daily = [{"date": k, "count": v} for k, v in daily.items()]

    # Rates are computed ONLY over runs that actually reached a verdict.
    # Dividing by all runs would understate quality while runs are in flight,
    # and inventing 0% for "no data" would be a lie — so we return None instead.
    evaluated = sum(by_verdict.get(k, 0) for k in ("PASSED", "FAILED", "INCONCLUSIVE"))
    passed = by_verdict.get("PASSED", 0)
    failed = by_verdict.get("FAILED", 0)
    success_rate = round(passed / evaluated * 100, 1) if evaluated else None
    failure_rate = round(failed / evaluated * 100, 1) if evaluated else None

    # Explorer activity, read from the same store the Explorer page uses.
    exp_store = getattr(app.state, "exploration_store", None)
    explorations = exp_store.list() if exp_store else []
    scripted = sum(1 for e in explorations if not e.get("is_llm"))
    llm = sum(1 for e in explorations if e.get("is_llm"))

    return {
        "tasks": len(tasks),
        "runs": len(runs),
        "runs_by_status": by_status,
        "runs_by_verdict": by_verdict,
        "runs_daily": runs_daily,
        "evaluated": evaluated,
        "success_rate": success_rate,
        "failure_rate": failure_rate,
        # Flattened once here so the workbench home page avoids N+1 run
        # history requests (one per task). Most recent first, capped.
        "recent_runs": sorted(runs, key=lambda r: r.get("created_at") or "", reverse=True)[:10],
        "cases": len(cases),
        # Cases that failed validation. Reported, never hidden: a broken file
        # used to 500 the whole overview, and silence would be worse than a
        # visible warning with the file name and the reason.
        "invalid_cases": invalid_cases,
        "events": len(events),
        "evidence": len(evidence),
        "repairs_pending": sum(
            1 for p in app.state.repair_service.list() if p["state"] == "PENDING_APPROVAL"
        ),
        "explorations": {"total": len(explorations), "scripted": scripted, "llm": llm},
    }


# ------------------------------------------------------------------- dispatch


@router.get("/dispatch/status")
def dispatch_status(request: Request) -> dict[str, Any]:
    """Queue depth, dispatcher counters and the dead-letter list."""
    orch = request.app.state.orchestrator
    return {
        "broker": orch.broker.depth(),
        "dispatcher": orch.dispatcher.stats(),
        "workers": orch.worker_count,
        "dead_letters": [
            {
                "job_id": letter.job.job_id,
                "run_id": letter.job.run_id,
                "task_id": letter.job.task_id,
                "case_id": letter.job.case_id,
                "reason": letter.reason,
                "failed_at": letter.failed_at,
                "deliveries": letter.deliveries,
            }
            for letter in orch.broker.dead_letters()
        ],
    }


# --------------------------------------------------------------------- intents


class IntentBody(BaseModel):
    intent_id: str
    description: str
    source_ref: str = ""


def _intents_file(case_dir: Path) -> Path:
    # Sits beside the cases directory so load_cases_dir never parses it as a case.
    return case_dir.parent / "intents.json"


@router.get("/intents")
def list_intents(request: Request) -> list[dict[str, Any]]:
    case_dir = _case_dir(request)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in load_cases_dir(case_dir):
        intent = case.intent
        seen.add(intent.intent_id)
        out.append(
            {
                "intent_id": intent.intent_id,
                "description": intent.description,
                "source_ref": intent.source_ref,
                "case_id": case.case_id,
                "case_version": case.version,
                "origin": "case",
            }
        )
    path = _intents_file(case_dir)
    if path.exists():
        for item in json.loads(path.read_text(encoding="utf-8")):
            if item.get("intent_id") not in seen:
                item["origin"] = "library"
                out.append(item)
    return out


@router.post("/intents", status_code=201)
def create_intent(body: IntentBody, request: Request) -> dict[str, Any]:
    if not body.intent_id.strip() or not body.description.strip():
        raise ValidationError("intent_id and description are required")
    path = _intents_file(_case_dir(request))
    existing: list[dict[str, Any]] = []
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    if any(i.get("intent_id") == body.intent_id for i in existing):
        raise ConflictError(f"intent exists: {body.intent_id}")
    record = {
        "intent_id": body.intent_id,
        "description": body.description,
        "source_ref": body.source_ref,
        "case_id": None,
        "case_version": None,
    }
    existing.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


# ------------------------------------------------------------------ case detail


@router.get("/cases/{case_id}")
def case_detail(case_id: str, request: Request) -> dict[str, Any]:
    case, source_file = _find_case(_case_dir(request), case_id)
    data = _jsonable(case)
    data["source_file"] = source_file
    data["versions"] = _versions_for(_case_dir(request), case_id)
    return data


def _versions_for(case_dir: Path, case_id: str) -> list[dict[str, Any]]:
    """Every on-disk version of this case id (append-only versions are files)."""
    versions: list[dict[str, Any]] = []
    for path in sorted(case_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(raw, dict) and raw.get("case_id") == case_id:
            versions.append(
                {
                    "version": raw.get("version"),
                    "file": path.name,
                    "compiled_from": raw.get("compiled_from", "manual"),
                }
            )
    return sorted(versions, key=lambda v: v.get("version") or 0)


# ------------------------------------------------------------------- diagnosis


@router.get("/runs/{run_id}/diagnosis")
def run_diagnosis(run_id: str, request: Request) -> dict[str, Any]:
    orch = request.app.state.orchestrator
    report = orch.runs.report(run_id)
    steps = report.get("steps", [])
    bundle = {
        "test_verdict": report.get("test_verdict", ""),
        "execution_status": report.get("execution_status", ""),
        "failed_step_ids": [s.get("step_id") for s in steps if not s.get("ok", True)],
        "observations": report.get("observations", {}),
        "assertions": report.get("assertion_results", []),
        "evidence": report.get("evidence", []),
        "events": report.get("events", []),
        "fault_events": _fault_events(
            request,
            window=(str(report.get("started_at", "")), str(report.get("finished_at", ""))),
        ),
    }
    diagnosis = DiagnosisService().diagnose(f"dg_{run_id}", run_id, bundle)
    return diagnosis.to_dict()


# ----------------------------------------------------------------- probes


@router.get("/probes")
def probe_registry() -> list[dict[str, Any]]:
    return specs_as_dicts()


@router.get("/probes/audit")
def probe_audit(request: Request) -> list[dict[str, Any]]:
    engine = request.app.state.probe_engine
    return [asdict(entry) for entry in engine.audit]


# ----------------------------------------------------------------- repairs


class ApproveBody(BaseModel):
    approver: str
    approval_record: str


class RejectBody(BaseModel):
    reason: str
    reviewer: str = "human"
    rejected_by: str = ""


class CreateRepairBody(BaseModel):
    case_id: str
    candidate: dict[str, Any]
    change_kinds: list[str]
    diff: list[dict[str, str]]
    source_run_id: str
    hypothesis_id: str
    evidence_refs: list[str]


class RepairOperatorBody(BaseModel):
    operator: str
    reason: str


def _repair_view(request: Request, proposal_id: str) -> dict[str, Any]:
    service = request.app.state.repair_service
    try:
        record = service.get(proposal_id)
    except ConflictError as exc:
        raise NotFoundError(str(exc)) from exc
    replay = request.app.state.repair_service.get_replay(proposal_id)
    record["sandbox_replay"] = replay
    return record


@router.get("/repairs/cases/{case_id}/formal")
def repair_source_case(case_id: str, request: Request) -> dict[str, Any]:
    case, source = _find_case(_case_dir(request), case_id)
    return {"case": case.to_dict(), "source_file": source}


@router.post("/repairs", status_code=201)
def create_repair(body: CreateRepairBody, request: Request) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,79}", body.case_id):
        raise ValidationError("case_id contains unsupported characters")
    if not body.source_run_id.strip() or not body.hypothesis_id.strip() or not body.evidence_refs:
        raise ValidationError("repair proposal requires run, hypothesis and evidence references")
    diagnosis = run_diagnosis(body.source_run_id, request)
    hypothesis = next(
        (
            row
            for row in diagnosis.get("hypotheses", [])
            if row.get("hypothesis_id") == body.hypothesis_id
        ),
        None,
    )
    if hypothesis is None or hypothesis.get("status") not in {"SUPPORTED", "CONFIRMED"}:
        raise ValidationError("repair proposal requires a supported diagnosis hypothesis")
    supporting = {str(item.get("ref_id") or "") for item in hypothesis.get("supporting", [])}
    if not set(body.evidence_refs) <= supporting:
        raise ValidationError("repair evidence refs must cite supporting diagnosis evidence")
    base, _ = _find_case(_case_dir(request), body.case_id)
    candidate = case_from_dict(body.candidate)
    if candidate.case_id != body.case_id:
        raise ValidationError("candidate case_id does not match selected formal case")
    proposal_id = f"rp_{uuid.uuid4().hex[:12]}"
    diff = [DiffEntry(**item) for item in body.diff]
    try:
        proposal = request.app.state.repair_service.create_proposal(
            proposal_id,
            base,
            candidate,
            change_kinds=tuple(body.change_kinds),
            diff=diff,
            source_run_id=body.source_run_id,
            source_hypothesis_id=body.hypothesis_id,
            source_evidence_refs=tuple(body.evidence_refs),
        )
    except ConflictError as exc:
        raise ValidationError(str(exc)) from exc
    return {**proposal.to_dict(), "sandbox_replay": None}


@router.get("/repairs")
def list_repairs(request: Request) -> list[dict[str, Any]]:
    return [
        _repair_view(request, str(item["proposal_id"]))
        for item in request.app.state.repair_service.list()
    ]


@router.get("/repairs/audit")
def repair_audit(request: Request) -> list[dict[str, Any]]:
    rows = request.app.state.repair_service.list()
    return [
        {
            "proposal_id": row["proposal_id"],
            "case_id": row["case_id"],
            "state": row["state"],
            "approved_by": row["approved_by"],
            "rejected_by": row.get("rejected_by", ""),
            "approval_record": row["approval_record"],
            "source_run_id": row.get("source_run_id", ""),
            "source_hypothesis_id": row.get("source_hypothesis_id", ""),
            "source_evidence_refs": row.get("source_evidence_refs", []),
            "promotion_record": row["promotion_record"],
        }
        for row in rows
    ]


@router.post("/repairs/{proposal_id}/validate")
def validate_repair(proposal_id: str, request: Request) -> dict[str, Any]:
    try:
        result = request.app.state.repair_service.validate(proposal_id)
    except ConflictError as exc:
        raise NotFoundError(str(exc)) from exc
    return _repair_view(request, result.proposal_id)


@router.post("/repairs/{proposal_id}/submit")
def submit_repair(proposal_id: str, request: Request) -> dict[str, Any]:
    try:
        result = request.app.state.repair_service.submit_for_approval(proposal_id)
    except ConflictError as exc:
        raise ValidationError(str(exc)) from exc
    return _repair_view(request, result.proposal_id)


@router.get("/repairs/{proposal_id}")
def get_repair(proposal_id: str, request: Request) -> dict[str, Any]:
    return _repair_view(request, proposal_id)


@router.post("/repairs/{proposal_id}/approve")
def approve_repair(proposal_id: str, body: ApproveBody, request: Request) -> dict[str, Any]:
    if is_self_approver(body.approver):
        raise ValidationError(
            "repair approval requires a named human approver; self-approval is refused"
        )
    if not body.approval_record.strip():
        raise ValidationError("approval_record is required")
    service = request.app.state.repair_service
    try:
        proposal = service.approve(
            proposal_id, approver=body.approver, approval_record=body.approval_record
        )
    except ConflictError as exc:
        raise NotFoundError(str(exc)) from exc
    return proposal.to_dict()


@router.post("/repairs/{proposal_id}/reject")
def reject_repair(proposal_id: str, body: RejectBody, request: Request) -> dict[str, Any]:
    if not body.reason.strip():
        raise ValidationError("rejection reason is required")
    rejected_by = body.rejected_by or body.reviewer
    if is_self_approver(rejected_by):
        raise ValidationError("repair rejection requires a named human reviewer")
    service = request.app.state.repair_service
    try:
        proposal = service.reject(proposal_id, body.reason, rejected_by=rejected_by)
    except ConflictError as exc:
        raise NotFoundError(str(exc)) from exc
    return _repair_view(request, proposal.proposal_id)


@router.post("/repairs/{proposal_id}/sandbox-replay")
def sandbox_repair(proposal_id: str, request: Request) -> dict[str, Any]:
    service = request.app.state.repair_service
    record = _repair_view(request, proposal_id)
    if record["state"] != "APPROVED":
        raise ValidationError("sandbox replay requires human approval")
    base, candidate = service._candidate_pair(proposal_id)
    root = Path(tempfile.gettempdir()) / "traceguard-repair-sandboxes"
    result = RepairSandbox(
        proposal_id=proposal_id,
        base_case=base,
        candidate=candidate,
        on_evidence=None,
        root=root,
    ).run()
    replay = {
        "proposal_id": proposal_id,
        "candidate_digest": service.case_digest(candidate),
        "base_version": record["base_testcase_version"],
        "verdict": result.verdict,
        "reason": result.reason,
        "cleanup_error": result.cleanup_error,
        "evidence_sha256": result.evidence_sha256,
        "evidence_hash": result.evidence_sha256,
        "evidence": result.evidence,
        "replay_result": {"verdict": result.verdict, "reason": result.reason},
        "replayed_at": datetime.now(UTC).isoformat(),
        "status": "PASSED"
        if result.verdict == "VALIDATED" and not result.cleanup_error
        else "FAILED",
    }
    service.save_replay(proposal_id, replay)
    if replay["status"] != "PASSED":
        raise ValidationError(f"sandbox replay did not pass: {result.reason}")
    return {**_repair_view(request, proposal_id), "sandbox_replay": replay}


@router.post("/repairs/{proposal_id}/promote")
def promote_repair(proposal_id: str, body: RepairOperatorBody, request: Request) -> dict[str, Any]:
    if not body.operator.strip():
        raise ValidationError("promotion requires a named human operator")
    if not body.reason.strip():
        raise ValidationError("promotion reason is required for audit")
    if is_self_approver(body.operator):
        raise ValidationError("automated actors cannot promote repair assets")
    record = _repair_view(request, proposal_id)
    if record["state"] != "APPROVED":
        raise ValidationError("promotion requires a human-approved proposal")
    replay = request.app.state.repair_service.get_replay(proposal_id)
    if not replay or replay.get("status") != "PASSED":
        raise ValidationError("promotion requires a successful sandbox replay")
    case_id = str(record["case_id"])
    current, _ = _find_case(_case_dir(request), case_id)
    target_path = _case_dir(request) / f"{case_id}.v{record['candidate_testcase_version']}.json"
    recovered_file = False
    if target_path.exists():
        base_candidate, candidate = request.app.state.repair_service._candidate_pair(proposal_id)
        expected = {**candidate.to_dict(), "version": int(record["candidate_testcase_version"])}
        try:
            recovered_file = json.loads(target_path.read_text(encoding="utf-8")) == expected
        except (OSError, ValueError):
            recovered_file = False
        if not recovered_file:
            raise ConflictError("formal version file already exists; refusing overwrite")
        if current.version == int(record["candidate_testcase_version"]):
            current = base_candidate
        elif current.version > int(record["candidate_testcase_version"]):
            raise ConflictError("a later formal version exists; refusing repair recovery")
    else:
        recovered_file = False

    def persist_formal(version: Any) -> None:
        nonlocal recovered_file
        if target_path.exists():
            if json.loads(target_path.read_text(encoding="utf-8")) == version.to_dict():
                recovered_file = True
                return
            raise ConflictError("formal version file already exists; refusing overwrite")
        temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(version.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temp_path, target_path)
        recovered_file = False

    def rollback_formal(version: Any) -> None:
        if not recovered_file and target_path.exists():
            try:
                existing = json.loads(target_path.read_text(encoding="utf-8"))
                if existing == version.to_dict():
                    target_path.unlink()
            except (OSError, ValueError):
                pass

    try:
        promoted, version = request.app.state.repair_service.promote(
            proposal_id,
            current_formal=current,
            sandbox_replay=lambda _candidate: True,
            operator=body.operator.strip(),
            promotion_reason=body.reason.strip(),
            persist_formal=persist_formal,
            rollback_formal=rollback_formal,
        )
    except ConflictError as exc:
        raise ValidationError(str(exc)) from exc
    except OSError as exc:
        raise ValidationError(f"promotion asset write failed: {type(exc).__name__}") from exc
    if promoted.state != "PROMOTED":
        raise ConflictError(promoted.failure_reason or "formal version changed during promotion")
    return _repair_view(request, proposal_id)


# ----------------------------------------------------------------- benchmark


@router.get("/benchmark/datasets")
def benchmark_datasets(request: Request) -> list[dict[str, Any]]:
    return [d.to_dict() for d in request.app.state.dataset_manager.all()]


@router.get("/benchmark/experiments")
def benchmark_experiments(request: Request) -> list[dict[str, Any]]:
    return [e.to_dict() for e in request.app.state.experiment_manager.all()]


# ---------------------------------------------------------------- registration


def register_extensions(
    app: Any,
    *,
    case_dir: Path,
    benchmark_data_dir: Path | None = None,
) -> None:
    """Attach the singletons these endpoints read from, then mount the router.

    GT isolation (R4-2): the GroundTruthManager is intentionally NOT exposed on
    ``app.state`` — no business route can reach GT records. Only the two
    read-only benchmark endpoints (datasets/experiments) are mounted, and
    experiment evaluation receives scenario ids, never GT objects.
    """
    app.state.probe_engine = build_default_engine()
    repair_store_root = benchmark_data_dir or Path("benchmark") / "data"
    app.state.repair_service = RepairService(repair_store_root / "repairs.jsonl")

    gt = GroundTruthManager()
    datasets = DatasetManager(gt)
    experiments = ExperimentManager(datasets, gt)

    # Restore persisted benchmark state (append-only files); a fresh install
    # simply has no files yet and the endpoints return explicit empty states.
    from benchmark.store import BenchmarkStore

    store = BenchmarkStore(benchmark_data_dir or Path("benchmark") / "data")
    store.restore(gt, datasets, experiments)

    # Deliberately NOT seeded here: DatasetManager.create requires a Ground Truth
    # record per scenario, and inventing GT would be fabrication. Until the
    # benchmark track seeds approved GT (see docs/benchmark/R4-GT-Mapping-Proposal.md),
    # these endpoints return an empty list and the UI shows an explicit empty state.
    # HTTP surface gets read-only facades WITHOUT the GT handle; the real
    # managers (with GT) live only in the trusted benchmark runner context.
    from benchmark.engine import DatasetManagerView, ExperimentManagerView

    app.state.dataset_manager = DatasetManagerView(datasets)
    app.state.experiment_manager = ExperimentManagerView(experiments)

    app.include_router(router)


__all__ = ["register_extensions", "router"]
