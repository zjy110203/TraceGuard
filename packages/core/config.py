"""Centralised, environment-driven configuration.

Design note (DISCIPLINE): no secret has a real default. Values marked as secrets
must be supplied via environment / .env; a missing secret in prod fails fast.
This implements v1.1 stage 0 task 4 (unified config) and task 9 (secrets not in repo).
"""

from __future__ import annotations

import functools
import os
from enum import StrEnum
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def ensure_env_file_vars(env_file: str = ".env") -> None:
    """Expose keys from ``.env`` to ``os.environ`` (without overriding).

    ``pydantic-settings`` reads ``.env`` for ``Settings`` only; ``os.environ``
    stays untouched, so any code that checks the environment directly (e.g.
    ``packages.agents.llm.provider_from_env``) would miss provider keys. This
    loader runs once, never overrides an explicitly set variable, and ignores
    malformed lines rather than guessing.
    """
    path = Path(env_file)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)


class AppEnv(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnv = AppEnv.DEV
    log_level: str = "INFO"
    log_json: bool = False

    # Database: SQLite by default so M0 runs with zero external services.
    # Switch to PostgreSQL via DATABASE_URL (see .env.example).
    database_url: str = "sqlite:///./data/traceguard.db"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    teamflow_host: str = "127.0.0.1"
    teamflow_port: int = 8001
    teamflow_test_user: str = "tester"
    teamflow_test_password: str = "test-pass-123"

    # Eval control plane token: must NOT be available to the Agent / business plane.
    eval_control_token: str = "local-dev-eval-token-change-me"

    # Stage-3 workbench: case library location and worker pool sizing.
    case_dir: str = "benchmark/cases"
    workbench_workers: int = 2
    workbench_enabled: bool = True

    # Benchmark persistence root (append-only JSON store). Tests point this at
    # a throwaway directory so "fresh install" assertions stay hermetic.
    benchmark_data_dir: str = "benchmark/data"

    @property
    def teamflow_base_url(self) -> str:
        return f"http://{self.teamflow_host}:{self.teamflow_port}"

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        return v.upper()

    @property
    def is_prod(self) -> bool:
        return self.app_env == AppEnv.PROD

    def validate_for_prod(self) -> None:
        """Fail fast if a production deployment keeps demo secrets."""
        if not self.is_prod:
            return
        if self.eval_control_token == "local-dev-eval-token-change-me":
            raise RuntimeError("EVAL_CONTROL_TOKEN must be set to a strong value in prod")
        if self.teamflow_test_password == "test-pass-123":
            raise RuntimeError("TEAMFLOW_TEST_PASSWORD must be changed in prod")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so tests can override via dependency."""
    s = Settings()  # type: ignore[call-arg]
    s.validate_for_prod()
    return s


def reset_settings_cache() -> None:
    """Testing helper: drop the cached settings instance."""
    get_settings.cache_clear()
