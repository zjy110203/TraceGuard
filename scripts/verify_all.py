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
``product``    real product E2E: isolated SUT+API, real browser exploration,
               compile -> review -> chain -> evidence -> graph -> diagnosis
``browser``    real Chrome acceptance against a live API (5 viewports)
``all``        every gate above, in order

Usage::

    .venv/Scripts/python.exe scripts/verify_all.py --mode quick
    .venv/Scripts/python.exe scripts/verify_all.py --mode all

Isolation contract (R5-4.5 §7)
------------------------------
* every run writes to its **own** output directory
  ``.traceguard-tmp/r5/verify/<stamp>-<mode>-<pid>/``;
* every run uses a **fresh** platform database and a **fresh** browser database
  — nothing is copied from ``data/`` and nothing survives into the next run;
* ``--browser-seed <dir>`` is the only way to point the browser gate at already
  produced rows, and it is used by ``--mode all`` to feed the browser sweep with
  the dataset the product E2E just created in the same run;
* each check is recorded individually (``report.json`` keeps every assertion),
  and an empty-data skip is counted separately from a real pass.

A non-zero exit means at least one gate failed — the report names it; nothing is
skipped to fake success.
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

MODES = ("quick", "python", "frontend", "security", "product", "browser", "all")


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
    # Regex used to pull a "skipped" count out of the command output.
    skip_re: str | None = None
    timeout_s: float | None = None


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
    skipped: int = 0
    # Every individual assertion this gate made, when it reports them.
    checks: list[dict[str, str]] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        if self.exit_code != 0:
            return False
        # A gate where every test was skipped verified nothing. pytest exits 0
        # in that case, and calling it PASS is exactly the dishonesty the
        # acceptance rules forbid (SKIP is reported, never counted as PASS).
        return not (self.passed == 0 and self.skipped > 0)

    @property
    def status(self) -> str:
        if self.exit_code != 0:
            return "FAIL"
        if self.passed == 0 and self.skipped > 0:
            return "SKIP"
        if self.passed == 0:
            return "EMPTY"
        return "PASS"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_s": self.duration_s,
            "started": self.started,
            "finished": self.finished,
            "passed": self.passed,
            "skipped": self.skipped,
            "ok": self.ok,
            "status": self.status,
            "log": self.log,
            "note": self.note,
            "checks": self.checks,
        }


def _counts(step: Step, text: str) -> tuple[int | None, int]:
    passed = None
    if step.count_re:
        matches = re.findall(step.count_re, text)
        if matches:
            try:
                passed = int(matches[-1])
            except ValueError:
                passed = None
    skipped = 0
    if step.skip_re:
        matches = re.findall(step.skip_re, text)
        if matches:
            try:
                skipped = int(matches[-1])
            except ValueError:
                skipped = 0
    return passed, skipped


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
        timeout=step.timeout_s,
    )
    duration = time.monotonic() - clock
    finished = datetime.now()
    log_text = (proc.stdout or "") + (proc.stderr or "")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", step.name).strip("-").lower()
    log_file = out_dir / f"{slug}.log"
    # Two steps can share a name (e.g. the full suite and the security subset are
    # both "pytest"). Silently overwriting the first log destroys the only copy
    # of a failure, which is exactly what happened before this guard existed.
    suffix = 2
    while log_file.exists():
        log_file = out_dir / f"{slug}-{suffix}.log"
        suffix += 1
    log_file.write_text(
        f"# {printable}\nexit={proc.returncode} duration={duration:.1f}s\n\n{log_text}",
        encoding="utf-8",
    )
    passed, skipped = _counts(step, log_text)
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
        skipped=skipped,
    )


def _pytest(argv: list[str]) -> Step:
    return Step(
        name="pytest",
        argv=[PY, "-m", "pytest", *argv],
        count_re=r"(\d+) passed",
        skip_re=r"(\d+) skipped",
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
    # The gate must match what the R5-7 report claims it covers. It previously
    # ran only three files while the report listed the Explorer boundary,
    # network gateway, data-reference injection, goal semantics and trace hash
    # v2 as well — i.e. the report described a wider gate than the code ran.
    return [
        _pytest(
            [
                "-q",
                "tests/test_security_r4.py",
                "tests/test_sandbox_paths.py",
                "tests/test_repair_sandbox.py",
                "tests/test_explorer_security.py",
                "tests/test_network_gateway.py",
                "tests/test_dataref_security.py",
                "tests/test_explorer_api.py",
                "tests/test_goal_semantics.py",
                "tests/test_trace_integrity_v2.py",
            ]
        )
    ]


def product_steps(out_dir: Path, *, build_web: bool) -> list[Step]:
    """The real product flow (own SUT, own API, own data dirs, real browser)."""
    summary = out_dir / "product-e2e.json"
    argv = [
        PY,
        str(ROOT / "scripts" / "e2e_product_flow.py"),
        "--work-dir",
        str(out_dir / "product"),
        "--json",
        str(summary),
    ]
    if build_web:
        argv.append("--build-web")
    return [
        Step(
            "product-e2e",
            argv,
            count_re=r"passed=(\d+)",
            skip_re=r"skipped=(\d+)",
            timeout_s=1800,
        )
    ]


# --------------------------------------------------------------------------- #
# Browser acceptance: start an isolated API, run the acceptance script, stop it.
# --------------------------------------------------------------------------- #


def run_browser(
    out_dir: Path,
    extra_env: dict[str, str],
    *,
    seed: Path | None = None,
) -> StepResult:
    import httpx

    work = out_dir / "browser"
    work.mkdir(parents=True, exist_ok=True)
    # Fresh database for every run (R5-4.5 §7): the file name carries the run
    # stamp so an earlier run's rows can never be mistaken for this run's.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    db = work / f"browser-{stamp}.db"
    env = os.environ.copy()
    env.update(extra_env)
    env["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    env["TRACEGUARD_DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    env["TEAMFLOW_BASE_URL"] = "http://127.0.0.1:8001"
    # Every file-backed store lives under this run's own directory.
    for key, sub in (
        ("EXPLORATION_DATA_DIR", "explorations"),
        ("BENCHMARK_DATA_DIR", "benchmark"),
        ("CHAIN_DATA_DIR", "chains"),
        ("BLOB_DIR", "blobs"),
        ("CANDIDATE_DATA_DIR", "candidates"),
    ):
        env[key] = str(out_dir / "stores" / sub)
    env["TRACEGUARD_SHOTS"] = str(work / "screenshots")
    seed_note = "fresh empty database"
    if seed is not None and seed.is_dir():
        # Seeding is explicit and still isolated: the dataset is read from the
        # seed directory (this run's product E2E output) and the DB is copied.
        seed_db = seed / "platform.db"
        if seed_db.exists():
            shutil.copy2(seed_db, db)
            seed_note = f"seeded from {_rel(seed)} (copied to {db.name})"
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
    print(f"  [i] browser database: {db.name} ({seed_note})", flush=True)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
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
                note=seed_note,
            )

        summary_path = work / "e2e-summary.json"
        run_env = env | {
            "TRACEGUARD_BASE": base,
            "TRACEGUARD_E2E_SUMMARY": str(summary_path),
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
        match = re.search(r"failures=(\d+)", text)
        if match:
            failures = int(match.group(1))
        total = None
        match = re.search(r"total=(\d+)", text)
        if match:
            total = int(match.group(1))
        skipped = 0
        match = re.search(r"skipped=(\d+)", text)
        if match:
            skipped = int(match.group(1))
        checks: list[dict[str, str]] = []
        if summary_path.exists():
            try:
                payload = json.loads(summary_path.read_text(encoding="utf-8"))
                checks = list(payload.get("results") or [])
            except json.JSONDecodeError:
                checks = []
        ok = proc.returncode == 0
        label = "PASS" if ok else "FAIL"
        print(f"  [{label}] browser exit={proc.returncode} {duration:.1f}s", flush=True)
        if not ok:
            for line in text.strip().splitlines()[-15:]:
                print(f"    {line}", flush=True)
        passed = None
        if total is not None and failures is not None:
            passed = total - failures - skipped
        return StepResult(
            name="browser",
            command=f"ui_acceptance.py @ {base}",
            exit_code=proc.returncode,
            duration_s=round(duration, 1),
            started=started.isoformat(timespec="seconds"),
            finished=datetime.now().isoformat(timespec="seconds"),
            passed=passed,
            log=_rel(log_path),
            skipped=skipped,
            checks=checks,
            note=seed_note,
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


def _load_checks(out_dir: Path, step: StepResult) -> list[dict[str, str]]:
    """Read the per-check list a gate wrote, for the combined report."""
    if step.checks:
        return step.checks
    if step.name == "product-e2e":
        summary = out_dir / "product-e2e.json"
        if summary.exists():
            try:
                payload = json.loads(summary.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
            return [
                {
                    "route": "product",
                    "check": str(item.get("step") or ""),
                    "verdict": f"{item.get('status')} {item.get('detail', '')}".strip(),
                }
                for item in payload.get("steps") or []
            ]
    return []


def _product_work_dir(out_dir: Path) -> Path | None:
    summary = out_dir / "product-e2e.json"
    if not summary.exists():
        return None
    try:
        payload = json.loads(summary.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    work = str(payload.get("work_dir") or "")
    if not work:
        return None
    path = Path(work)
    if not path.is_absolute():
        path = ROOT / path
    return path if path.is_dir() else None


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

ALL_MODES = ["python", "frontend", "security", "product", "browser"]


def main() -> int:
    parser = argparse.ArgumentParser(description="TraceGuard verification runner")
    parser.add_argument("--mode", default="quick", choices=MODES)
    parser.add_argument(
        "--keep-going", action="store_true", help="run later gates even if one fails"
    )
    parser.add_argument(
        "--browser-seed",
        default=os.environ.get("TRACEGUARD_BROWSER_SEED", ""),
        help="dataset directory the browser gate may read (default: this run's product E2E)",
    )
    parser.add_argument(
        "--build-web",
        action="store_true",
        help="build apps/web inside the product gate (needed on a clean checkout)",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="explicit output directory (default: a unique per-run path)",
    )
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (
        Path(args.out_dir) if args.out_dir else R5 / "verify" / f"{stamp}-{args.mode}-{os.getpid()}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Isolated DB + basetemp for every gate that runs tests. Both names carry
    # this run's stamp: no wipe, no reuse, no dependence on a previous run.
    isolated_db = out_dir / f"isolated-{stamp}.db"
    extra_env = {
        # Settings reads DATABASE_URL; alembic env.py reads TRACEGUARD_DATABASE_URL.
        # Both point at the throwaway file so no gate can write to data/traceguard.db.
        "DATABASE_URL": f"sqlite:///{isolated_db.as_posix()}",
        "TRACEGUARD_DATABASE_URL": f"sqlite:///{isolated_db.as_posix()}",
        "PYTHONUTF8": "1",
    }

    modes = ALL_MODES if args.mode == "all" else [args.mode]
    steps: list[tuple[str, Step | None]] = []
    for mode in modes:
        if mode == "browser":
            steps.append(("browser", None))
        elif mode == "product":
            for step in product_steps(out_dir, build_web=args.build_web):
                steps.append((mode, step))
        else:
            for step in MODE_STEPS[mode]:
                if step.name == "pytest":
                    # One basetemp per run, inside this run's output directory.
                    step.argv = [*step.argv, f"--basetemp={out_dir / 'pytest-tmp'}"]
                steps.append((mode, step))

    print("=" * 70)
    print(f" TraceGuard verify — mode={args.mode} @ {stamp}")
    print(f" python={PY}")
    print(f" output={_rel(out_dir)}")
    print("=" * 70)

    explicit_seed = Path(args.browser_seed) if args.browser_seed else None
    results: list[StepResult] = []
    for _mode, queued in steps:
        if queued is None:
            # Prefer an explicitly requested dataset, else the one this very run
            # just produced — never a leftover from an earlier run.
            seed = explicit_seed if explicit_seed is not None else _product_work_dir(out_dir)
            results.append(run_browser(out_dir, extra_env, seed=seed))
        else:
            results.append(_run(queued, out_dir, extra_env))
        if not results[-1].ok and not args.keep_going:
            name = results[-1].name
            print(f"\n[!] gate '{name}' failed — stopping (use --keep-going to continue)")
            break

    for result in results:
        result.checks = _load_checks(out_dir, result)

    failed = [r for r in results if not r.ok]
    report = {
        "mode": args.mode,
        "stamp": stamp,
        "python": PY,
        "out_dir": _rel(out_dir),
        "steps": [r.to_dict() for r in results],
        "failed": [r.name for r in failed],
        "ok": not failed,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [f"# Verify report — mode={args.mode} @ {stamp}", ""]
    lines.append(f"Output directory: `{_rel(out_dir)}`")
    lines.append("")
    lines.append("| step | status | exit | duration | passed | skipped | log |")
    lines.append("|------|--------|------|----------|--------|---------|-----|")
    for r in results:
        passed = r.passed if r.passed is not None else "-"
        lines.append(
            f"| {r.name} | {r.status} | {r.exit_code} | {r.duration_s}s | "
            f"{passed} | {r.skipped} | `{r.log}` |"
        )
    lines.append("")
    for r in results:
        if not r.checks:
            continue
        lines.append(f"## {r.name} — {len(r.checks)} recorded assertions")
        if r.note:
            lines.append("")
            lines.append(f"fixture: {r.note}")
        lines.append("")
        lines.append("| check | verdict |")
        lines.append("|-------|---------|")
        for check in r.checks:
            verdict = str(check.get("verdict") or "").replace("|", "\\|")
            lines.append(f"| {check.get('route')}/{check.get('check')} | {verdict} |")
        lines.append("")
    verdict = "PASS" if not failed else "FAIL"
    suffix = f" — failed: {', '.join(r.name for r in failed)}" if failed else ""
    lines.append(f"**Result: {verdict}{suffix}**")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 70)
    for r in results:
        print(
            f"  {r.status:4} {r.name:22} exit={r.exit_code:<3} "
            f"{r.duration_s}s passed={r.passed} skipped={r.skipped}"
        )
    print("=" * 70)
    print(f" report: {_rel(out_dir / 'report.md')}")
    if failed:
        print(f" RESULT: FAIL ({', '.join(r.name for r in failed)})")
        return 1
    print(" RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
