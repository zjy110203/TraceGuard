"""One cross-platform verification entry point for TraceGuard.

Why a Python runner instead of a shell script: the project must verify the same
way on Windows PowerShell and on a Linux CI runner. This module owns process
launching, exit-code capture, timing and machine-readable logs, so no gate can
silently pass because a pipe swallowed a failure.

Modes
-----
``quick``      ruff + format + mypy + vue-tsc            (seconds, pre-commit)
``python``     ruff + format + mypy + full pytest        (the Python gate)
``frontend``   npm consistency + vue-tsc + vite build    (the web gate)
``security``   the focused security/regression test set  (R5-7 subset)
``browser``    real Chrome acceptance against a live API (5 viewports)
``all``        every gate above, in order

Usage::

    .venv/Scripts/python.exe scripts/verify_all.py --mode quick
    .venv/Scripts/python.exe scripts/verify_all.py --mode all

Every run writes ``.traceguard-tmp/r5/verify/<stamp>/`` with ``report.json``,
``report.md`` and one log file per step. A non-zero exit means at least one
gate failed — the report names it; nothing is skipped to fake success.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
R5 = ROOT / ".traceguard-tmp" / "r5"

PY = sys.executable  # intended to be .venv/Scripts/python.exe

# mypy scope matches README (packages apps benchmark); CI is aligned to this.
MYPY_ARGV = [PY, "-m", "mypy", "packages", "apps", "benchmark", "--ignore-missing-imports"]


def _is_windows() -> bool:
    return os.name == "nt"


def _npm() -> str:
    """Resolve the npm executable, preferring the .cmd shim on Windows."""
    for name in ("npm.cmd", "npm") if _is_windows() else ("npm",):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("npm not found on PATH")


def _node_bin(tool: str) -> str:
    """Absolute path to a locally installed CLI (never a network ``npx`` fetch)."""
    base = WEB / "node_modules" / ".bin" / tool
    for candidate in (base.with_suffix(".cmd"), base) if _is_windows() else (base,):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"{tool} not installed under apps/web/node_modules (run npm ci)")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rel(path: Path) -> str:
    """Project-relative path for reports; tolerate paths outside the repo."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


@dataclass
class Step:
    name: str
    argv: list[str]
    cwd: Path = ROOT
    env: dict[str, str] = field(default_factory=dict)
    # Regex used to pull a "passed" count out of the command output.
    count_re: str | None = None


@dataclass
class StepResult:
    name: str
    command: str
    exit_code: int
    duration_s: float
    started: str
    finished: str
    passed: int | None
    log: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _run(step: Step, out_dir: Path, extra_env: dict[str, str]) -> StepResult:
    env = os.environ.copy()
    env.update(extra_env)
    env.update(step.env)
    started = datetime.now()
    clock = time.monotonic()
    printable = " ".join([Path(step.argv[0]).name, *step.argv[1:]])
    print(f"\n$ ({step.cwd.name}) {printable}", flush=True)
    proc = subprocess.run(  # noqa: S603 - argv is internal, no shell
        step.argv,
        cwd=str(step.cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    duration = time.monotonic() - clock
    finished = datetime.now()
    log_text = (proc.stdout or "") + (proc.stderr or "")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", step.name).strip("-").lower()
    log_file = out_dir / f"{slug}.log"
    log_file.write_text(
        f"# {printable}\nexit={proc.returncode} duration={duration:.1f}s\n\n{log_text}",
        encoding="utf-8",
    )
    passed = None
    if step.count_re:
        matches = re.findall(step.count_re, log_text)
        if matches:
            try:
                passed = int(matches[-1])
            except ValueError:
                passed = None
    status = "PASS" if proc.returncode == 0 else "FAIL"
    print(f"  [{status}] exit={proc.returncode} {duration:.1f}s", flush=True)
    if proc.returncode != 0:
        tail = log_text.strip().splitlines()[-15:]
        for line in tail:
            print(f"    {line}", flush=True)
    return StepResult(
        name=step.name,
        command=printable,
        exit_code=proc.returncode,
        duration_s=round(duration, 1),
        started=started.isoformat(timespec="seconds"),
        finished=finished.isoformat(timespec="seconds"),
        passed=passed,
        log=_rel(log_file),
    )


def _pytest(argv: list[str]) -> Step:
    return Step(
        name="pytest",
        argv=[PY, "-m", "pytest", *argv],
        count_re=r"(\d+) passed",
    )


def python_steps(full: bool = True) -> list[Step]:
    steps = [
        Step("ruff-check", [PY, "-m", "ruff", "check", "."]),
        Step("ruff-format-check", [PY, "-m", "ruff", "format", "--check", "."]),
        Step("mypy", MYPY_ARGV),
    ]
    if full:
        steps.append(_pytest(["-q"]))
    return steps


def frontend_steps() -> list[Step]:
    return [
        Step("npm-ls", [_npm(), "ls", "--depth=0"], cwd=WEB),
        Step("vue-tsc", [_node_bin("vue-tsc"), "--noEmit"], cwd=WEB),
        Step("vite-build", [_npm(), "run", "build"], cwd=WEB),
    ]


def security_steps() -> list[Step]:
    return [
        _pytest(
            [
                "-q",
                "tests/test_security_r4.py",
                "tests/test_sandbox_paths.py",
                "tests/test_repair_sandbox.py",
            ]
        )
    ]


def browser_steps() -> list[Step]:
    """Browser acceptance is handled specially (needs a live server)."""
    return []


# --------------------------------------------------------------------------- #
# Browser acceptance: start an isolated API, run the acceptance script, stop it.
# --------------------------------------------------------------------------- #


def run_browser(out_dir: Path, extra_env: dict[str, str]) -> StepResult:
    import httpx

    work = R5 / "browser"
    work.mkdir(parents=True, exist_ok=True)
    # Optional data seed: point acceptance at an already-produced dataset (a
    # throwaway chain/e2e run) so pages that need real rows — evidence, graph,
    # diagnosis, candidates — are exercised with data instead of only empty
    # states. Read-only: everything is copied into the seed dir chosen here.
    seed_root = os.environ.get("TRACEGUARD_BROWSER_SEED", "").strip()
    seed = Path(seed_root) if seed_root else None
    # A seeded run gets its own DB copy: reusing the plain one would silently
    # keep the unseeded schema/data from a previous run.
    db = work / (f"seed-{seed.name}.db" if seed is not None else "traceguard.db")
    prod = ROOT / "data" / "traceguard.db"
    if seed is not None:
        seed_db = seed / "traceguard.db"
        if not db.exists() and seed_db.exists():
            shutil.copy2(seed_db, db)
            print(f"  [i] seeded browser DB from {seed_db} (copy)", flush=True)
    if not db.exists() and prod.exists():
        # Copy, never move: the production DB stays byte-identical. The browser
        # run reads realistic data but only ever writes to this throwaway copy.
        shutil.copy2(prod, db)
        print(f"  [i] seeded isolated browser DB from {prod.name} (copy)", flush=True)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(extra_env)
    env["DATABASE_URL"] = f"sqlite:///{(db).as_posix()}"
    env["TRACEGUARD_DATABASE_URL"] = f"sqlite:///{(db).as_posix()}"
    env["TEAMFLOW_BASE_URL"] = "http://127.0.0.1:8001"
    # Keep file-backed stores out of the product data dir during verification.
    env["EXPLORATION_DATA_DIR"] = str(work / "explorations")
    env["BENCHMARK_DATA_DIR"] = str(work / "benchmark")
    env["CHAIN_DATA_DIR"] = str(work / "chains")
    env["BLOB_DIR"] = str(work / "blobs")
    env["CANDIDATE_DATA_DIR"] = str(work / "candidates")
    if seed is not None:
        # A seeded dataset carries its own case library and review queue;
        # mixing them with the product library would show candidates whose
        # formal case file does not exist.
        if (seed / "cases").is_dir():
            env["CASE_DIR"] = str(seed / "cases")
        for key, sub in (
            ("EXPLORATION_DATA_DIR", "explorations"),
            ("BENCHMARK_DATA_DIR", "benchmark"),
            ("CHAIN_DATA_DIR", "chains"),
            ("BLOB_DIR", "blobs"),
            ("CANDIDATE_DATA_DIR", "candidates"),
        ):
            if (seed / sub).is_dir():
                env[key] = str(seed / sub)

    started = datetime.now()
    clock = time.monotonic()
    log_path = out_dir / "browser.log"
    server_log = work / "server.log"
    server = subprocess.Popen(  # noqa: S603
        [
            PY,
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
        stdout=server_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    try:
        healthy = False
        for _ in range(80):
            try:
                if httpx.get(f"{base}/health", timeout=1.0, trust_env=False).status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        if not healthy:
            duration = time.monotonic() - clock
            log_path.write_text("platform API did not become healthy\n", encoding="utf-8")
            print("  [FAIL] API did not become healthy", flush=True)
            return StepResult(
                name="browser",
                command="ui_acceptance.py (API startup failed)",
                exit_code=1,
                duration_s=round(duration, 1),
                started=started.isoformat(timespec="seconds"),
                finished=datetime.now().isoformat(timespec="seconds"),
                passed=None,
                log=_rel(log_path),
            )

        run_env = env | {
            "TRACEGUARD_BASE": base,
            "TRACEGUARD_E2E_SUMMARY": str(R5 / "e2e-summary.json"),
        }
        print(f"\n$ ui_acceptance.py @ {base}", flush=True)
        proc = subprocess.run(  # noqa: S603
            [PY, str(ROOT / "scripts" / "ui_acceptance.py")],
            cwd=str(ROOT),
            env=run_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        duration = time.monotonic() - clock
        text = (proc.stdout or "") + (proc.stderr or "")
        log_path.write_text(text, encoding="utf-8")
        failures = None
        m = re.search(r"failures=(\d+)", text)
        if m:
            failures = int(m.group(1))
        total = None
        mt = re.search(r"total=(\d+)", text)
        if mt:
            total = int(mt.group(1))
        ok = proc.returncode == 0
        label = "PASS" if ok else "FAIL"
        print(f"  [{label}] browser exit={proc.returncode} {duration:.1f}s", flush=True)
        if not ok:
            for line in text.strip().splitlines()[-15:]:
                print(f"    {line}", flush=True)
        return StepResult(
            name="browser",
            command=f"ui_acceptance.py @ {base}",
            exit_code=proc.returncode,
            duration_s=round(duration, 1),
            started=started.isoformat(timespec="seconds"),
            finished=datetime.now().isoformat(timespec="seconds"),
            passed=(total - failures) if (total is not None and failures is not None) else None,
            log=_rel(log_path),
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

MODE_STEPS: dict[str, list[Step]] = {
    "quick": [
        Step("ruff-check", [PY, "-m", "ruff", "check", "."]),
        Step("ruff-format-check", [PY, "-m", "ruff", "format", "--check", "."]),
        Step("mypy", MYPY_ARGV),
        Step("vue-tsc", [_node_bin("vue-tsc"), "--noEmit"], cwd=WEB),
    ],
    "python": python_steps(full=True),
    "frontend": frontend_steps(),
    "security": security_steps(),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="TraceGuard verification runner")
    parser.add_argument(
        "--mode",
        default="quick",
        choices=["quick", "python", "frontend", "security", "browser", "all"],
    )
    parser.add_argument(
        "--keep-going", action="store_true", help="run later gates even if one fails"
    )
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = R5 / "verify" / f"{datetime.now():%Y%m%d}-{args.mode}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Isolated pytest basetemp + DB for every gate that runs tests.
    #
    # The DB name is stamped per run and never reused: deleting the previous
    # file goes through the host's safe-delete guard, which can abort (trash
    # failures surface as OSError) and take the whole gate down before any test
    # even starts. A fresh name has the same isolation guarantee and no delete.
    isolated_db = out_dir / f"isolated-{stamp}.db"
    extra_env = {
        # Settings reads DATABASE_URL; alembic env.py reads TRACEGUARD_DATABASE_URL.
        # Both point at the throwaway file so no gate can write to data/traceguard.db.
        "DATABASE_URL": f"sqlite:///{isolated_db.as_posix()}",
        "TRACEGUARD_DATABASE_URL": f"sqlite:///{isolated_db.as_posix()}",
        "PYTHONUTF8": "1",
    }

    modes = ["python", "frontend", "security", "browser"] if args.mode == "all" else [args.mode]
    steps: list[tuple[str, Step | None]] = []
    for mode in modes:
        if mode == "browser":
            steps.append(("browser", None))
        else:
            for step in MODE_STEPS[mode]:
                if step.name == "pytest":
                    # One basetemp per run. Reusing a shared path makes pytest
                    # prune old numbered dirs, which can hit host-level
                    # safe-delete guards and fail the gate for the wrong reason.
                    step.argv = [*step.argv, f"--basetemp={R5 / 'verify' / f'pytest-tmp-{stamp}'}"]
                steps.append((mode, step))

    print("=" * 70)
    print(f" TraceGuard verify — mode={args.mode} @ {stamp}")
    print(f" python={PY}")
    print(f" output={_rel(out_dir)}")
    print("=" * 70)

    results: list[StepResult] = []
    for _mode, step in steps:
        if step is None:
            results.append(run_browser(out_dir, extra_env))
        else:
            results.append(_run(step, out_dir, extra_env))
        if not results[-1].ok and not args.keep_going:
            name = results[-1].name
            print(f"\n[!] gate '{name}' failed — stopping (use --keep-going to continue)")
            break

    failed = [r for r in results if not r.ok]
    report = {
        "mode": args.mode,
        "stamp": stamp,
        "python": PY,
        "steps": [r.__dict__ for r in results],
        "failed": [r.name for r in failed],
        "ok": not failed,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [f"# Verify report — mode={args.mode} @ {stamp}", ""]
    lines.append("| step | exit | duration | passed | log |")
    lines.append("|------|------|----------|--------|-----|")
    for r in results:
        passed = r.passed if r.passed is not None else "-"
        lines.append(f"| {r.name} | {r.exit_code} | {r.duration_s}s | {passed} | `{r.log}` |")
    lines.append("")
    verdict = "PASS" if not failed else "FAIL"
    suffix = f" — failed: {', '.join(r.name for r in failed)}" if failed else ""
    lines.append(f"**Result: {verdict}{suffix}**")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 70)
    for r in results:
        print(f"  {'PASS' if r.ok else 'FAIL':4} {r.name:22} exit={r.exit_code:<3} {r.duration_s}s")
    print("=" * 70)
    print(f" report: {_rel(out_dir)}\\report.md")
    if failed:
        print(f" RESULT: FAIL ({', '.join(r.name for r in failed)})")
        return 1
    print(" RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
