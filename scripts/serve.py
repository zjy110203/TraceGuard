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
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

TEAMFLOW_APP = "apps.teamflow.main:app"
PLATFORM_APP = "apps.api.main:app"


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


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
    app_path: str, host: str, port: str, name: str
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
    from packages.core.config import ensure_env_file_vars

    ensure_env_file_vars()
    parser = argparse.ArgumentParser(description="Start TraceGuard locally")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="platform API + workbench")
    parser.add_argument("--teamflow-port", type=int, default=8001)
    parser.add_argument("--no-teamflow", action="store_true", help="skip the target app")
    args = parser.parse_args()

    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    if not args.no_teamflow:
        if not _port_free(args.host, args.teamflow_port):
            if _already_serving(f"http://{args.host}:{args.teamflow_port}"):
                print(f"[!] TeamFlow 已在运行：http://{args.host}:{args.teamflow_port}")
            else:
                print(f"[x] 端口 {args.teamflow_port} 被其他程序占用（TeamFlow 无法启动）")
                return 1

    if not _port_free(args.host, args.port):
        if _already_serving(f"http://{args.host}:{args.port}"):
            print()
            print("=" * 66)
            print(" TraceGuard 已经在运行，无需再次启动")
            print("=" * 66)
            print(f"  直接在浏览器打开：  http://{args.host}:{args.port}/")
            print("=" * 66)
            return 0
        print(f"[x] 端口 {args.port} 被其他程序占用（platform 无法启动）")
        return 1

    if not args.no_teamflow and not _port_free(args.host, args.teamflow_port):
        print("[!] TeamFlow 未启动，只能浏览已有数据；提交 Run 会失败")

    if not args.no_teamflow and _port_free(args.host, args.teamflow_port):
        servers.append(_start(TEAMFLOW_APP, args.host, args.teamflow_port, "teamflow"))
    servers.append(_start(PLATFORM_APP, args.host, args.port, "platform"))

    base = f"http://{args.host}:{args.port}"
    if not _wait_health(base):
        print("[x] platform API did not become healthy; see traceback above")
        return 1
    teamflow_ok = True
    if not args.no_teamflow:
        teamflow_ok = _wait_health(f"http://{args.host}:{args.teamflow_port}")

    print("=" * 66)
    print(" TraceGuard 已启动")
    print("=" * 66)
    print(f"  工作台(浏览器打开)  {base}/")
    print(f"  API 文档            {base}/docs")
    if not args.no_teamflow:
        state = "ok" if teamflow_ok else "未就绪（提交 Run 会失败）"
        print(f"  被测应用 TeamFlow   http://{args.host}:{args.teamflow_port}/  [{state}]")
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
