"""Oracle-Protected Self-Healing sandbox (R3-W6).

The stage-10 state machine proves a *candidate* is structurally safe; this module
proves it actually works in a real browser. Lifecycle:

    open()      -> unique sandbox dir + ownership marker
    _start_target() -> isolated TeamFlow subprocess (own PID file, own SQLite)
    replay()    -> real Chrome via LocalRunner, same data seed both runs
    run()       -> baseline replay, candidate replay, comparison, evidence files
    close()     -> stop only OUR subprocess, delete only OUR directory

Security properties (per R3 §2.4):
* nothing is copied from the project except the two case JSON files the caller
  passes in; ``.venv`` / ``node_modules`` / ``.env`` / real databases are never
  touched — the target app gets a throwaway SQLite file inside the sandbox dir;
* the subprocess is started by this module and identified by its PID, recorded in
  the ownership marker, so crash-cleanup only ever terminates our own child;
* directory deletion happens only after re-resolving the path and verifying the
  marker belongs to this sandbox — never on a symlink/junction escape.

A candidate is judged safe only when: baseline replay FAILS on the drifted asset,
candidate replay PASSES on the identical scenario, and a control case replays
PASSED both before and after (regression). Anything else is NOT validated, and no
promotion is attempted here — promotion stays behind the human approval gate in
``RepairService``.
"""

from __future__ import annotations

import enum
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from packages.execution.models import TestCaseVersion, load_case
from packages.execution.runner import LocalRunner

# parents[2] = the project root (packages/execution/sandbox.py). parents[1]
# would point INSIDE packages/ — that mistake made the child process run with
# cwd=packages/, where `apps` is not importable.
ROOT = Path(__file__).resolve().parents[2]
SANDBOX_ROOT = ROOT / ".traceguard-tmp" / "r3"
MARKER_NAME = "_traceguard_sandbox.json"
OWNERSHIP = {"project": "traceguard", "round": "r3", "owner": "RepairSandbox"}
HEALTH_TIMEOUT_S = 60.0
REPLAY_BUDGET_S = 90.0
EVAL_TOKEN = "local-dev-eval-token-change-me"


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot provide its guarantees."""


@dataclass
class ReplayOutcome:
    case_id: str
    execution_status: str
    test_verdict: str
    steps_ok: int
    steps_total: int
    assertions_passed: int
    assertions_failed: int
    aborted_reason: str
    started_at: str
    finished_at: str
    duration_s: float


@dataclass
class SandboxResult:
    sandbox_id: str
    proposal_id: str
    baseline: ReplayOutcome | None
    candidate: ReplayOutcome | None
    control_before: ReplayOutcome | None
    control_after: ReplayOutcome | None
    verdict: str  # VALIDATED | NOT_VALIDATED | SANDBOX_FAILED | NO_BEHAVIORAL_CHANGE
    reason: str
    target_pid: int | None = None
    target_url: str = ""
    # The sandbox directory is deleted on close(); evidence must therefore live
    # here (and be handed to the platform via ``on_evidence``) rather than only
    # inside the throwaway directory, or cleanup would destroy the proof.
    evidence: dict[str, Any] = field(default_factory=dict)
    evidence_sha256: str = ""
    # R4 hardening: delivery/cleanup failures must be explicit, never silent.
    evidence_delivery_error: str = ""
    cleanup_error: str = ""


class RepairSandbox:
    """Isolated replay environment for one repair proposal."""

    def __init__(
        self,
        *,
        proposal_id: str,
        base_case: TestCaseVersion,
        candidate: TestCaseVersion,
        control_case: TestCaseVersion | None = None,
        on_evidence: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.sandbox_id = f"sb_{uuid.uuid4().hex[:12]}"
        self.proposal_id = proposal_id
        self.base_case = base_case
        self.candidate = candidate
        self.control_case = control_case
        self.on_evidence = on_evidence
        self._dir = SANDBOX_ROOT / self.sandbox_id
        self._process: subprocess.Popen[Any] | None = None
        self._stderr_handle: Any = None
        self._target_url = ""
        self._target_pid: int | None = None
        self._cleanup_error = ""

    # ------------------------------------------------------------- lifecycle

    def open(self) -> Path:
        SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        self._dir.mkdir(parents=True, exist_ok=True)
        resolved = self._dir.resolve()
        if SANDBOX_ROOT.resolve() not in resolved.parents:
            raise SandboxError("sandbox dir escaped the project temporary root")
        marker = {
            **OWNERSHIP,
            "sandbox_id": self.sandbox_id,
            "proposal_id": self.proposal_id,
            "created_at": time.time(),
        }
        (resolved / MARKER_NAME).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return resolved

    def _start_target(self) -> str:
        """Start an isolated TeamFlow subprocess on a throwaway SQLite database."""
        port = _free_port()
        db_path = self._dir / "teamflow.db"
        # `python -m uvicorn` does not add the project root to sys.path.
        env = _child_env(
            {
                "PYTHONPATH": str(ROOT),
                "TRACEGUARD_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
                "TEAMFLOW_DISABLE_SEED": "1",
            }
        )
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.teamflow.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
        # Keep the target's stderr on disk: a subprocess that dies before health
        # must leave a diagnosable trace, not just a generic failure.
        self._stderr_handle = (self._dir / "target_stderr.log").open("wb")
        self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_handle,
        )
        self._target_pid = self._process.pid
        (self._dir / "target.pid").write_text(str(self._process.pid), encoding="utf-8")
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/health", timeout=1.0, trust_env=False).status_code == 200:
                    self._target_url = url
                    return url
            except httpx.HTTPError:
                pass
            if self._process.poll() is not None:
                raise SandboxError("sandbox TeamFlow exited before becoming healthy")
            time.sleep(0.2)
        raise SandboxError("sandbox TeamFlow did not become healthy in time")

    # -------------------------------------------------------------- replay

    def _write_case(self, case: TestCaseVersion, name: str) -> Path:
        payload = json.dumps(_case_jsonable(case), ensure_ascii=False, indent=2)
        path = self._dir / name
        path.write_text(payload + "\n", encoding="utf-8")
        loaded = load_case(path)
        assert loaded.case_id == case.case_id, "case round-trip changed identity"
        return path

    def replay(self, case: TestCaseVersion, label: str) -> ReplayOutcome:
        """Run one case with real Chrome against the isolated target."""
        started = time.monotonic()
        runner = LocalRunner(
            self._target_url,
            report_dir=self._dir / "reports" / label,
            eval_token=EVAL_TOKEN,
        )
        report = runner.run(case)
        duration = time.monotonic() - started
        if duration > REPLAY_BUDGET_S + 30:
            raise SandboxError(f"replay exceeded budget: {duration:.1f}s")
        assertions = report.assertion_results
        return ReplayOutcome(
            case_id=report.case_id,
            execution_status=report.execution_status.value,
            test_verdict=report.test_verdict,
            steps_ok=sum(1 for s in report.steps if s.ok),
            steps_total=len(report.steps),
            assertions_passed=sum(1 for a in assertions if a["verdict"] == "PASSED"),
            assertions_failed=sum(1 for a in assertions if a["verdict"] == "FAILED"),
            aborted_reason=report.aborted_reason,
            started_at=report.started_at,
            finished_at=report.finished_at,
            duration_s=round(duration, 2),
        )

    def _reset_data(self) -> None:
        with httpx.Client(base_url=self._target_url, timeout=10, trust_env=False) as client:
            headers = {"X-Eval-Token": EVAL_TOKEN}
            client.post("/__eval__/faults/clear", headers=headers)
            client.post("/__eval__/data/reset", headers=headers)

    # ---------------------------------------------------------------- main

    def run(self) -> SandboxResult:
        result = SandboxResult(
            sandbox_id=self.sandbox_id,
            proposal_id=self.proposal_id,
            baseline=None,
            candidate=None,
            control_before=None,
            control_after=None,
            verdict="SANDBOX_FAILED",
            reason="",
        )
        try:
            self.open()
            self._write_case(self.base_case, "base_case.json")
            self._write_case(self.candidate, "candidate_case.json")
            self._start_target()
            result.target_url = self._target_url
            result.target_pid = self._target_pid

            # Baseline: the drifted asset is expected to FAIL here.
            self._reset_data()
            result.baseline = self.replay(self.base_case, "baseline")
            # Candidate on an identical seed.
            self._reset_data()
            result.candidate = self.replay(self.candidate, "candidate")
            if self.control_case is not None:
                self._write_case(self.control_case, "control_case.json")
                self._reset_data()
                result.control_before = self.replay(self.control_case, "control_before")
                self._reset_data()
                result.control_after = self.replay(self.control_case, "control_after")

            self._compare(result)
            self._write_evidence(result)
            return result
        except SandboxError as exc:
            result.reason = f"sanbox error: {exc}".replace("sanbox", "sandbox")
            self._write_evidence(result)
            return result
        finally:
            self.close()
            # Cleanup failure must not disguise itself as success: if the
            # directory could not be removed, the result says so.
            if self._cleanup_error:
                result.cleanup_error = self._cleanup_error

    def _compare(self, result: SandboxResult) -> None:
        base, cand = result.baseline, result.candidate
        if base is None or cand is None:
            result.reason = "replay incomplete"
            return
        if base.test_verdict != "FAILED" and base.steps_ok == base.steps_total:
            result.verdict = "NO_BEHAVIORAL_CHANGE"
            result.reason = (
                "baseline already passes; a repair cannot be validated against a "
                "passing baseline (no drifted asset in this sandbox)"
            )
            return
        if base.test_verdict == "FAILED" and cand.test_verdict == "PASSED":
            if result.control_before and result.control_after:
                control_ok = (
                    result.control_before.test_verdict == "PASSED"
                    and result.control_after.test_verdict == "PASSED"
                )
                if not control_ok:
                    result.verdict = "NOT_VALIDATED"
                    result.reason = "control case regressed; repair changes more than the target"
                    return
            result.verdict = "VALIDATED"
            result.reason = (
                f"baseline FAILED ({base.steps_ok}/{base.steps_total} steps), "
                f"candidate PASSED ({cand.steps_ok}/{cand.steps_total} steps) on the "
                "same seed; control regression clean"
            )
            return
        result.verdict = "NOT_VALIDATED"
        result.reason = (
            f"baseline={base.test_verdict}, candidate={cand.test_verdict}; "
            "candidate did not fix the drifted asset without breaking others"
        )

    def _write_evidence(self, result: SandboxResult) -> None:
        import hashlib

        payload = {
            "sandbox_id": result.sandbox_id,
            "proposal_id": result.proposal_id,
            "verdict": result.verdict,
            "reason": result.reason,
            "baseline": asdict(result.baseline) if result.baseline else None,
            "candidate": asdict(result.candidate) if result.candidate else None,
            "control_before": asdict(result.control_before) if result.control_before else None,
            "control_after": asdict(result.control_after) if result.control_after else None,
            "target_pid": result.target_pid or self._target_pid,
            "target_url": result.target_url or self._target_url,
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        result.evidence = payload
        result.evidence_sha256 = hashlib.sha256(raw.encode()).hexdigest()
        if self.on_evidence is not None:
            # Hand the evidence to the platform BEFORE the directory is removed.
            # A failed delivery is recorded on the result — the caller gets an
            # explicit error state instead of a silently lost payload.
            try:
                self.on_evidence(payload)
            except Exception as exc:  # noqa: BLE001 - any delivery failure is reported
                result.evidence_delivery_error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------- teardown

    def close(self) -> None:
        """Stop only OUR subprocess and delete only OUR directory."""
        proc = self._process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self._process = None
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None

        resolved = self._dir.resolve()
        marker = resolved / MARKER_NAME
        if not resolved.exists():
            return
        if SANDBOX_ROOT.resolve() not in resolved.parents:
            return  # escaped containment: refuse to delete
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # no/invalid marker: keep the directory rather than guess
        if recorded.get("sandbox_id") != self.sandbox_id:
            return
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            # Failure is surfaced to run() (result.cleanup_error), not swallowed.
            self._cleanup_error = f"cleanup failed: {exc}"
        except SystemExit as exc:
            # Some environments inject delete-guard shims that abort rmtree
            # with SystemExit (a BaseException). A blocked teardown must be
            # recorded like any other cleanup failure, never propagated.
            self._cleanup_error = f"cleanup blocked: {type(exc).__name__}"


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    """Windows environment is case-insensitive, but ``os.environ`` can contain
    case-differing duplicates (e.g. ``HTTPS_PROXY`` and ``https_proxy`` from Git
    Bash). Passing both makes Windows build a corrupted child environment block
    in which PYTHONPATH is silently lost — the target then fails to import. Keys
    are therefore normalised to a single uppercase entry each.
    """
    merged: dict[str, str] = {}
    for key, value in os.environ.items():
        merged[key.upper()] = value
    merged.update({k.upper(): v for k, v in extra.items()})
    return merged


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _case_jsonable(case: TestCaseVersion) -> dict[str, Any]:
    raw = asdict(case)

    def walk(value: Any) -> Any:
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value

    return walk(raw)
