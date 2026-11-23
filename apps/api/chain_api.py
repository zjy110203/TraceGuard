"""Full-chain REST surface: TestCase -> Run -> Oracle -> Evidence -> Diagnosis.

Why this route exists
---------------------
``POST /tasks/{id}/runs`` executes a case through the orchestrator but stops at
the run/step rows: nothing is written to the evidence plane. That is why the
Evidence, Evidence-Graph and Diagnosis pages could only ever show empty states —
the data that would fill them was produced exclusively by
``scripts/run_benchmark.py``, which is not reachable from the product.

This route exposes ``packages.execution.pipeline.FullChain``, the same code the
benchmark script uses, and persists exactly what it captured:

  * events   -> ``EventStore.append`` (per-producer sequence, idempotent)
  * evidence -> ``EvidenceManager.import_record`` (original hash preserved,
                binary screenshots moved to the content-addressed blob store)
  * graph    -> ``CorrelationGraph`` (already backed by ``gr_node``/``gr_edge``)
  * report   -> append-only JSON, one file per chain

Honesty boundaries kept intact:
  * Only human-approved cases may run. ``approved_by`` empty => 409, so this
    route can never be used to execute an unreviewed candidate.
  * No fault events and no probes are injected from this route. Probes would
    need handler stubs to answer, and a stubbed answer is not evidence; the
    chain therefore reports the diagnosis it can actually justify from what the
    browser observed.
  * A failing execution is a real result, not an error: the report is stored
    with ``completed=false`` and the failure detail in its stages.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from packages.core.errors import ConflictError, NotFoundError, ValidationError
from packages.evidence.storage import BlobStore
from packages.execution.models import TestCaseVersion, load_cases_dir
from packages.execution.pipeline import FullChain

router = APIRouter(prefix="/api/v1", tags=["chain"])

_CHAIN_ID = re.compile(r"chain_[A-Za-z0-9_-]+")


# ----------------------------------------------------------------- storage


class ChainStore:
    """Append-only JSON store, one report per chain."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def save(self, report: dict[str, Any]) -> None:
        chain_id = str(report["chain_id"])
        if not _CHAIN_ID.fullmatch(chain_id):
            raise ValidationError(f"invalid chain id: {chain_id}")
        path = self._dir() / f"{chain_id}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, chain_id: str) -> dict[str, Any] | None:
        if not _CHAIN_ID.fullmatch(chain_id):
            return None
        path = self.root / f"{chain_id}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in self.root.glob("chain_*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue  # one corrupt report must not blank the list
        out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return out


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    execution = report.get("execution") or {}
    diagnosis = report.get("diagnosis") or {}
    return {
        "chain_id": report.get("chain_id"),
        "scenario_id": report.get("scenario_id"),
        "case_id": report.get("case_id"),
        "case_version": report.get("case_version"),
        "run_id": execution.get("run_id"),
        "test_verdict": execution.get("test_verdict"),
        "execution_status": execution.get("execution_status"),
        "evidence_count": execution.get("evidence_count", 0),
        "event_count": execution.get("event_count", 0),
        "diagnosis_status": diagnosis.get("diagnosis_status"),
        "disposition": report.get("disposition"),
        "completed": bool(report.get("completed")),
        "started_at": report.get("started_at"),
        "duration_s": report.get("duration_s"),
        "persisted": report.get("persisted"),
        "stages": [
            {"name": s.get("name"), "status": s.get("status"), "detail": s.get("detail")}
            for s in report.get("stages", [])
        ],
    }


# ------------------------------------------------------------------ models


class RunChainRequest(BaseModel):
    case_id: str
    scenario_id: str = ""
    workspace_id: str = "ws-default"


# ------------------------------------------------------------------ helpers


def _load_case(case_dir: Path, case_id: str) -> TestCaseVersion:
    cases = load_cases_dir(case_dir)
    case = next((c for c in cases if c.case_id == case_id), None)
    if case is None:
        raise NotFoundError(
            f"unknown case: {case_id}",
            details={"available": sorted(c.case_id for c in cases)},
        )
    if not case.approved_by:
        # The human-review gate: a candidate that never passed review must not
        # be executable through a product endpoint.
        raise ConflictError(
            "case is not human-approved",
            details={"case_id": case_id, "approved_by": case.approved_by},
        )
    return case


def _persist_events(request: Request, events: list[dict[str, Any]]) -> dict[str, int]:
    store = request.app.state.event_store
    written = 0
    skipped = 0
    for event in events:
        producer_instance = str(event.get("producer_instance_id") or "")
        producer_id = str(event.get("producer_id") or "local_runner")
        if not producer_instance:
            skipped += 1
            continue
        store.register_producer(producer_instance, producer_id)
        try:
            store.append(
                event_id=str(event["event_id"]),
                event_type=str(event["event_type"]),
                workspace_id=str(request.app.state.default_workspace_id),
                producer_id=producer_id,
                producer_instance_id=producer_instance,
                sequence=int(event["sequence"]),
                context_refs={
                    "run_id": event.get("run_id", ""),
                    "step_id": event.get("step_id") or "",
                    "correlation_id": event.get("correlation_id", ""),
                },
                payload=dict(event.get("payload") or {}),
                occurred_at=str(event.get("occurred_at") or ""),
                idempotency_key=str(event.get("idempotency_key") or ""),
            )
            written += 1
        except Exception:  # noqa: BLE001 - duplicate sequence is not a chain failure
            skipped += 1
    return {"written": written, "skipped": skipped}


def _blob_store(request: Request) -> BlobStore | None:
    """Created on first use, not at import.

    Building the app must not mkdir inside the product data directory: test
    sessions import this module and would otherwise leave artifacts behind.
    """
    store = getattr(request.app.state, "blob_store", None)
    if store is not None:
        return store
    root = getattr(request.app.state, "blob_dir", None)
    if root is None:
        return None
    from packages.evidence.storage import LocalBlobStore

    store = LocalBlobStore(Path(root))
    request.app.state.blob_store = store
    return store


def _persist_evidence(
    request: Request, evidence: list[dict[str, Any]], blob_store: BlobStore | None
) -> dict[str, int]:
    manager = request.app.state.evidence_manager
    written = 0
    skipped = 0
    for record in evidence:
        try:
            manager.import_record(record, blob_store=blob_store)
            written += 1
        except Exception:  # noqa: BLE001 - re-import of the same id is a no-op
            skipped += 1
    return {"written": written, "skipped": skipped}


def _register_run(
    request: Request, case: TestCaseVersion, report: Any, run_report: dict[str, Any]
) -> str:
    """Make the chain's run visible as an ordinary run (list/detail/diagnosis).

    A chain executed from the product must not be a side channel: without this
    the run id exists only inside a JSON file, so Run Details and the diagnosis
    endpoint cannot see it. The status machine is walked legally — created,
    prepared, running, finalizing, terminal — because runs are immutable once
    terminal and an illegal jump would be rejected.
    """
    orch = getattr(request.app.state, "orchestrator", None)
    run_id = str(report.execution.get("run_id") or "")
    if orch is None or not run_id:
        return ""
    from packages.execution.tasks import RunStatus

    task_id = f"task_{report.chain_id}"
    try:
        orch.tasks.create(task_id, case.case_id, f"chain: {case.title}")
    except Exception:  # noqa: BLE001 - task already exists from a prior chain
        pass
    try:
        orch.runs.create_run(
            run_id=run_id,
            task_id=task_id,
            case_id=case.case_id,
            case_version=case.version,
        )
        orch.runs.transition(run_id, RunStatus.PREPARING)
        orch.runs.transition(run_id, RunStatus.RUNNING)
        if report.steps:
            orch.runs.append_steps(run_id, report.steps)
        orch.runs.transition(run_id, RunStatus.FINALIZING)
        orch.runs.finalize(
            run_id,
            status=RunStatus.COMPLETED if report.completed else RunStatus.FAILED,
            execution_status=str(report.execution.get("execution_status") or "FAILED"),
            test_verdict=str(report.execution.get("test_verdict") or "INCONCLUSIVE"),
            report=run_report,
            error=str(report.execution.get("error") or "")[:500],
        )
    except Exception:  # noqa: BLE001 - never lose the chain because a row was rejected
        return ""
    return run_id


# ------------------------------------------------------------------ routes


@router.get("/chains")
def list_chains(request: Request) -> list[dict[str, Any]]:
    store: ChainStore = request.app.state.chain_store
    return [_summary(r) for r in store.list()]


@router.get("/chains/{chain_id}")
def get_chain(request: Request, chain_id: str) -> dict[str, Any]:
    store: ChainStore = request.app.state.chain_store
    report = store.get(chain_id)
    if report is None:
        raise NotFoundError(f"chain not found: {chain_id}")
    return report


@router.post("/chains", status_code=201)
def run_chain(request: Request, body: RunChainRequest) -> dict[str, Any]:
    """Execute one human-approved case through the whole chain.

    Declared sync on purpose: the Playwright sync API cannot run inside the
    event loop, and FastAPI executes sync handlers in a threadpool.
    """
    case_dir: Path = request.app.state.case_dir
    case = _load_case(case_dir, body.case_id)
    case.validate()

    scenario = body.scenario_id or case.case_id
    started_clock = datetime.now(UTC)
    chain = FullChain(
        base_url=request.app.state.teamflow_base_url,
        graph=request.app.state.correlation_graph,
        eval_token=request.app.state.eval_token,
        # Same registry the orchestrator uses: a chain must resolve exactly the
        # logical locators a workbench run would.
        locators=getattr(request.app.state.orchestrator, "locators", None),
    )
    report = chain.run(
        scenario,
        case,
        workspace_id=body.workspace_id,
        probe_requests=None,
        fault_events=None,
    )

    payload = report.to_dict()
    # The screenshot hex has already been moved to the blob store; keeping it in
    # the report would duplicate megabytes of base64 in an append-only file.
    for item in payload.get("evidence", []):
        item_payload = item.get("payload")
        if isinstance(item_payload, dict):
            item_payload.pop("image", None)

    # Persist before saving the report: a stored chain must never claim evidence
    # it did not actually write.
    persisted = {
        "events": _persist_events(request, report.events),
        "evidence": _persist_evidence(request, report.evidence, _blob_store(request)),
    }
    started_at = started_clock.isoformat()
    run_report: dict[str, Any] = {
        "run_id": report.execution.get("run_id", ""),
        "case_id": case.case_id,
        "case_version": case.version,
        "execution_status": report.execution.get("execution_status", ""),
        "test_verdict": report.execution.get("test_verdict", ""),
        "steps": report.steps,
        "observations": report.observations,
        "assertion_results": report.assertion_results,
        "evidence": payload.get("evidence", []),
        "events": report.events,
        "chain_id": report.chain_id,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "error": str(report.execution.get("error") or ""),
    }
    registered_run = _register_run(request, case, report, run_report)

    record: dict[str, Any] = {
        **payload,
        "case_id": case.case_id,
        "case_version": case.version,
        "approved_by": case.approved_by,
        "run_id": registered_run,
        "started_at": started_at,
        "duration_s": round((datetime.now(UTC) - started_clock).total_seconds(), 2),
        "persisted": persisted,
    }
    store: ChainStore = request.app.state.chain_store
    store.save(record)
    return _summary(record) | {"chain_id": record["chain_id"], "stages": record["stages"]}


def register_chain_api(
    app: Any,
    *,
    chain_dir: Path,
    blob_dir: Path,
    default_workspace_id: str = "ws-default",
) -> None:
    app.state.chain_store = ChainStore(chain_dir)
    app.state.default_workspace_id = default_workspace_id
    # Where content-addressed screenshots go; the store itself is built lazily.
    app.state.blob_dir = blob_dir
    app.include_router(router)
