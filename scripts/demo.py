"""One-command demo of the TraceGuard minimal closed loop.

Starts TeamFlow in-process, drives the real system Chrome through three
scenarios, and prints a human-readable view of what the loop observed and
concluded. No Agent, no manual terminal juggling.

Usage (from repo root, no shell env vars needed):
    # PowerShell:
    .\\.venv\\Scripts\\python.exe -m scripts.demo
    # Git Bash / Linux:
    .venv/Scripts/python.exe -m scripts.demo

The three scenarios are the interesting part: a healthy run, a UI that lies
(TF-UI-001), and a DB fault (TF-DB-001). They show that the loop records
divergence without corrupting the verdict.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from packages.execution.models import _version_from_dict
from packages.execution.runner import LocalRunner

# Keep the demo output readable: silence chatty HTTP INFO logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("packages.execution.runner").setLevel(logging.WARNING)

EVAL_H = {"X-Eval-Token": "local-dev-eval-token-change-me"}
CASE_FILE = Path("benchmark/cases/TC_E2E_001.json")
REPORT_DIR = Path("data/runs")

# Never trust HTTP(S)_PROXY env vars: WSL/VS Code proxies can hijack 127.0.0.1,
# which makes local health checks fail even though the server is healthy.


def _pick_free_port() -> int:
    """Let the OS pick a free port — avoids TIME_WAIT / occupation of a fixed port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_server(port: int) -> tuple[uvicorn.Server, threading.Thread, str]:
    base = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            if httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200:
                return server, thread, base
        except httpx.HTTPError:
            time.sleep(0.2)
    server.should_exit = True
    thread.join(timeout=5)
    raise RuntimeError(
        f"TeamFlow did not become healthy on {base}. "
        "Check that http_proxy/HTTP_PROXY does not intercept 127.0.0.1."
    )


def _load_case():
    case = _version_from_dict(json.loads(CASE_FILE.read_text(encoding="utf-8")))
    case.validate()
    return case


def _reset(client: httpx.Client, fault: str | None) -> None:
    client.post("/__eval__/faults/clear", headers=EVAL_H)
    client.post("/__eval__/data/reset", headers=EVAL_H)
    if fault:
        client.post(f"/__eval__/faults/{fault}/enable", headers=EVAL_H)


def main() -> int:
    print("=" * 68)
    print(" TraceGuard 最小闭环演示  (真 Chrome 驱动真 TeamFlow)")
    print("=" * 68)
    server, thread, base = _start_server(_pick_free_port())
    scenario_rows: list[tuple[str, str, str, str, str]] = []
    try:
        case = _load_case()
        runner = LocalRunner(base, report_dir=REPORT_DIR)
        scenarios: list[tuple[str, str | None]] = [
            ("正常路径", None),
            ("UI 撒谎 TF-UI-001", "TF-UI-001"),
            ("DB 故障 TF-DB-001", "TF-DB-001"),
        ]
        for name, fault in scenarios:
            with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
                _reset(client, fault)
            report = runner.run(case)
            steps_ok = sum(1 for s in report.steps if s.ok)
            count = report.observations.get("independent_state", {}).get("project_count", "?")
            failed_steps = [s.step_id for s in report.steps if not s.ok]
            ui = "全部通过" if not failed_steps else ",".join(failed_steps) + " 失败"
            print()
            print(f"[{name}]")
            print(f"  执行状态 : {report.execution_status.value}")
            print(f"  测试结论 : {report.test_verdict}")
            print(f"  步骤     : {steps_ok}/{len(report.steps)} 通过   (UI 断言: {ui})")
            print(f"  独立真值 : independent_state.project_count = {count}")
            print(f"  留证     : {len(report.evidence)} 条证据 / {len(report.events)} 个事件")
            print(f"  结论依据 : {report.verdict_notes[0] if report.verdict_notes else '-'}")
            print(f"  报告     : {REPORT_DIR}/{report.run_id}.json")
            scenario_rows.append(
                (name, report.test_verdict, ui, str(count), str(len(report.evidence)))
            )

        print()
        print("-" * 68)
        print(" 三场景对比 —— 这就是 TraceGuard 要捕捉的东西")
        print("-" * 68)
        print(f"  {'场景':<20}{'Verdict':<12}{'UI 断言':<16}{'DB真值':<8}{'证据'}")
        for name, verdict, ui, count, ev in scenario_rows:
            print(f"  {name:<20}{verdict:<12}{ui:<16}{count:<8}{ev}")
        print()
        print(" 解读: UI 撒谎时 Verdict 仍为 PASSED (项目确实建成了)，")
        print("       但 s9 的 UI 失败被如实留下 —— 这个分歧正是阶段 7 诊断器的输入。")
        print("-" * 68)
    finally:
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            client.post("/__eval__/faults/clear", headers=EVAL_H)
        server.should_exit = True
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
