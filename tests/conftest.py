"""Shared test fixtures — each test gets an isolated temp SQLite DB.

Using a fresh engine per test guarantees repeatability (stage 1 acceptance:
可重复初始化和清理) and prevents fault state leaking between tests.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.engine import Engine

    from packages.core.config import Settings

# Runs before any test module is imported. ``apps.api.main`` builds a
# module-level app on import, and that app creates the task tables on whatever
# DATABASE_URL points at — so a plain ``pytest`` run (without verify_all's
# isolated URL) used to write to the FORMAL ``data/traceguard.db``. That is a
# data-isolation breach found by the R5-4.9 audit; the suite now points at a
# throwaway DB. An explicit DATABASE_URL (verify_all, CI) still wins.
_SUITE_DB_DIR = Path(tempfile.mkdtemp(prefix="traceguard-pytest-"))
_SUITE_DB_URL = f"sqlite:///{(_SUITE_DB_DIR / 'suite.db').as_posix()}"
os.environ.setdefault("DATABASE_URL", _SUITE_DB_URL)
os.environ.setdefault("TRACEGUARD_DATABASE_URL", _SUITE_DB_URL)

EVAL_TOKEN = "test-eval-token"


@pytest.fixture(scope="session", autouse=True)
def _drop_suite_db() -> Iterator[None]:
    """Remove the throwaway suite DB directory once the session ends."""
    yield
    shutil.rmtree(_SUITE_DB_DIR, ignore_errors=True)


@pytest.fixture()
def settings() -> Settings:
    from packages.core.config import Settings

    return Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        eval_control_token=EVAL_TOKEN,
        teamflow_test_user="tester",
        teamflow_test_password="test-pass-123",
    )


@pytest.fixture()
def app(settings: Settings) -> FastAPI:
    from apps.teamflow.main import build_app

    return build_app(settings)


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def eval_headers() -> dict[str, str]:
    return {"X-Eval-Token": EVAL_TOKEN}


@pytest.fixture()
def engine(settings: Settings) -> Engine:
    from sqlalchemy import create_engine

    from apps.teamflow.eval_models import Base as EvalBase
    from apps.teamflow.models import Base as BusinessBase

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    BusinessBase.metadata.create_all(eng)
    EvalBase.metadata.create_all(eng)
    yield eng
    eng.dispose()
