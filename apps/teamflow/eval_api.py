"""Eval control plane — token-gated fault injection & Ground Truth access.

This router is mounted at /__eval__. Every endpoint requires X-Eval-Token.
The token is deliberately NOT exposed through the business plane, the Web UI,
or any browser-side asset. Agent/Playwright must never receive it — that is what
makes Ground Truth isolation structural rather than a convention.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.config import Settings
from packages.core.errors import DomainError

from . import faults, service
from .eval_models import EventLog, GroundTruth, Scenario

router = APIRouter(prefix="/__eval__", tags=["eval-control"])


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
