"""Stage-4 Event/Evidence query + Viewer endpoints (draft contract).

Read-oriented workbench API: query events/evidence by run, verify integrity,
inspect sequence gaps, and view the audit log. Append operations are performed
by the orchestrator/runner through the infrastructure layer, not by untrusted
clients. ``workspace_id`` is always supplied by trusted server context.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from packages.evidence.events import EventStore, detect_sequence_gaps
from packages.evidence.manager import EvidenceManager

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])


def _store(request: Request) -> EventStore:
    return request.app.state.event_store  # type: ignore[no-any-return]


def _manager(request: Request) -> EvidenceManager:
    return request.app.state.evidence_manager  # type: ignore[no-any-return]


class VerifyBody(BaseModel):
    # base64 of the exact bytes is not assumed; hex-encoded bytes are accepted
    # for the draft viewer so integrity can be checked without binary upload.
    data_hex: str


@router.get("/events")
def events(
    request: Request,
    run_id: str | None = None,
    event_type: str | None = None,
    workspace_id: str = "ws-default",
    limit: int = Query(200, le=1000),
) -> list[dict[str, Any]]:
    return _store(request).query(
        run_id=run_id, event_type=event_type, workspace_id=workspace_id, limit=limit
    )


@router.get("/events/gaps")
def gaps(request: Request, workspace_id: str = "ws-default") -> list[dict[str, Any]]:
    all_events = _store(request).query(workspace_id=workspace_id, limit=1000)
    return [g.to_dict() for g in detect_sequence_gaps(all_events)]


@router.get("/events/audit")
def audit(request: Request) -> list[dict[str, Any]]:
    return _store(request).audit_log()


@router.get("/records")
def records(
    request: Request,
    run_id: str | None = None,
    layer: str | None = None,
    limit: int = Query(200, le=1000),
) -> list[dict[str, Any]]:
    return _manager(request).query(run_id=run_id, layer=layer, limit=limit)


@router.get("/records/{evidence_id}")
def record(evidence_id: str, request: Request) -> dict[str, Any]:
    data = _manager(request).get(evidence_id)
    if data is None:
        from packages.core.errors import NotFoundError

        raise NotFoundError(f"unknown evidence: {evidence_id}")
    return data


@router.post("/records/{evidence_id}/verify")
def verify(evidence_id: str, body: VerifyBody, request: Request) -> dict[str, Any]:
    try:
        data = bytes.fromhex(body.data_hex)
    except ValueError as exc:
        from packages.core.errors import ValidationError

        raise ValidationError("data_hex is not valid hexadecimal") from exc
    return _manager(request).verify(evidence_id, data).to_dict()


@router.get("/records/{evidence_id}/sources")
def sources(evidence_id: str, request: Request) -> list[str]:
    return _manager(request).derived_sources(evidence_id)


@router.get("/graph/nodes")
def graph_nodes(request: Request, node_type: str | None = None) -> list[dict[str, Any]]:
    return request.app.state.correlation_graph.nodes(node_type=node_type)  # type: ignore[no-any-return]


@router.get("/graph/edges")
def graph_edges(request: Request, relation: str | None = None) -> list[dict[str, Any]]:
    return request.app.state.correlation_graph.edges(relation=relation)  # type: ignore[no-any-return]


@router.get("/graph/subgraph/{root_id}")
def graph_subgraph(root_id: str, request: Request, depth: int = 2) -> dict[str, Any]:
    return request.app.state.correlation_graph.subgraph(root_id, depth=depth)  # type: ignore[no-any-return]


@router.get("/graph/conflicts/{hypothesis_id}")
def graph_conflicts(hypothesis_id: str, request: Request) -> dict[str, Any]:
    return request.app.state.correlation_graph.conflicts(hypothesis_id)  # type: ignore[no-any-return]


def register_evidence_api(app: Any) -> None:
    """Wire EventStore + EvidenceManager into an app with its own tables."""
    from sqlalchemy.orm import sessionmaker

    from packages.evidence.events import EventStore, create_event_tables
    from packages.evidence.graph import CorrelationGraph, create_graph_tables
    from packages.evidence.manager import (
        EvidenceBase,
        EvidenceManager,
    )

    engine = app.state.engine
    create_event_tables(engine)
    create_graph_tables(engine)
    EvidenceBase.metadata.create_all(engine, checkfirst=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.state.event_store = EventStore(factory)
    app.state.evidence_manager = EvidenceManager(factory)
    app.state.correlation_graph = CorrelationGraph(factory)
    app.include_router(router)
