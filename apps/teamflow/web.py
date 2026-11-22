"""TeamFlow Web UI — minimal server-rendered pages for the '创建项目' path.

UI exists so Playwright has a real page to drive in stage 2. TF-UI-001 and
TF-TIME-001 are applied here (presentation layer only), never in the ORACLE
independent-state endpoint. That separation is the whole point: a UI can lie,
the authoritative state cannot.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import faults, service
from .api import get_db

router = APIRouter(tags=["teamflow-web"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_form(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),  # type: ignore[assignment]
):
    user = service.authenticate(db, username, password)
    if user is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": "用户名或密码错误"}, status_code=401
        )
    response = RedirectResponse("/projects", status_code=303)
    response.set_cookie("tf_session", user.username, httponly=True, samesite="lax")
    return response


@router.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("tf_session")
    return response


@router.get("/projects", response_class=HTMLResponse)
def projects_page(
    request: Request,
    db: Session = Depends(get_db),  # type: ignore[assignment]
):
    projects = [service.project_to_dict(p) for p in service.list_projects(db)]
    ctx = {
        "projects": projects,
        "hide_success": faults.ui_success_hidden(db),
        "refresh_delay_ms": faults.list_refresh_delayed(db),
    }
    return templates.TemplateResponse(request, "projects.html", ctx)


@router.get("/projects/new", response_class=HTMLResponse)
def new_project_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "new_project.html", {"error": None})


@router.post("/projects/new", response_class=HTMLResponse)
def create_project_form(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),  # type: ignore[assignment]
):
    from packages.core.errors import DomainError

    try:
        service.create_project(db, name, description or None)
    except DomainError as exc:
        return templates.TemplateResponse(
            request, "new_project.html", {"error": exc.message}, status_code=exc.status_code
        )
    return RedirectResponse("/projects?created=1", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),  # type: ignore[assignment]
):
    project = service.project_to_dict(service.get_project(db, project_id))
    return templates.TemplateResponse(request, "detail.html", {"project": project})
