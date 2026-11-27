"""Eval control plane — token-gated fault injection & Ground Truth access.

This router is mounted at /__eval__. Every endpoint requires X-Eval-Token.
The token is deliberately NOT exposed through the business plane, the Web UI,
or any browser-side asset. Agent/Playwright must never receive it — that is what
makes Ground Truth isolation structural rather than a convention.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.config import Settings
from packages.core.errors import DomainError

from . import faults, service
from .eval_models import EventLog, GroundTruth, Scenario
from .models import Project

router = APIRouter(prefix="/__eval__", tags=["eval-control"])

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_SEED_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _run_marker(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise HTTPException(status_code=422, detail="invalid run id")
    return f"traceguard-run:{run_id}"


def _belongs_to_run(description: str, marker: str) -> bool:
    return description == marker or description.startswith((f"{marker}:", f"{marker};"))


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def require_eval_token(
    x_eval_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),  # type: ignore[assignment]
) -> None:
    if x_eval_token != settings.eval_control_token:
        raise HTTPException(status_code=401, detail="invalid eval token")


@router.get("/ui-revision", dependencies=[Depends(require_eval_token)])
def ui_revision(request: Request) -> dict[str, str]:
    return {"revision": getattr(request.app.state, "ui_revision", "v1")}


@router.post("/ui-revision", dependencies=[Depends(require_eval_token)])
def set_ui_revision(body: dict[str, str], request: Request) -> dict[str, str]:
    revision = body.get("revision", "")
    if revision not in {"v1", "v2"}:
        raise HTTPException(status_code=422, detail="unsupported demo UI revision")
    request.app.state.ui_revision = revision
    # This changes presentation only; no Ground Truth or Oracle data is touched.
    return {"revision": revision}


@router.get("/faults", dependencies=[Depends(require_eval_token)])
def list_faults(db: Session = Depends(get_db)) -> list[dict[str, Any]]:  # type: ignore[assignment]
    return faults.list_faults(db)


@router.post("/faults/{fault_id}/enable", dependencies=[Depends(require_eval_token)])
def enable_fault(
    fault_id: str,
    params: dict[str, Any] | None = None,
    db: Session = Depends(get_db),  # type: ignore[assignment]
) -> dict[str, Any]:
    row = faults.set_fault(db, fault_id, True, params)
    return {"fault_id": row.fault_id, "enabled": True, "params": json.loads(row.params_json)}


@router.post("/faults/{fault_id}/disable", dependencies=[Depends(require_eval_token)])
def disable_fault(
    fault_id: str,
    db: Session = Depends(get_db),  # type: ignore[assignment]
) -> dict[str, Any]:
    row = faults.set_fault(db, fault_id, False, None)
    return {"fault_id": row.fault_id, "enabled": False}


@router.post("/faults/clear", dependencies=[Depends(require_eval_token)])
def clear_faults(db: Session = Depends(get_db)) -> dict[str, Any]:  # type: ignore[assignment]
    return {"cleared": faults.clear_all_faults(db)}


@router.post("/data/reset", dependencies=[Depends(require_eval_token)])
def reset_data(db: Session = Depends(get_db)) -> dict[str, Any]:  # type: ignore[assignment]
    settings = get_settings_from_db(db)
    return service.reset_data(db, settings.teamflow_test_user, settings.teamflow_test_password)


@router.post("/runs/{run_id}/projects", dependencies=[Depends(require_eval_token)])
def seed_run_project(
    run_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),  # type: ignore[assignment]
) -> dict[str, Any]:
    """Create an explicitly run-owned fixture; never accepts arbitrary IDs."""
    marker = _run_marker(run_id)
    key = str(payload.get("key") or "")
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "")
    if not _SEED_KEY.fullmatch(key):
        raise HTTPException(status_code=422, detail="invalid seed key")
    if not name or len(name) > 200 or not name.startswith(f"TG_RUN_{run_id}_"):
        raise HTTPException(status_code=422, detail="invalid run-owned project name")
    if len(description) > 4000:
        raise HTTPException(status_code=422, detail="project description is too long")
    owner = f"{marker}:{key}"
    existing = next(
        (
            row
            for row in db.execute(
                select(Project).where(Project.workspace_id == service.WS_ID)
            ).scalars()
            if (row.description or "").startswith(f"{owner};")
        ),
        None,
    )
    if existing is not None:
        if existing.name != name:
            raise HTTPException(status_code=409, detail="run seed identity conflict")
        return {"id": existing.id, "name": existing.name, "owner_marker": owner}
    project = Project(
        id=uuid4().hex,
        workspace_id=service.WS_ID,
        name=name,
        description=f"{owner}; {description}" if description else owner,
        status="ACTIVE",
    )
    db.add(project)
    db.commit()
    return {"id": project.id, "name": project.name, "owner_marker": owner}


@router.post("/runs/{run_id}/cleanup", dependencies=[Depends(require_eval_token)])
def cleanup_run_projects(
    run_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),  # type: ignore[assignment]
) -> dict[str, Any]:
    """Idempotently remove only rows carrying this run's name and owner marker."""
    marker = _run_marker(run_id)
    resource_ids = payload.get("resource_ids", [])
    if not isinstance(resource_ids, list) or len(resource_ids) > 500:
        raise HTTPException(status_code=422, detail="invalid resource id list")
    prefix = f"TG_RUN_{run_id}_"
    rows: dict[str, Project] = {}
    for raw_id in resource_ids:
        project_id = str(raw_id)
        project = db.get(Project, project_id)
        if project is None:
            continue
        description = project.description or ""
        if (
            project.workspace_id != service.WS_ID
            or not project.name.startswith(prefix)
            or not _belongs_to_run(description, marker)
        ):
            raise HTTPException(status_code=409, detail="run-owned project identity changed")
        rows[project.id] = project
    # Browser-created rows have no platform lease resource ID. They are still
    # eligible only when both independent ownership fields match this run.
    for project in db.execute(
        select(Project).where(Project.workspace_id == service.WS_ID)
    ).scalars():
        if project.name.startswith(prefix) and _belongs_to_run(project.description or "", marker):
            rows[project.id] = project
    removed = sorted(rows)
    for project in rows.values():
        db.delete(project)
    db.commit()
    return {"run_id": run_id, "deleted_ids": removed, "deleted_count": len(removed)}


def get_settings_from_db(_db: Session) -> Settings:
    # Eval routes run inside TeamFlow; settings is process-wide singleton.
    from packages.core.config import get_settings as _get

    return _get()


@router.get("/state", dependencies=[Depends(require_eval_token)])
def independent_state(db: Session = Depends(get_db)) -> dict[str, Any]:  # type: ignore[assignment]
    """Eval-side view of authoritative business state (for experiment checks)."""
    return service.independent_state(db)


@router.post("/scenarios/{scenario_id}", dependencies=[Depends(require_eval_token)])
def upsert_scenario(
    scenario_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),  # type: ignore[assignment]
) -> dict[str, Any]:
    service.seed_scenario(
        db,
        scenario_id=scenario_id,
        fault_id=payload.get("fault_id"),
        description=payload.get("description", ""),
        metadata=payload.get("metadata", {}),
        ground_truth=payload.get("ground_truth", {}),
    )
    return {"scenario_id": scenario_id, "saved": True}


@router.get("/scenarios", dependencies=[Depends(require_eval_token)])
def list_scenarios(db: Session = Depends(get_db)) -> list[dict[str, Any]]:  # type: ignore[assignment]
    rows = db.execute(select(Scenario)).scalars().all()
    return [
        {
            "scenario_id": r.scenario_id,
            "fault_id": r.fault_id,
            "description": r.description,
            "metadata": json.loads(r.metadata_json),
        }
        for r in rows
    ]


@router.get("/scenarios/{scenario_id}/ground-truth", dependencies=[Depends(require_eval_token)])
def ground_truth(
    scenario_id: str,
    db: Session = Depends(get_db),  # type: ignore[assignment]
) -> dict[str, Any]:
    row = db.get(GroundTruth, scenario_id)
    if row is None:
        raise DomainError("ground truth not found", details={"scenario_id": scenario_id})
    return {"scenario_id": scenario_id, "expected": json.loads(row.expected_json)}


@router.get("/events", dependencies=[Depends(require_eval_token)])
def events(db: Session = Depends(get_db)) -> list[dict[str, Any]]:  # type: ignore[assignment]
    rows = db.execute(select(EventLog).order_by(EventLog.id)).scalars().all()
    return [
        {
            "id": r.id,
            "scenario_id": r.scenario_id,
            "fault_id": r.fault_id,
            "outcome": r.outcome,
            "detail": r.detail,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        }
        for r in rows
    ]


@router.get("/fault-catalog", dependencies=[Depends(require_eval_token)])
def fault_catalog() -> list[dict[str, Any]]:
    return [
        {"fault_id": s.fault_id, "label": s.label, "default_params": s.default_params}
        for s in faults.FAULT_SPECS.values()
    ]
