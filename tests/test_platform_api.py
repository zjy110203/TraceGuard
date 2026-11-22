"""Stage 0 acceptance — platform API starts, health check and DB connectivity."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import build_app
from packages.core.config import Settings


def test_platform_api_health() -> None:
    settings = Settings(app_env="test", database_url="sqlite:///:memory:")
    app = build_app(settings)
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["db"]["ok"] is True
        assert body["service"] == "platform-api"


def test_platform_version() -> None:
    settings = Settings(app_env="test", database_url="sqlite:///:memory:")
    with TestClient(build_app(settings)) as c:
        assert c.get("/version").json()["baseline"] == "v1.1"
