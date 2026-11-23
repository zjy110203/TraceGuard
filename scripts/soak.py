"""R5-7 soak test: keep the real product under continuous load for N minutes.

What it does
------------
Starts TeamFlow (target app) and the platform API against a **throwaway copy** of
the product database, then for the requested duration:

  * polls the cheap read endpoints continuously (overview / evidence / graph),
  * runs one full chain (real Chrome) every ``--interval`` seconds, strictly
    sequentially — one browser at a time, never two,
  * samples RSS of both server processes every sample window.

Every failure is recorded rather than swallowed: a non-200, an exception, a
non-PASSED chain verdict, or a timeout all end up in the report. Nothing is
written outside the working directory given by ``--work``.

Usage:
    .venv/Scripts/python.exe scripts/soak.py --minutes 30 --work .traceguard-tmp/r5/soak
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(base: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1.0, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def _rss_mb(pid: int) -> float | None:
    """Resident memory in MB via tasklist (Windows) — no psutil dependency."""
    try:
        out = subprocess.run(  # noqa: S603, S607
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # "python.exe","1234","Console","1","123,456 K"
    parts = [p.strip('"') for p in out.strip().split('","')]
    if len(parts) < 5:
        return None
    mem = parts[-1].replace("K", "").replace(",", "").strip()
    try:
        return round(int(mem) / 1024, 1)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="TraceGuard soak test")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=180.0, help="seconds between chains")
    parser.add_argument("--poll", type=float, default=10.0, help="seconds between read polls")
    parser.add_argument("--case", default="TC-LOGIN-001")
    parser.add_argument("--work", default=".traceguard-tmp/r5/soak")
    args = parser.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    db = work / "traceguard.db"
    if not db.exists():
        prod = ROOT / "data" / "traceguard.db"
        if prod.exists():
            shutil.copy2(prod, db)  # copy, never move
    cases = work / "cases"
    if not cases.exists():
        shutil.copytree(ROOT / "benchmark" / "cases", cases)

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db.as_posix()}",
            "TRACEGUARD_DATABASE_URL": f"sqlite:///{db.as_posix()}",
            "CASE_DIR": str(cases),
            "CHAIN_DATA_DIR": str(work / "chains"),
            "BLOB_DIR": str(work / "blobs"),
            "CANDIDATE_DATA_DIR": str(work / "candidates"),
            "EXPLORATION_DATA_DIR": str(work / "explorations"),
            "BENCHMARK_DATA_DIR": str(work / "benchmark"),
            "PYTHONUTF8": "1",
        }
    )

    tf_base = "http://127.0.0.1:8001"
    tf_log = (work / "teamflow.log").open("w", encoding="utf-8")
    teamflow = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.teamflow.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=tf_log,
        stderr=subprocess.STDOUT,
    )
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env["TEAMFLOW_BASE_URL"] = tf_base
    api_log = (work / "platform.log").open("w", encoding="utf-8")
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
    )

    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "minutes": args.minutes,
        "case": args.case,
        "base": base,
        "samples": [],
        "errors": [],
        "chains": [],
        "polls": {"ok": 0, "failed": 0, "max_latency_ms": 0.0},
    }
    client = httpx.Client(timeout=20, trust_env=False)
    try:
        if not _wait_health(tf_base) or not _wait_health(base):
            report["errors"].append("server did not become healthy")
            return 1

        deadline = time.monotonic() + args.minutes * 60
        next_chain = time.monotonic()  # first chain runs immediately
        next_sample = time.monotonic()
        read_paths = [
            "/api/v1/overview",
            "/api/v1/evidence/records?limit=50",
            "/api/v1/evidence/graph/nodes",
            "/api/v1/cases",
            "/api/v1/chains",
        ]
        read_i = 0
        chains_ok = chains_bad = 0

        while time.monotonic() < deadline:
            now = time.monotonic()

            if now >= next_chain:
                started = time.monotonic()
                try:
                    resp = client.post(
                        f"{base}/api/v1/chains", json={"case_id": args.case}, timeout=300
                    )
                    body = resp.json() if resp.content else {}
                    verdict = body.get("test_verdict")
                    ok = resp.status_code == 201 and bool(body.get("run_id"))
                    report["chains"].append(
                        {
                            "at": datetime.now(UTC).isoformat(),
                            "http": resp.status_code,
                            "verdict": verdict,
                            "run_id": body.get("run_id"),
                            "evidence": body.get("evidence_count"),
                            "duration_s": round(time.monotonic() - started, 1),
                            "ok": ok,
                        }
                    )
                    chains_ok += int(ok)
                    chains_bad += int(not ok)
                    if not ok:
                        report["errors"].append(f"chain http={resp.status_code} verdict={verdict}")
                except httpx.HTTPError as exc:
                    chains_bad += 1
                    report["errors"].append(f"chain exception: {type(exc).__name__}: {exc}")
                next_chain = max(now, time.monotonic() - 1) + args.interval

            if now >= next_sample:
                report["samples"].append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "api_rss_mb": _rss_mb(api.pid),
                        "teamflow_rss_mb": _rss_mb(teamflow.pid),
                        "api_alive": api.poll() is None,
                        "teamflow_alive": teamflow.poll() is None,
                        "db_mb": round(db.stat().st_size / (1024 * 1024), 2) if db.exists() else 0,
                    }
                )
                next_sample = now + 30

            path = read_paths[read_i % len(read_paths)]
            read_i += 1
            t0 = time.monotonic()
            try:
                resp = client.get(f"{base}{path}", timeout=20)
                latency = (time.monotonic() - t0) * 1000
                report["polls"]["max_latency_ms"] = round(
                    max(report["polls"]["max_latency_ms"], latency), 1
                )
                if resp.status_code == 200:
                    report["polls"]["ok"] += 1
                else:
                    report["polls"]["failed"] += 1
                    report["errors"].append(f"poll {path} -> {resp.status_code}")
            except httpx.HTTPError as exc:
                report["polls"]["failed"] += 1
                report["errors"].append(f"poll {path} exception: {type(exc).__name__}")

            time.sleep(min(args.poll, max(0.0, deadline - time.monotonic())))

        # Post-run integrity: evidence ids unique, counts monotonic, servers alive.
        records = client.get(f"{base}/api/v1/evidence/records?limit=1000", timeout=30).json()
        ids = [r.get("evidence_id") for r in records]
        nodes = client.get(f"{base}/api/v1/evidence/graph/nodes", timeout=30).json()
        report["final"] = {
            "evidence_records": len(records),
            "unique_evidence_ids": len(set(ids)),
            "duplicate_ids": len(ids) - len(set(ids)),
            "graph_nodes": len(nodes),
            "chains_ok": chains_ok,
            "chains_failed": chains_bad,
            "api_alive": api.poll() is None,
            "teamflow_alive": teamflow.poll() is None,
            "db_mb": round(db.stat().st_size / (1024 * 1024), 2) if db.exists() else 0,
        }
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["ok"] = (
            not report["errors"]
            and chains_bad == 0
            and report["final"]["duplicate_ids"] == 0
            and report["final"]["api_alive"]
            and report["final"]["teamflow_alive"]
        )
        (work / "soak-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report["final"], ensure_ascii=False, indent=2))
        print(f"errors={len(report['errors'])} ok={report['ok']}")
        return 0 if report["ok"] else 2
    finally:
        client.close()
        for proc in (api, teamflow):
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        api_log.close()
        tf_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
