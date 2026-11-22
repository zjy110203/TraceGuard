"""Shared test fixtures — each test gets an isolated temp SQLite DB.

Using a fresh engine per test guarantees repeatability (stage 1 acceptance:
可重复初始化和清理) and prevents fault state leaking between tests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from apps.teamflow.eval_models import Base as EvalBase
from apps.teamflow.main import build_app
from apps.teamflow.models import Base as BusinessBase
from packages.core.config import Settings

EVAL_TOKEN = "test-eval-token"


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        eval_control_token=EVAL_TOKEN,
        teamflow_test_user="tester",
        teamflow_test_password="test-pass-123",
    )


@pytest.fixture()
def app(settings: Settings):
    application = build_app(settings)
    return application


@pytest.fixture()
def client(app) -> TestClient:  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        yield c  # type: ignore[misc]


@pytest.fixture()
def eval_headers() -> dict[str, str]:
    return {"X-Eval-Token": EVAL_TOKEN}


@pytest.fixture()
def engine(settings: Settings):
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    BusinessBase.metadata.create_all(eng)
    EvalBase.metadata.create_all(eng)
    yield eng
    eng.dispose()
