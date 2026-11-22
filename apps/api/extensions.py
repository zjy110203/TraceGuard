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
from packages.execution.models import TestCaseVersion, load_case, load_cases_dir
from packages.execution.repair import RepairService

router = APIRouter(prefix="/api/v1", tags=["workbench-ext"])

SELF_APPROVERS = frozenset({"", "agent", "ai", "system", "auto", "traceguard"})


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
    for path in sorted(case_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(raw, dict) or "case_id" not in raw:
            continue
        if raw.get("case_id") == case_id:
            return load_case(path), path.name
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

    cases = load_cases_dir(_case_dir(request))
    store = getattr(app.state, "event_store", None)
    manager = getattr(app.state, "evidence_manager", None)
    events = store.query(limit=1000) if store else []
    evidence = manager.query(limit=1000) if manager else []

    return {
        "tasks": len(tasks),
        "runs": len(runs),
        "runs_by_status": by_status,
        "runs_by_verdict": by_verdict,
        # Flattened once here so the workbench home page avoids N+1 run
        # history requests (one per task). Most recent first, capped.
        "recent_runs": sorted(runs, key=lambda r: r.get("created_at") or "", reverse=True)[:10],
        "cases": len(cases),
        "events": len(events),
        "evidence": len(evidence),
        "repairs_pending": sum(
            1 for p in app.state.repair_service.list() if p["state"] == "PENDING_APPROVAL"
        ),
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


@router.get("/repairs")
def list_repairs(request: Request) -> list[dict[str, Any]]:
    return request.app.state.repair_service.list()


@router.get("/repairs/{proposal_id}")
def get_repair(proposal_id: str, request: Request) -> dict[str, Any]:
    try:
        return request.app.state.repair_service.get(proposal_id)
    except ConflictError as exc:
        raise NotFoundError(str(exc)) from exc


@router.post("/repairs/{proposal_id}/approve")
def approve_repair(proposal_id: str, body: ApproveBody, request: Request) -> dict[str, Any]:
    if body.approver.strip().lower() in SELF_APPROVERS:
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
    service = request.app.state.repair_service
    try:
        proposal = service.reject(proposal_id, body.reason)
    except ConflictError as exc:
        raise NotFoundError(str(exc)) from exc
    return proposal.to_dict()


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
    app.state.repair_service = RepairService()

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
