"""Centralised, environment-driven configuration.

Design note (DISCIPLINE): no secret has a real default. Values marked as secrets
must be supplied via environment / .env; a missing secret in prod fails fast.
This implements v1.1 stage 0 task 4 (unified config) and task 9 (secrets not in repo).
"""

from __future__ import annotations

import functools
import ipaddress
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


def is_loopback_host(host: str) -> bool:
    """Accept only localhost and literal loopback addresses for local mode."""
    value = host.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


# Capability is process-local, so an environment variable or arbitrary Uvicorn
# option cannot authorize the application. Only the validated official launcher
# can issue it. Test settings are explicitly isolated and do not grant this
# capability to development or production configuration.
_local_startup_authorized = False


def authorize_local_startup(*, api_host: str, teamflow_host: str, settings: Settings) -> None:
    settings.validate_for_prod()
    settings.validate_local_deployment(api_host=api_host, teamflow_host=teamflow_host)
    global _local_startup_authorized
    _local_startup_authorized = True


def require_authorized_startup(settings: Settings) -> None:
    if settings.app_env == AppEnv.TEST:
        return
    if not _local_startup_authorized:
        raise RuntimeError(
            "services must be started by the validated official scripts.serve entrypoint"
        )


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

    # Used by scripts.serve; kept configurable only within the loopback boundary.
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    teamflow_host: str = "127.0.0.1"
    teamflow_port: int = 8001
    teamflow_test_user: str = "tester"
    teamflow_test_password: str = "test-pass-123"

    llm_provider: str = ""
    llm_endpoint: str = ""
    llm_model: str = ""

    # Eval control plane token: must NOT be available to the Agent / business plane.
    eval_control_token: str = "local-dev-eval-token-change-me"

    # Stage-3 workbench: case library location and worker pool sizing.
    case_dir: str = "benchmark/cases"
    workbench_workers: int = 1
    workbench_enabled: bool = True

    data_lease_heartbeat_seconds: int = 20
    data_lease_timeout_seconds: int = 60

    # Benchmark persistence root (append-only JSON store). Tests point this at
    # a throwaway directory so "fresh install" assertions stay hermetic.
    benchmark_data_dir: str = "benchmark/data"

    # Explorer traces (append-only JSON, one file per exploration). Same
    # isolation contract as benchmark_data_dir: tests point it at a temp dir.
    exploration_data_dir: str = "data/explorations"

    # Full-chain reports (TestCase -> Run -> Oracle -> Evidence -> Diagnosis).
    # One JSON file per chain so a chain can be inspected without re-running it.
    chain_data_dir: str = "data/chains"

    # Content-addressed root for captured binary evidence (screenshots). Keeps
    # multi-hundred-KB hex blobs out of the JSON evidence column while still
    # preserving the exact bytes.
    blob_dir: str = "data/blobs"

    # Compiled candidates awaiting human review. Empty means "nothing pending",
    # and only an explicit approval writes into case_dir.
    candidate_data_dir: str = "data/candidates"

    @property
    def teamflow_base_url(self) -> str:
        host = self.teamflow_host
        if ":" in host and not (host.startswith("[") and host.endswith("]")):
            host = f"[{host}]"
        return f"http://{host}:{self.teamflow_port}"

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
        raise RuntimeError(
            "production startup rejected: platform API authentication is not implemented"
        )

    def validate_local_deployment(self, *, api_host: str, teamflow_host: str) -> None:
        """Enforce the current loopback-only, single-worker deployment boundary."""
        for name, host in (("API_HOST", api_host), ("TEAMFLOW_HOST", teamflow_host)):
            if not is_loopback_host(host):
                raise RuntimeError(f"{name} must be localhost or a loopback address")
        if self.workbench_workers != 1:
            raise RuntimeError("local deployment requires WORKBENCH_WORKERS=1")
        if not 1 <= self.data_lease_heartbeat_seconds <= 20:
            raise RuntimeError("DATA_LEASE_HEARTBEAT_SECONDS must be between 1 and 20")
        if not self.data_lease_heartbeat_seconds < self.data_lease_timeout_seconds <= 60:
            raise RuntimeError("DATA_LEASE_TIMEOUT_SECONDS must be > heartbeat and <= 60")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so tests can override via dependency."""
    s = Settings()  # type: ignore[call-arg]
    s.validate_for_prod()
    return s


def reset_settings_cache() -> None:
    """Testing helper: drop the cached settings instance."""
    get_settings.cache_clear()
