"""One-command dev launcher: TeamFlow (target app) + platform API + workbench SPA.

Why a script instead of two terminals: the workbench needs the platform API on 8000
and the target app on 8001 (``settings.teamflow_base_url``). Starting them by hand
invites the classic mistake of opening the SPA while TeamFlow is down, which makes
every submitted run fail in a way that looks like a product bug.

Usage:
    python -m scripts.serve              # 8000 + 8001
    python -m scripts.serve --port 8080  # platform on a different port
    python -m scripts.serve --no-teamflow  # only the API (browse-only)

Both servers run in threads inside this process; Ctrl+C stops both. No process is
detached and nothing is written outside the project directory.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import uvicorn

if TYPE_CHECKING:
    from packages.core.config import Settings

TEAMFLOW_APP = "apps.teamflow.main:app"
PLATFORM_APP = "apps.api.main:app"


def _port_free(host: str, port: int) -> bool:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, socktype, proto, _, address in addresses:
        with socket.socket(family, socktype, proto) as sock:
            try:
                sock.bind(address)
                return True
            except OSError:
                continue
    return False


def _host_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}"


def _resolve_hosts(cli_host: str | None, settings: Settings) -> tuple[str, str]:
    api_host = cli_host or settings.api_host
    teamflow_host = cli_host or settings.teamflow_host
    settings.validate_local_deployment(api_host=api_host, teamflow_host=teamflow_host)
    return api_host, teamflow_host


def _already_serving(url: str) -> bool:
    """True when the port answers /health with a TraceGuard service.

    Distinguishes 'already running, just open the browser' from 'some other
    program grabbed the port'. Found necessary the first time a user ran this
    script twice and saw only '[x] port in use'.
    """
    try:
        resp = httpx.get(f"{url}/health", timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200 or not resp.text.strip().startswith("{"):
        return False
    return resp.json().get("service") in {"platform-api", "teamflow"}


def _wait_health(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1.0, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def _start(
    app_path: str, host: str, port: int, name: str
) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app_path, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"{name}-{port}", daemon=True)
    thread.start()
    return server, thread


def main() -> int:
    # serve.py runs as a plain script (scripts/ on sys.path, not the project
    # root), so the packages import below needs the root added explicitly.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # Expose provider keys from .env (DEEPSEEK_API_KEY etc.) before anything
    # builds an LLM provider. Never overrides explicitly set variables.
    from packages.core.config import (
        authorize_local_startup,
        ensure_env_file_vars,
        get_settings,
        reset_settings_cache,
    )

    ensure_env_file_vars()
    from packages.agents.llm import validate_provider_config

    parser = argparse.ArgumentParser(description="Start TraceGuard locally")
    parser.add_argument(
        "--host",
        help="loopback bind override for both services (default: API_HOST / TEAMFLOW_HOST)",
    )
    parser.add_argument("--port", type=int, default=8000, help="platform API + workbench")
    parser.add_argument("--teamflow-port", type=int, default=8001)
    parser.add_argument("--no-teamflow", action="store_true", help="skip the target app")
    args = parser.parse_args()

    settings = get_settings()
    try:
        api_host, teamflow_host = _resolve_hosts(args.host, settings)
        authorize_local_startup(api_host=api_host, teamflow_host=teamflow_host, settings=settings)
    except RuntimeError as exc:
        print(f"[x] 本机部署边界拒绝启动：{exc}")
        return 1
    try:
        validate_provider_config()
    except RuntimeError as exc:
        print(f"[x] Provider 配置拒绝启动：{exc}")
        return 1
    # Both apps and the platform's TeamFlow client read these settings during
    # import. Keep their configured origins aligned with the validated binds.
    os.environ["API_HOST"] = api_host
    os.environ["TEAMFLOW_HOST"] = teamflow_host
    os.environ["API_PORT"] = str(args.port)
    os.environ["TEAMFLOW_PORT"] = str(args.teamflow_port)
    reset_settings_cache()

    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    if not args.no_teamflow:
        if not _port_free(teamflow_host, args.teamflow_port):
            teamflow_url = _host_url(teamflow_host, args.teamflow_port)
            if _already_serving(teamflow_url):
                print(f"[!] TeamFlow 已在运行：{teamflow_url}")
            else:
                print(f"[x] 端口 {args.teamflow_port} 被其他程序占用（TeamFlow 无法启动）")
                return 1

    api_url = _host_url(api_host, args.port)
    if not _port_free(api_host, args.port):
        if _already_serving(api_url):
            print()
            print("=" * 66)
            print(" TraceGuard 已经在运行，无需再次启动")
            print("=" * 66)
            print(f"  直接在浏览器打开：  {api_url}/")
            print("=" * 66)
            return 0
        print(f"[x] 端口 {args.port} 被其他程序占用（platform 无法启动）")
        return 1

    if not args.no_teamflow and not _port_free(teamflow_host, args.teamflow_port):
        print("[!] TeamFlow 未启动，只能浏览已有数据；提交 Run 会失败")

    if not args.no_teamflow and _port_free(teamflow_host, args.teamflow_port):
        servers.append(_start(TEAMFLOW_APP, teamflow_host, args.teamflow_port, "teamflow"))
    servers.append(_start(PLATFORM_APP, api_host, args.port, "platform"))

    base = api_url
    if not _wait_health(base):
        print("[x] platform API did not become healthy; see traceback above")
        return 1
    teamflow_ok = True
    if not args.no_teamflow:
        teamflow_url = _host_url(teamflow_host, args.teamflow_port)
        teamflow_ok = _wait_health(teamflow_url)

    print("=" * 66)
    print(" TraceGuard 已启动")
    print("=" * 66)
    print(f"  工作台(浏览器打开)  {base}/")
    print(f"  API 文档            {base}/docs")
    if not args.no_teamflow:
        state = "ok" if teamflow_ok else "未就绪（提交 Run 会失败）"
        print(f"  被测应用 TeamFlow   {_host_url(teamflow_host, args.teamflow_port)}/  [{state}]")
    else:
        print("  被测应用 TeamFlow   未启动（只能浏览已有数据）")
    print()
    print("  Ctrl+C 结束；不要关闭本窗口")
    print("=" * 66)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
        for server, _ in servers:
            server.should_exit = True
        for _, thread in servers:
            thread.join(timeout=10)
        return 0


if __name__ == "__main__":
    sys.exit(main())
