"""TeamFlow system-under-test application entry point.

Wires: DB + seed -> business API (/api/v1) -> Web UI (/) -> eval control
(/__eval__, token-gated). This is the runnable M1 deliverable.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from packages.core import database
from packages.core.config import Settings, get_settings
from packages.core.errors import DomainError
from packages.core.http import RequestIdMiddleware
from packages.core.logging import get_logger, setup_logging

from . import api, eval_api, service, web
from .eval_models import Base as EvalBase
from .models import Base as BusinessBase

logger = get_logger("tg.teamflow")


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_json)

    engine = database.create_db_engine(settings.database_url)
    session_factory = database.make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Create all tables (M0 skeleton; alembic migration added in task 2).
        BusinessBase.metadata.create_all(engine)
        EvalBase.metadata.create_all(engine)
        with session_factory() as session:
            service.seed_default(
                session, settings.teamflow_test_user, settings.teamflow_test_password
            )
        logger.info("teamflow_startup", extra={"extra_fields": {"env": settings.app_env.value}})
        yield
        engine.dispose()

    app = FastAPI(title="TeamFlow (SUT)", version="0.1.0-m1", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    @app.exception_handler(DomainError)
    async def _domain_error(_req: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.include_router(api.router)
    app.include_router(web.router)
    app.include_router(eval_api.router)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "service": "teamflow", "baseline": "v1.1-stage1"}

    return app


app = build_app()
