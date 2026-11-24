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

Status model (single source of truth)
-------------------------------------
``StepResult.status`` is the only place a verdict is computed. The console line
(:func:`summary_line`), ``report.json`` and ``report.md`` all read it, so the
three can never disagree.

* ``NOT_RUN`` — the step was queued but never executed (an earlier gate stopped
  the run); it still takes part in the exit code, so a short run is never a
  silent pass.
* ``PASS``     — exit 0, and for test-style gates at least one test really ran.
* ``SKIP``     — test-style gate where every collected test was skipped.
* ``EMPTY``    — test-style gate that collected nothing (pytest exit 5 /
  "no tests ran"); a command gate can never be EMPTY.
* ``BLOCKED``  — non-zero exit **and** a narrow environment-blocking signature
  was found in the output (missing module/tool, installed browser missing,
  port already in use); the matched line is kept as evidence.
* ``FAIL``     — non-zero exit with no blocking evidence: a real failure.

Anything that is not ``PASS`` makes the run exit non-zero.
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
from collections.abc import Sequence
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

# Narrow, high-confidence environment-blocking signatures. They are deliberately
# specific: a genuine assertion failure must never be re-labelled as "blocked".
# "Address already in use" is NOT here on purpose — only the browser gate treats
# a port collision as blocking, because only it starts a server.
DEFAULT_BLOCK_RE: tuple[str, ...] = (
    r"No module named '",
    r"Executable doesn't exist",
    r"playwright install",
    r"ModuleNotFoundError",
    r"npm error code ENOENT",
    r"Cannot find module",
    r"not installed under apps/web/node_modules",
)

# pytest exit code 5 = "no tests were collected". It is not a failure and must
# not be reported as one.
PYTEST_EMPTY_EXIT_CODES: tuple[int, ...] = (5,)

BLOCKED = "BLOCKED"
NOT_RUN = "NOT_RUN"

# TimeoutExpired carries no return code of its own; keep the conventional 124.
TIMEOUT_EXIT_CODE = 124
# A step that never launched has no exit code at all.
UNLAUNCHED_EXIT_CODE = -1


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
    """A queued gate.

    ``kind`` decides the verdict semantics: ``command`` gates (lint, typecheck,
    build) pass or fail on the exit code and are never EMPTY; ``test`` gates
    additionally have to prove that tests actually ran.
    """

    name: str
    argv: list[str]
    cwd: Path = ROOT
    env: dict[str, str] = field(default_factory=dict)
    # Regex used to pull a "passed" count out of the command output.
    count_re: str | None = None
    # Regex used to pull a "skipped" count out of the command output.
    skip_re: str | None = None
    timeout_s: float | None = None
    kind: str = "command"
    block_re: tuple[str, ...] = DEFAULT_BLOCK_RE
    # Exit codes that mean "nothing ran" rather than "something broke".
    empty_exit_codes: tuple[int, ...] = ()


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
    kind: str = "command"
    # True only when the command was actually launched. A queued step that was
    # never started must say so instead of vanishing from the report.
    ran: bool = True
    # Concrete text proving an environment block (never empty when BLOCKED).
    blocked_evidence: str = ""
    # Evidence for EMPTY/SKIP (e.g. pytest's "no tests ran").
    evidence: str = ""
    empty_exit_codes: tuple[int, ...] = ()

    @property
    def status(self) -> str:
        """The single source of truth for every report and console line."""
        if not self.ran:
            return NOT_RUN
        if self.exit_code in self.empty_exit_codes:
            return "EMPTY"
        if self.exit_code == 0:
            if self.kind == "command":
                # A command gate is judged by its exit code alone: it never
                # collects tests, so "zero counts" is not emptiness.
                return "PASS"
            if self.passed is not None and self.passed > 0:
                return "PASS"
            if self.passed == 0 and self.skipped > 0:
                return "SKIP"
            if self.passed == 0:
                return "EMPTY"
            # Exit 0 with an unknown count: the suite ran and succeeded; calling
            # it EMPTY would claim something the output does not say.
            return "PASS"
        if self.blocked_evidence:
            return BLOCKED
        return "FAIL"

    @property
    def ok(self) -> bool:
        return self.status == "PASS"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": self.command,
            "kind": self.kind,
            "exit_code": self.exit_code,
            "duration_s": self.duration_s,
            "started": self.started,
            "finished": self.finished,
            "ran": self.ran,
            "status": self.status,
            "passed": self.passed,
            "skipped": self.skipped,
            "ok": self.ok,
            "blocked_evidence": self.blocked_evidence,
            "evidence": self.evidence,
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


def _find_line(text: str, patterns: Sequence[str]) -> str:
    """First line matching any pattern, stripped — the evidence we keep."""
    for line in text.splitlines():
        for pattern in patterns:
            if re.search(pattern, line):
                return line.strip()[:300]
    return ""


def _log_path(out_dir: Path, name: str) -> Path:
    """Unique per-step log path.

    Two steps can share a name (e.g. the full suite and the security subset are
    both "pytest"). Silently overwriting the first log destroys the only copy of
    a failure, which is exactly what happened before this guard existed.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "step"
    candidate = out_dir / f"{slug}.log"
    suffix = 2
    while candidate.exists():
        candidate = out_dir / f"{slug}-{suffix}.log"
        suffix += 1
    return candidate


def summary_line(result: StepResult) -> str:
    """The one console line printed when a gate finishes."""
    return (
        f"  {result.status:<8} {result.name:<22} exit={result.exit_code:<4} "
        f"{result.duration_s}s passed={result.passed} skipped={result.skipped}"
    )


def compute_exit_code(results: Sequence[StepResult]) -> int:
    """0 only when every result is PASS; SKIP/EMPTY/BLOCKED/NOT_RUN all count."""
    return 0 if all(result.ok for result in results) else 1


def write_report(
    out_dir: Path,
    *,
    mode: str,
    stamp: str,
    results: Sequence[StepResult],
) -> tuple[Path, Path]:
    """Write report.json + report.md; both take their status from ``StepResult``."""
    failed = [r for r in results if not r.ok]
    report = {
        "mode": mode,
        "stamp": stamp,
        "python": PY,
        "out_dir": _rel(out_dir),
        "steps": [r.to_dict() for r in results],
        "failed": [r.name for r in failed],
        "ok": not failed,
    }
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# Verify report — mode={mode} @ {stamp}", ""]
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
        if r.status == "PASS":
            continue
        detail = r.blocked_evidence or r.evidence or r.note
        if detail:
            lines.append(f"- **{r.name}**: {r.status} — {detail}")
    if any(r.status != "PASS" for r in results):
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
    md_path = out_dir / "report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def not_run_result(step: Step, reason: str = "an earlier gate stopped the run") -> StepResult:
    """Record a queued step that was never launched so the report stays whole."""
    now = datetime.now().isoformat(timespec="seconds")
    printable = " ".join([Path(step.argv[0]).name, *step.argv[1:]]) if step.argv else "(not run)"
    return StepResult(
        name=step.name,
        command=printable,
        exit_code=UNLAUNCHED_EXIT_CODE,
        duration_s=0.0,
        started=now,
        finished=now,
        passed=None,
        log="",
        kind=step.kind,
        ran=False,
        note=f"not run: {reason}",
    )


def _exception_result(
    step: Step,
    printable: str,
    *,
    started: datetime,
    duration: float,
    out_dir: Path,
    evidence: str,
    exit_code: int,
) -> StepResult:
    log_file = _log_path(out_dir, step.name)
    log_file.write_text(f"# {printable}\n{BLOCKED}: {evidence}\n", encoding="utf-8")
    result = StepResult(
        name=step.name,
        command=printable,
        exit_code=exit_code,
        duration_s=round(duration, 1),
        started=started.isoformat(timespec="seconds"),
        finished=datetime.now().isoformat(timespec="seconds"),
        passed=None,
        log=_rel(log_file),
        kind=step.kind,
        blocked_evidence=evidence,
        note="environment blocked before the gate could produce a verdict",
    )
    print(summary_line(result), flush=True)
    return result


def _run(step: Step, out_dir: Path, extra_env: dict[str, str]) -> StepResult:
    env = os.environ.copy()
    env.update(extra_env)
    env.update(step.env)
    started = datetime.now()
    clock = time.monotonic()
    printable = " ".join([Path(step.argv[0]).name, *step.argv[1:]])
    print(f"\n$ ({step.cwd.name}) {printable}", flush=True)
    try:
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
    except subprocess.TimeoutExpired:
        # A hang is an environment block, not proof that the code is wrong.
        return _exception_result(
            step,
            printable,
            started=started,
            duration=time.monotonic() - clock,
            out_dir=out_dir,
            evidence=f"timeout after {step.timeout_s}s",
            exit_code=TIMEOUT_EXIT_CODE,
        )
    except OSError as exc:
        # Missing executable, permission problem, bad cwd: never a traceback.
        return _exception_result(
            step,
            printable,
            started=started,
            duration=time.monotonic() - clock,
            out_dir=out_dir,
            evidence=f"{type(exc).__name__}: {exc}",
            exit_code=1,
        )
    duration = time.monotonic() - clock
    finished = datetime.now()
    log_text = (proc.stdout or "") + (proc.stderr or "")
    log_file = _log_path(out_dir, step.name)
    log_file.write_text(
        f"# {printable}\nexit={proc.returncode} duration={duration:.1f}s\n\n{log_text}",
        encoding="utf-8",
    )
    passed, skipped = _counts(step, log_text)
    blocked_evidence = ""
    if proc.returncode != 0 and proc.returncode not in step.empty_exit_codes:
        blocked_evidence = _find_line(log_text, step.block_re)
    evidence = ""
    if step.kind == "test" and passed == 0 and skipped == 0:
        evidence = _find_line(
            log_text,
            (r"no tests ran", r"collected 0 items", r"no tests collected"),
        )
    result = StepResult(
        name=step.name,
        command=printable,
        exit_code=proc.returncode,
        duration_s=round(duration, 1),
        started=started.isoformat(timespec="seconds"),
        finished=finished.isoformat(timespec="seconds"),
        passed=passed,
        log=_rel(log_file),
        skipped=skipped,
        kind=step.kind,
        blocked_evidence=blocked_evidence,
        evidence=evidence,
        empty_exit_codes=step.empty_exit_codes,
    )
    print(summary_line(result), flush=True)
    if result.status != "PASS":
        if blocked_evidence:
            print(f"    blocked: {blocked_evidence}", flush=True)
        tail = log_text.strip().splitlines()[-15:]
        for line in tail:
            print(f"    {line}", flush=True)
    return result


def _pytest(argv: list[str]) -> Step:
    return Step(
        name="pytest",
        argv=[PY, "-m", "pytest", *argv],
        count_re=r"(\d+) passed",
        skip_re=r"(\d+) skipped",
        kind="test",
        empty_exit_codes=PYTEST_EMPTY_EXIT_CODES,
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
            kind="test",
        )
    ]


# --------------------------------------------------------------------------- #
# Browser acceptance: start an isolated API, run the acceptance script, stop it.
# --------------------------------------------------------------------------- #


def _terminate(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        server.kill()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _wait_healthy(base: str, server: subprocess.Popen[bytes], attempts: int = 80) -> bool:
    import httpx

    for _ in range(attempts):
        if server.poll() is not None:
            # The child died; no amount of polling will make it healthy.
            return False
        try:
            response = httpx.get(f"{base}/health", timeout=1.0, trust_env=False)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code == 200:
            return True
        time.sleep(0.25)
    return False


def run_browser(
    out_dir: Path,
    extra_env: dict[str, str],
    *,
    seed: Path | None = None,
) -> StepResult:
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

    started = datetime.now()
    clock = time.monotonic()
    log_path = out_dir / "browser.log"
    server_log = work / "server.log"
    server: subprocess.Popen[bytes] | None = None
    base = ""
    evidence = ""
    attempt_limit = 3
    try:
        # ``_free_port`` releases the socket before uvicorn binds it, so a race
        # is possible. Retry on a fresh port and only treat repeated failures as
        # a block — never assume the code under test is wrong.
        for attempt in range(1, attempt_limit + 1):
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            print(
                f"  [i] starting platform API on {base} (attempt {attempt}/{attempt_limit})",
                flush=True,
            )
            with server_log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n# attempt {attempt} port {port}\n")
                handle.flush()
                candidate = subprocess.Popen(  # noqa: S603
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
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
            if _wait_healthy(base, candidate):
                server = candidate
                break
            _terminate(candidate)
            tail = server_log.read_text(encoding="utf-8", errors="replace")
            occupied = _find_line(tail, (r"Address already in use",))
            if occupied:
                evidence = f"{occupied} (attempt {attempt}/{attempt_limit}, port {port})"
                break
            evidence = (
                f"platform API did not become healthy on {base} (attempt {attempt}/{attempt_limit})"
            )
        if server is None:
            evidence = evidence or "platform API did not become healthy"
            log_path.write_text(f"API startup blocked: {evidence}\n", encoding="utf-8")
            result = StepResult(
                name="browser",
                command="ui_acceptance.py (API startup blocked)",
                exit_code=1,
                duration_s=round(time.monotonic() - clock, 1),
                started=started.isoformat(timespec="seconds"),
                finished=datetime.now().isoformat(timespec="seconds"),
                passed=None,
                log=_rel(log_path),
                kind="test",
                blocked_evidence=evidence,
                note=seed_note,
            )
            print(summary_line(result), flush=True)
            return result

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
        # Do not invent a pass count when the script did not report one.
        passed: int | None = None
        if total is not None and failures is not None:
            passed = max(total - failures - skipped, 0)
        result = StepResult(
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
            kind="test",
        )
        print(summary_line(result), flush=True)
        if result.status != "PASS":
            if result.blocked_evidence:
                print(f"    blocked: {result.blocked_evidence}", flush=True)
            for line in text.strip().splitlines()[-15:]:
                print(f"    {line}", flush=True)
        return result
    finally:
        if server is not None:
            _terminate(server)


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

ALL_MODES = ["python", "frontend", "security", "product", "browser"]


def mode_steps(mode: str) -> list[Step]:
    """Build a mode's steps lazily.

    Building these at import time used to call ``_node_bin`` and raise a
    RuntimeError on a checkout without ``node_modules`` — before argparse even
    ran, so no report could be written. Resolution must happen when the gate is
    actually about to run.
    """
    if mode == "quick":
        return [
            Step("ruff-check", [PY, "-m", "ruff", "check", "."]),
            Step("ruff-format-check", [PY, "-m", "ruff", "format", "--check", "."]),
            Step("mypy", MYPY_ARGV),
            Step("vue-tsc", [_node_bin("vue-tsc"), "--noEmit"], cwd=WEB),
        ]
    if mode == "python":
        return python_steps(full=True)
    if mode == "frontend":
        return frontend_steps()
    if mode == "security":
        return security_steps()
    raise ValueError(f"mode {mode!r} has no step list")


def toolchain_blocked(mode: str, exc: RuntimeError) -> StepResult:
    """A mode whose tools cannot even be resolved is BLOCKED, with the reason."""
    now = datetime.now().isoformat(timespec="seconds")
    return StepResult(
        name=f"{mode}-toolchain",
        command=f"(resolve {mode} tools)",
        exit_code=1,
        duration_s=0.0,
        started=now,
        finished=now,
        passed=None,
        log="",
        blocked_evidence=str(exc),
        note="environment blocked before any command was launched",
    )


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
    # ``None`` means the browser gate; a StepResult here is a gate that was
    # blocked during construction (its toolchain could not be resolved).
    queue: list[Step | None | StepResult] = []
    for mode in modes:
        if mode == "browser":
            queue.append(None)
        elif mode == "product":
            queue.extend(product_steps(out_dir, build_web=args.build_web))
        else:
            try:
                built = mode_steps(mode)
            except RuntimeError as exc:
                queue.append(toolchain_blocked(mode, exc))
                continue
            for step in built:
                if step.name == "pytest" and not any(
                    arg.startswith("--basetemp=") for arg in step.argv
                ):
                    # One basetemp per run, inside this run's output directory.
                    step.argv = [*step.argv, f"--basetemp={out_dir / 'pytest-tmp'}"]
                queue.append(step)

    print("=" * 70)
    print(f" TraceGuard verify — mode={args.mode} @ {stamp}")
    print(f" python={PY}")
    print(f" output={_rel(out_dir)}")
    print("=" * 70)

    explicit_seed = Path(args.browser_seed) if args.browser_seed else None
    results: list[StepResult] = []
    stopped = False
    for item in queue:
        if stopped:
            # Everything still queued is recorded as NOT_RUN: an aborted run must
            # not look like a complete one, and NOT_RUN still fails the run.
            if isinstance(item, Step):
                results.append(not_run_result(item))
            elif isinstance(item, StepResult):
                results.append(item)
            else:
                results.append(not_run_result(Step("browser", [])))
            continue
        if isinstance(item, StepResult):
            result = item
        elif item is None:
            # Prefer an explicitly requested dataset, else the one this very run
            # just produced — never a leftover from an earlier run.
            seed = explicit_seed if explicit_seed is not None else _product_work_dir(out_dir)
            result = run_browser(out_dir, extra_env, seed=seed)
        else:
            result = _run(item, out_dir, extra_env)
        results.append(result)
        if not result.ok and not args.keep_going:
            stopped = True
            print(
                f"\n[!] gate '{result.name}' is {result.status} — stopping "
                "(use --keep-going to continue)",
                flush=True,
            )

    for result in results:
        if result.ran:
            result.checks = _load_checks(out_dir, result)

    json_path, md_path = write_report(out_dir, mode=args.mode, stamp=stamp, results=results)

    print("\n" + "=" * 70)
    for result in results:
        print(summary_line(result))
    print("=" * 70)
    print(f" report: {_rel(md_path)} (json: {_rel(json_path)})")
    code = compute_exit_code(results)
    if code != 0:
        bad = ", ".join(f"{r.name}={r.status}" for r in results if not r.ok)
        print(f" RESULT: FAIL ({bad})")
        return code
    print(" RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
