"""TraceGuard platform main service.

Stage 0 scope: configuration, structured logging, unified error envelope,
health checks and DB connectivity. No business modules yet (per stage 0
"明确不做"). Business capabilities arrive in stages 2+.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from apps.api.candidate_api import register_candidates
from apps.api.chain_api import register_chain_api
from apps.api.evidence_api import register_evidence_api
from apps.api.explorer_api import register_explorer
from apps.api.extensions import register_extensions
from apps.api.workbench import register_workbench
from packages.core import database
from packages.core.config import Settings, get_settings
from packages.core.errors import DomainError
from packages.core.http import RequestIdMiddleware
from packages.core.logging import get_logger, setup_logging

logger = get_logger("tg.api")


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_json)

    engine = database.create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("platform_api_startup", extra={"extra_fields": {"env": settings.app_env.value}})
        yield
        engine.dispose()

    app = FastAPI(title="TraceGuard Platform API", version="0.1.0-m0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.state.settings = settings
    app.state.engine = engine

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_req: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness + DB connectivity. Reads DB with a trivial statement."""
        db_ok = True
        error: str | None = None
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - exercised only on DB outage
            db_ok = False
            error = type(exc).__name__
        return {
            "status": "ok" if db_ok else "degraded",
            "service": "platform-api",
            "env": settings.app_env.value,
            "db": {"ok": db_ok, "error": error},
        }

    @app.get("/version")
    def version() -> dict[str, str]:
        return {"product": "TraceGuard", "api": "0.2.0-m1", "baseline": "v1.1"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        """Absorb the legacy favicon probe.

        The SPA ships ``/favicon.svg``; some browsers still request
        ``/favicon.ico`` for HTML documents, and a 404 there pollutes the
        console (and the acceptance run). A 204 is the honest answer: there is
        no .ico, and nothing is broken.
        """
        return Response(status_code=204)

    register_evidence_api(app)
    register_explorer(app, exploration_dir=Path(settings.exploration_data_dir))
    register_candidates(app, candidate_dir=Path(settings.candidate_data_dir))

    if settings.workbench_enabled:
        register_workbench(
            app,
            session_factory=database.make_session_factory(engine),
            case_dir=Path(settings.case_dir),
            teamflow_base_url=settings.teamflow_base_url,
            eval_token=settings.eval_control_token,
            max_workers=settings.workbench_workers,
        )
        register_extensions(
            app,
            case_dir=Path(settings.case_dir),
            benchmark_data_dir=Path(settings.benchmark_data_dir),
        )
        # Needs the evidence plane + case dir + teamflow URL wired above.
        register_chain_api(
            app,
            chain_dir=Path(settings.chain_data_dir),
            blob_dir=Path(settings.blob_dir),
        )

    # Serve the built workbench SPA when it exists. Mounted last so every API
    # route wins; hash routing means the SPA never needs a server-side fallback.
    web_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    if (web_dist / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="workbench")

    return app


app = build_app()
