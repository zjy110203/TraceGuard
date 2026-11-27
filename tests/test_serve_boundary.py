from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import scripts.serve as serve
from packages.core.config import Settings
from scripts.serve import _resolve_hosts, main


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]"])
def test_local_deployment_accepts_loopback_hosts(host: str) -> None:
    settings = Settings()
    settings.validate_local_deployment(api_host=host, teamflow_host=host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.4", "::", "example.test"])
def test_local_deployment_rejects_non_loopback_hosts(host: str) -> None:
    settings = Settings()
    with pytest.raises(RuntimeError, match="loopback"):
        settings.validate_local_deployment(api_host=host, teamflow_host="127.0.0.1")


def test_local_deployment_requires_one_worker() -> None:
    settings = Settings(workbench_workers=2)
    with pytest.raises(RuntimeError, match="WORKBENCH_WORKERS=1"):
        settings.validate_local_deployment(api_host="127.0.0.1", teamflow_host="::1")


def test_serve_uses_api_host_setting_and_cli_override() -> None:
    settings = Settings(api_host="localhost", teamflow_host="::1")
    assert _resolve_hosts(None, settings) == ("localhost", "::1")
    assert _resolve_hosts("127.0.0.1", settings) == ("127.0.0.1", "127.0.0.1")


def test_production_rejects_without_authentication() -> None:
    settings = Settings(
        app_env="prod",
        eval_control_token="strong-token",
        teamflow_test_password="strong-password",
    )
    with pytest.raises(RuntimeError, match="authentication is not implemented"):
        settings.validate_for_prod()


def test_platform_app_rejects_production_without_authentication() -> None:
    from apps.api.main import build_app

    settings = Settings(
        app_env="prod",
        eval_control_token="strong-token",
        teamflow_test_password="strong-password",
    )
    with pytest.raises(RuntimeError, match="authentication is not implemented"):
        build_app(settings)


@pytest.mark.parametrize("app_path", [serve.PLATFORM_APP, serve.TEAMFLOW_APP])
def test_direct_uvicorn_non_loopback_startup_is_rejected(app_path: str) -> None:
    env = os.environ.copy()
    env["APP_ENV"] = "dev"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            app_path,
            "--host",
            "0.0.0.0",
            "--port",
            "0",
            "--lifespan",
            "off",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode != 0
    assert "validated official scripts.serve" in result.stderr


def test_serve_refuses_non_loopback_before_binding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TEAMFLOW_DATABASE_URL", raising=False)
    settings = Settings()
    monkeypatch.setattr("packages.core.config.get_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["serve", "--host", "0.0.0.0", "--no-teamflow"])

    assert main() == 1
    assert "本机部署边界拒绝启动" in capsys.readouterr().out


def test_serve_starts_on_localhost_with_aligned_app_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAMFLOW_DATABASE_URL", raising=False)
    settings = Settings()
    starts: list[tuple[str, str, int, str]] = []
    fake_server = SimpleNamespace(should_exit=False)
    fake_thread = SimpleNamespace(join=lambda timeout: None)

    monkeypatch.setattr("packages.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("packages.core.config.reset_settings_cache", lambda: None)
    monkeypatch.setattr("packages.agents.llm.validate_provider_config", lambda: None)
    monkeypatch.setattr(sys, "argv", ["serve", "--host", "localhost", "--no-teamflow"])
    monkeypatch.setattr(serve, "_port_free", lambda host, port: True)
    monkeypatch.setattr(
        serve,
        "_start",
        lambda app, host, port, name: (
            starts.append((app, host, port, name)) or fake_server,
            fake_thread,
        ),
    )
    monkeypatch.setattr(serve, "_wait_health", lambda url: True)
    monkeypatch.setattr(serve.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt))

    assert main() == 0
    assert starts == [(serve.PLATFORM_APP, "localhost", 8000, "platform")]
    assert os.environ["API_HOST"] == "localhost"
    assert os.environ["TEAMFLOW_HOST"] == "localhost"
    assert os.environ["API_PORT"] == "8000"
    assert os.environ["TEAMFLOW_PORT"] == "8001"
    assert "TEAMFLOW_DATABASE_URL" not in os.environ
