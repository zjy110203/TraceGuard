"""R5-7 data restore verification — on a temporary copy only.

A backup is worthless until it has been restored and served from. This script:

  1. copies the backup to a throwaway path (the backup itself is never opened
     for writing, and ``data/`` is untouched),
  2. runs ``alembic upgrade head`` against the copy,
  3. runs ``PRAGMA integrity_check`` and counts the tables the product needs,
  4. boots the real platform API against the restored copy and checks that
     health, overview and the case library answer correctly,
  5. writes a JSON verdict.

Usage:
    .venv/Scripts/python.exe scripts/restore_check.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TABLES = (
    "tg_task",
    "tg_run",
    "tg_step",
    "ev_event",
    "evd_record",
    "gr_node",
    "gr_edge",
    "tf_project",
)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a TraceGuard backup restores")
    parser.add_argument("--backup", default="data/traceguard.db.bak-20261122")
    parser.add_argument("--work", default=".traceguard-tmp/r5/restore")
    args = parser.parse_args()

    backup = ROOT / args.backup
    if not backup.exists():
        print(f"[x] backup not found: {backup}")
        return 1

    work = ROOT / args.work
    work.mkdir(parents=True, exist_ok=True)
    restored = work / f"restored-{datetime.now(UTC):%Y%m%d%H%M%S}.db"
    shutil.copy2(backup, restored)  # copy: the backup file is never modified

    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "backup": str(backup.relative_to(ROOT)),
        "backup_bytes": backup.stat().st_size,
        "restored": str(restored.relative_to(ROOT)),
        "steps": [],
    }

    def step(name: str, ok: bool, detail: str) -> None:
        report["steps"].append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")

    env = os.environ.copy()
    env["TRACEGUARD_DATABASE_URL"] = f"sqlite:///{restored.as_posix()}"
    env["DATABASE_URL"] = f"sqlite:///{restored.as_posix()}"
    env["PYTHONUTF8"] = "1"

    def alembic(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *argv],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    # 1. migrate the copy to head.
    #
    # A backup taken from a running instance can contain every table while
    # carrying no alembic_version row, because the product also creates tables
    # via SQLAlchemy metadata at startup. Replaying the initial migration then
    # fails with "table ... already exists". The documented recovery is to
    # stamp the base revision (declaring the existing schema as the starting
    # point) and upgrade from there — and then PROVE the result by checking the
    # column the later migration adds and by serving requests from it below.
    proc = alembic("upgrade", "head")
    detail = " ".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-2:])
    if proc.returncode != 0 and "already exists" in ((proc.stderr or "") + (proc.stdout or "")):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        base = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_base()
        stamp = alembic("stamp", base)
        step(
            "alembic-stamp-base",
            stamp.returncode == 0,
            f"base={base} exit={stamp.returncode} (pre-existing schema without migration state)",
        )
        proc = alembic("upgrade", "head")
        detail = " ".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-2:])
    step(
        "alembic-upgrade-head",
        proc.returncode == 0,
        f"exit={proc.returncode} {detail[:200]}",
    )

    # 2. integrity + required tables + row counts
    con = sqlite3.connect(f"file:{restored.as_posix()}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        step("integrity-check", integrity == "ok", integrity)
        present = {
            t
            for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = [t for t in REQUIRED_TABLES if t not in present]
        counts = {
            t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            for t in REQUIRED_TABLES
            if t in present
        }
        report["row_counts"] = counts
        step("required-tables", not missing, f"missing={missing or 'none'} counts={counts}")
        rev = con.execute("SELECT version_num FROM alembic_version").fetchone()
        step("alembic-version-recorded", bool(rev), f"version_num={rev[0] if rev else None}")
        cols = {row[1] for row in con.execute("PRAGMA table_info(evd_record)").fetchall()}
        step(
            "schema-has-attachment-ref",
            "attachment_ref" in cols,
            f"evd_record columns={len(cols)} attachment_ref={'attachment_ref' in cols}",
        )
    finally:
        con.close()

    # 3. serve the restored copy and read from it
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env.update(
        {
            "TEAMFLOW_BASE_URL": "http://127.0.0.1:8001",
            "CHAIN_DATA_DIR": str(work / "chains"),
            "BLOB_DIR": str(work / "blobs"),
            "CANDIDATE_DATA_DIR": str(work / "candidates"),
            "EXPLORATION_DATA_DIR": str(work / "explorations"),
            "BENCHMARK_DATA_DIR": str(work / "benchmark"),
        }
    )
    log = (work / "api.log").open("w", encoding="utf-8")
    api = subprocess.Popen(  # noqa: S603
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
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        healthy = _wait_health(base)
        step("api-healthy-on-restored-db", healthy, base if healthy else "no /health 200")
        if healthy:
            overview = httpx.get(f"{base}/api/v1/overview", timeout=20, trust_env=False)
            step(
                "overview-answers",
                overview.status_code == 200,
                f"http={overview.status_code} keys={sorted(overview.json())[:6]}"
                if overview.status_code == 200
                else f"http={overview.status_code}",
            )
            cases = httpx.get(f"{base}/api/v1/cases", timeout=20, trust_env=False)
            case_count = len(cases.json()) if cases.status_code == 200 else -1
            step(
                "case-library-readable",
                cases.status_code == 200 and case_count >= 0,
                f"http={cases.status_code} cases={case_count}",
            )
            tasks = httpx.get(f"{base}/api/v1/tasks", timeout=20, trust_env=False)
            task_count = len(tasks.json()) if tasks.status_code == 200 else -1
            step(
                "task-queue-readable",
                tasks.status_code == 200,
                f"http={tasks.status_code} tasks={task_count}",
            )
    finally:
        api.terminate()
        try:
            api.wait(timeout=20)
        except subprocess.TimeoutExpired:
            api.kill()
        log.close()

    report["finished_at"] = datetime.now(UTC).isoformat()
    report["ok"] = all(s["ok"] for s in report["steps"])
    (work / "restore-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nRESULT: {'PASS' if report['ok'] else 'FAIL'} -> {work / 'restore-report.json'}")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
