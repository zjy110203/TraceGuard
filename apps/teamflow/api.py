"""TeamFlow business-plane API — the surfaces Agent/Playwright interact with.

IMPORTANT: no endpoint here may return fault config or Ground Truth. Only the
independent state query reads authoritative DB rows. Login is minimal demo auth
(documented as non-production); stage 1 explicitly allows minimal auth.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from packages.core.errors import UnauthorizedError

from . import service
from .models import Project

router = APIRouter(prefix="/api/v1", tags=["teamflow-business"])


def get_db(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def current_user(
    tf_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),  # type: ignore[assignment]
) -> str:
    # Demo auth: session cookie stores the raw username. Production must use
    # signed tokens + password hashing (documented, out of stage-1 scope).
    if not tf_session:
        raise UnauthorizedError("not logged in")
    return tf_session


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    ok: bool
    username: str
    workspace_id: str


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    status: str
    created_at: str | None


@router.post("/auth/login")
def login(
    payload: LoginRequest, response: Response, db: Session = Depends(get_db)
) -> LoginResponse:  # type: ignore[assignment]
    user = service.authenticate(db, payload.username, payload.password)
    if user is None:
        raise UnauthorizedError("invalid credentials")
    response.set_cookie("tf_session", user.username, httponly=True, samesite="lax")
    return LoginResponse(ok=True, username=user.username, workspace_id=user.workspace_id)


@router.post("/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("tf_session")
    return {"ok": True}


@router.get("/auth/me")
def me(user: str = Depends(current_user)) -> dict[str, str]:  # type: ignore[assignment]
    return {"username": user, "workspace_id": service.WS_ID}


@router.post("/projects", response_model=ProjectResponse, status_code=201)
def create_project(
    payload: CreateProjectRequest,
    db: Session = Depends(get_db),  # type: ignore[assignment]
    _user: str = Depends(current_user),  # type: ignore[assignment]
) -> dict[str, Any]:
    project: Project = service.create_project(db, payload.name, payload.description)
    return service.project_to_dict(project)


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(
    db: Session = Depends(get_db),  # type: ignore[assignment]
    _user: str = Depends(current_user),  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    # TF-UI/TIME faults affect presentation, not the authoritative API list.
    # TIME fault: 列表刷新延迟 is applied at the UI layer, not here, so the API
    # stays a reliable independent source.
    return [service.project_to_dict(p) for p in service.list_projects(db)]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    db: Session = Depends(get_db),  # type: ignore[assignment]
    _user: str = Depends(current_user),  # type: ignore[assignment]
) -> dict[str, Any]:
    return service.project_to_dict(service.get_project(db, project_id))


@router.put("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    payload: CreateProjectRequest,
    db: Session = Depends(get_db),  # type: ignore[assignment]
    _user: str = Depends(current_user),  # type: ignore[assignment]
) -> dict[str, Any]:
    return service.project_to_dict(
        service.update_project(db, project_id, payload.name, payload.description)
    )


@router.get("/state/independent")
def independent_state(
    db: Session = Depends(get_db),  # type: ignore[assignment]
    _user: str = Depends(current_user),  # type: ignore[assignment]
) -> dict[str, Any]:
    """Independent business state query — reads tf_project directly.

    This endpoint is the Oracle-facing source of truth available to the business
    plane. It never returns Ground Truth or fault configuration.
    """
    return service.independent_state(db)
