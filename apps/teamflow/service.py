"""TeamFlow service layer — business logic and data lifecycle.

Design note: the business write path is the ONLY writer of Project rows.
An independent state query reads those rows directly from the DB, so it reflects
real persisted state and never depends on what the UI happened to display.
Fault hooks are invoked at precise points to simulate the five fault classes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from packages.core.errors import ConflictError, DomainError, NotFoundError, ValidationError

from . import faults
from .models import Project, TestUser, Workspace

WS_ID = "ws-default"
USERNAME_KEY = "TEAMFLOW_TEST_USER"


def seed_default(session: Session, test_user: str, test_password: str) -> None:
    """Idempotent seed: default workspace + test account. Safe to re-run."""
    if session.get(Workspace, WS_ID) is None:
        session.add(Workspace(id=WS_ID, name="Default Workspace"))
        # Flush so the parent row exists before inserting children that carry a
        # FK to it (TestUser has no ORM relationship, so ordering isn't inferred).
        session.flush()
    existing = (
        session.execute(
            select(TestUser).where(TestUser.workspace_id == WS_ID, TestUser.username == test_user)
        )
        .scalars()
        .first()
    )
    if existing is None:
        session.add(
            TestUser(
                id=uuid.uuid4().hex,
                workspace_id=WS_ID,
                username=test_user,
                password=test_password,
            )
        )
    session.commit()


def authenticate(session: Session, username: str, password: str) -> TestUser | None:
    return (
        session.execute(
            select(TestUser).where(TestUser.username == username, TestUser.password == password)
        )
        .scalars()
        .first()
    )


def reset_data(session: Session, test_user: str, test_password: str) -> dict[str, int]:
    """Repeatable cleanup: wipe projects/scenarios/GT/events, re-seed baseline.

    Fault configs are disabled but retained so their catalogue stays visible.
    Returns counts for evidence of the reset.
    """
    from .eval_models import EventLog, FaultConfig, GroundTruth, Scenario

    n_projects = session.execute(select(func.count()).select_from(Project)).scalar_one()
    session.execute(delete(Project))
    session.execute(delete(GroundTruth))
    session.execute(delete(Scenario))
    session.execute(delete(EventLog))
    for fc in session.execute(select(FaultConfig)).scalars().all():
        fc.enabled = False
    session.commit()
    seed_default(session, test_user, test_password)
    return {"projects_deleted": int(n_projects)}


def create_project(session: Session, name: str, description: str | None) -> Project:
    """Create with fault hooks. Order of hooks mirrors realistic failure surfaces."""
    name = name.strip()
    if not name:
        raise ValidationError("project name must not be empty")
    if len(name) > 200:
        raise ValidationError("project name must be at most 200 characters")
    # TF-API-001: API returns 500 before any write.
    api = faults.get_active_fault(session, faults.TF_API_001)
    if api:
        faults._log(session, faults.TF_API_001, "API_500", None)
        session.commit()
        raise faults.InjectedApiError(api)

    # TF-NET-001: latency before processing.
    faults.maybe_apply_network_delay(session)

    # TF-DB-001: simulate transaction failure — rollback and raise without write.
    # Note: an armed fault with empty params returns {} — falsy but still armed.
    # Must test `is not None`, never truthiness.
    db_fault = faults.get_active_fault(session, faults.TF_DB_001)
    if db_fault is not None:
        faults._log(session, faults.TF_DB_001, "TX_ROLLBACK", "injected before insert")
        session.commit()
        raise DomainError(
            "Injected persistence failure (TF-DB-001)",
            details={"fault_id": faults.TF_DB_001},
        )

    if (
        session.execute(select(Project).where(Project.workspace_id == WS_ID, Project.name == name))
        .scalars()
        .first()
    ):
        raise ConflictError("project name already exists", details={"name": name})

    project = Project(
        id=uuid.uuid4().hex,
        workspace_id=WS_ID,
        name=name,
        description=description,
        status="ACTIVE",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    faults._log(session, "TF", "PROJECT_CREATED", project.id)
    session.commit()
    return project


def list_projects(session: Session) -> list[Project]:
    return list(
        session.execute(
            select(Project).where(Project.workspace_id == WS_ID).order_by(Project.created_at)
        )
        .scalars()
        .all()
    )


def get_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.workspace_id != WS_ID:
        raise NotFoundError("project not found", details={"project_id": project_id})
    return project


def update_project(
    session: Session, project_id: str, name: str, description: str | None
) -> Project:
    name = name.strip()
    if not name:
        raise ValidationError("project name must not be empty")
    if len(name) > 200:
        raise ValidationError("project name must be at most 200 characters")
    project = get_project(session, project_id)
    duplicate = session.execute(
        select(Project).where(
            Project.workspace_id == project.workspace_id,
            Project.name == name,
            Project.id != project_id,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError("project name already exists", details={"name": name})
    project.name = name
    project.description = description
    session.commit()
    session.refresh(project)
    return project


def project_to_dict(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "workspace_id": project.workspace_id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }


def independent_state(session: Session) -> dict[str, Any]:
    """Independent business-state query — reads authoritative DB rows only.

    This is the Oracle-facing source of truth: it never renders UI text and
    never consults Ground Truth. What it returns is what actually persisted.
    """
    rows = list_projects(session)
    return {
        "source": "teamflow.db/tf_project",
        "workspace_id": WS_ID,
        "project_count": len(rows),
        "projects": [project_to_dict(p) for p in rows],
    }


def seed_scenario(
    session: Session,
    scenario_id: str,
    fault_id: str | None,
    description: str,
    metadata: dict[str, Any],
    ground_truth: dict[str, Any],
) -> None:
    from .eval_models import GroundTruth, Scenario

    if session.get(Scenario, scenario_id) is None:
        session.add(
            Scenario(
                scenario_id=scenario_id,
                fault_id=fault_id,
                description=description,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        )
    if session.get(GroundTruth, scenario_id) is None:
        session.add(
            GroundTruth(
                scenario_id=scenario_id,
                expected_json=json.dumps(ground_truth, ensure_ascii=False),
            )
        )
    session.commit()
