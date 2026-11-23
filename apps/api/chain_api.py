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
from packages.core.persistence import OutcomeCounter, PersistenceOutcome, classify_error
from packages.evidence.storage import BlobStore
from packages.execution.models import TestCaseVersion, load_cases_dir
from packages.execution.pipeline import ChainStage

router = APIRouter(prefix="/api/v1", tags=["chain"])

_CHAIN_ID = re.compile(r"chain_[A-Za-z0-9_-]+")


# ----------------------------------------------------------------- storage


class ChainStore:
    """Append-only JSON store, one report per chain."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.corrupt_files: list[str] = []

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
                if str(path) not in self.corrupt_files:
                    self.corrupt_files.append(str(path))
                continue  # one corrupt report must not blank the list
        out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return out


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    execution = report.get("execution") or {}
    diagnosis = report.get("diagnosis") or {}
    persisted = report.get("persisted") or {}
    return {
        "chain_id": report.get("chain_id"),
        "scenario_id": report.get("scenario_id"),
        "case_id": report.get("case_id"),
        "case_version": report.get("case_version"),
        "run_id": execution.get("run_id") or report.get("run_id"),
        "run_status": report.get("run_status"),
        "workspace_id": report.get("workspace_id"),
        "test_verdict": execution.get("test_verdict"),
        "execution_status": execution.get("execution_status"),
        "blocked": bool(execution.get("blocked")),
        "blocked_reason": execution.get("blocked_reason", ""),
        "evidence_count": execution.get("evidence_count", 0),
        "event_count": execution.get("event_count", 0),
        "diagnosis_status": diagnosis.get("diagnosis_status"),
        "disposition": report.get("disposition"),
        "completed": bool(report.get("completed")),
        "cancelled": bool(report.get("cancelled")),
        "cleanup": report.get("cleanup") or {},
        # Non-duplicate graph write failures (R5-4.5): an empty list is the only
        # proof the Graph page can actually show this run.
        "graph_errors": report.get("graph_errors") or [],
        "started_at": report.get("started_at"),
        "duration_s": report.get("duration_s"),
        "persisted": persisted,
        "persistence_status": persisted.get("status", ""),
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


def _persist_events(
    request: Request, events: list[dict[str, Any]], workspace_id: str
) -> dict[str, Any]:
    """Persist events, classifying every outcome (R5-4.4 §6.3).

    Previously *any* exception counted as a duplicate, so a genuine storage
    failure was indistinguishable from normal de-duplication.
    """
    store = request.app.state.event_store
    counter = OutcomeCounter()
    for event in events:
        event_id = str(event.get("event_id") or "")
        producer_instance = str(event.get("producer_instance_id") or "")
        producer_id = str(event.get("producer_id") or "local_runner")
        if not producer_instance:
            counter.add(PersistenceOutcome.FAILED, event_id, "missing producer_instance_id")
            continue
        store.register_producer(producer_instance, producer_id)
        try:
            store.append(
                event_id=event_id,
                event_type=str(event["event_type"]),
                workspace_id=workspace_id,
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
            counter.add(PersistenceOutcome.WRITTEN, event_id)
        except Exception as exc:  # noqa: BLE001 - classified, never hidden
            counter.add(classify_error(exc), event_id, f"{type(exc).__name__}: {exc}")
    return counter.summary()


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
) -> dict[str, Any]:
    manager = request.app.state.evidence_manager
    counter = OutcomeCounter()
    for record in evidence:
        evidence_id = str(record.get("evidence_id") or "")
        try:
            manager.import_record(record, blob_store=blob_store)
            counter.add(PersistenceOutcome.WRITTEN, evidence_id)
        except Exception as exc:  # noqa: BLE001 - classified, never hidden
            counter.add(classify_error(exc), evidence_id, f"{type(exc).__name__}: {exc}")
    return counter.summary()


def _strip_screenshot_bytes(evidence: list[dict[str, Any]]) -> None:
    """Drop the base64 payloads after the blob store has the real bytes.

    The image is already content-addressed on disk; keeping a hex copy inside
    the append-only run report duplicated megabytes per step.
    """
    for item in evidence:
        payload = item.get("payload")
        if isinstance(payload, dict):
            payload.pop("image", None)


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
    event loop, and FastAPI executes sync handlers in a threadpool. The Run row
    exists before execution starts (R5-4.2), so another request can watch it on
    SSE or cancel it while this call is still in flight.
    """
    case_dir: Path = request.app.state.case_dir
    case = _load_case(case_dir, body.case_id)
    case.validate()

    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise ConflictError(
            "full-chain execution requires the workbench orchestrator",
            details={"workbench_enabled": False},
        )

    scenario = body.scenario_id or case.case_id
    workspace_id = body.workspace_id or request.app.state.default_workspace_id
    started_clock = datetime.now(UTC)

    def persist(report: Any) -> dict[str, Any]:
        """Persist the chain's artifacts while the Run is FINALIZING."""
        blob = _blob_store(request)
        events = _persist_events(request, report.events, workspace_id)
        evidence = _persist_evidence(request, report.evidence, blob)
        # Strip the bytes only after the blob store actually has them.
        _strip_screenshot_bytes(report.evidence)
        stages = [
            {
                "name": "event_persistence",
                "status": events["status"],
                "detail": f"written={events['written']} duplicates={events['duplicates']} "
                f"failures={len(events['failures'])}",
            },
            {
                "name": "evidence_persistence",
                "status": evidence["status"],
                "detail": f"written={evidence['written']} duplicates={evidence['duplicates']} "
                f"failures={len(evidence['failures'])}",
            },
        ]
        for entry in stages:
            report.stages.append(ChainStage(entry["name"], entry["status"], entry["detail"]))
        status = (
            "PARTIAL"
            if "PARTIAL" in (events["status"], evidence["status"])
            else ("FAILED" if "FAILED" in (events["status"], evidence["status"]) else "OK")
        )
        return {
            "events": events,
            "evidence": evidence,
            "status": status,
            "stages": stages,
        }

    run_view, report = orch.run_chain(
        case,
        workspace_id=workspace_id,
        scenario_id=scenario,
        after_run=persist,
    )

    payload = report.to_dict()
    started_at = run_view.created_at or started_clock.isoformat()
    record: dict[str, Any] = {
        **payload,
        "case_id": case.case_id,
        "case_version": case.version,
        "approved_by": case.approved_by,
        "run_id": run_view.run_id,
        "run_status": run_view.status,
        "workspace_id": workspace_id,
        "started_at": started_at,
        "duration_s": round((datetime.now(UTC) - started_clock).total_seconds(), 2),
        "persisted": report.persisted,
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
