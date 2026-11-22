"""Tests for traceguard core config / logging / database base."""

from __future__ import annotations

from packages.core.config import Settings
from packages.core.database import create_db_engine
from packages.core.errors import NotFoundError


def test_settings_defaults_to_sqlite() -> None:
    s = Settings()
    assert s.database_url.startswith("sqlite")
    assert s.app_env.value in {"dev", "test", "prod"}


def test_prod_rejects_demo_secrets() -> None:
    s = Settings(app_env="prod", eval_control_token="local-dev-eval-token-change-me")
    import pytest

    with pytest.raises(RuntimeError, match="EVAL_CONTROL_TOKEN"):
        s.validate_for_prod()


def test_error_envelope_shape() -> None:
    err = NotFoundError("missing", details={"id": "x"})
    env = err.to_envelope()
    assert env["error"]["code"] == "NOT_FOUND"
    assert env["error"]["message"] == "missing"
    assert env["error"]["details"]["id"] == "x"


def test_create_sqlite_engine_creates_parent_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db_file = tmp_path / "nested" / "t.db"
    eng = create_db_engine(f"sqlite:///{db_file}")
    assert db_file.parent.exists()
    eng.dispose()
