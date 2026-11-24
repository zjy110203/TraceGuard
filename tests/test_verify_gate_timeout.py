"""Regression tests for the R5-4.11 timeout classification in ``verify_all``.

Before this change ``subprocess.TimeoutExpired`` was recorded as BLOCKED, i.e.
a deadlock, a stuck browser or a test that waits forever were all reported as
"environment blocked" and the gate could never fail for the one reason it
exists. These tests launch real (short-lived) hanging processes: no mocking of
the timeout path is involved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.verify_all import (
    TIMEOUT_EXIT_CODE,
    Step,
    StepResult,
    _run,
    compute_exit_code,
    run_queue,
    summary_line,
    write_report,
)

ROOT = Path(__file__).resolve().parents[1]
SLEEP_S = 0.6


def _hang_step(
    name: str = "hanging-gate",
    *,
    kind: str = "command",
    preamble: str = "print('gate started', flush=True)",
    timeout_block_re: tuple[str, ...] = (),
) -> Step:
    """A real process that prints, then hangs past its deadline."""
    script = f"{preamble}; import time; time.sleep(30)"
    return Step(
        name,
        [sys.executable, "-u", "-c", script],
        timeout_s=SLEEP_S,
        kind=kind,
        timeout_block_re=timeout_block_re,
    )


def _log_text(result: StepResult) -> str:
    log = Path(result.log)
    if not log.is_absolute():
        log = ROOT / log
    return log.read_text(encoding="utf-8")


def test_command_timeout_is_fail_not_blocked(tmp_path: Path) -> None:
    result = _run(_hang_step(), tmp_path, {})

    assert result.status == "FAIL"
    assert result.status != "BLOCKED"
    assert result.ok is False
    assert result.timed_out is True
    assert result.exit_code == TIMEOUT_EXIT_CODE
    # A deadline is not, by itself, evidence of an environment problem.
    assert result.blocked_evidence == ""
    assert "timeout after" in result.evidence


def test_test_gate_timeout_is_fail_not_empty(tmp_path: Path) -> None:
    result = _run(_hang_step(name="pytest", kind="test"), tmp_path, {})

    assert result.status == "FAIL"
    assert result.status not in {"BLOCKED", "EMPTY", "SKIP"}
    assert result.timed_out is True


def test_timeout_records_seconds_pid_and_log(tmp_path: Path) -> None:
    result = _run(_hang_step(name="needs-evidence"), tmp_path, {})

    assert result.timeout_s == SLEEP_S
    assert "pid " in result.evidence
    log = _log_text(result)
    assert "TIMEOUT" in log
    # Output produced before the kill is the only diagnostic left.
    assert "gate started" in log


def test_timeout_classified_blocked_only_with_declared_narrow_signature(
    tmp_path: Path,
) -> None:
    blocked = _run(
        _hang_step(
            name="port-wait",
            preamble="print('ERROR: Address already in use', flush=True)",
            timeout_block_re=(r"Address already in use",),
        ),
        tmp_path,
        {},
    )

    assert blocked.status == "BLOCKED"
    assert blocked.timed_out is True
    assert "Address already in use" in blocked.blocked_evidence


def test_timeout_keeps_remaining_gates_as_not_run(tmp_path: Path) -> None:
    after = Step("after-timeout", [sys.executable, "-c", "print('ok')"])
    results = run_queue(
        [_hang_step(), after],
        out_dir=tmp_path,
        extra_env={},
        keep_going=False,
    )

    assert [r.status for r in results] == ["FAIL", "NOT_RUN"]
    assert results[1].ran is False
    assert compute_exit_code(results) != 0


def test_keep_going_still_runs_the_rest_after_a_timeout(tmp_path: Path) -> None:
    after = Step("after-timeout", [sys.executable, "-c", "print('ok')"])
    results = run_queue(
        [_hang_step(), after],
        out_dir=tmp_path,
        extra_env={},
        keep_going=True,
    )

    assert results[0].status == "FAIL"
    assert results[1].status == "PASS"


def test_timeout_is_consistent_across_console_json_markdown_and_exit_code(
    tmp_path: Path,
) -> None:
    result = _run(_hang_step(name="timeout-gate"), tmp_path, {})
    json_path, md_path = write_report(
        tmp_path, mode="all", stamp="20260101-000000", results=[result]
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    step = payload["steps"][0]
    markdown = md_path.read_text(encoding="utf-8")

    assert step["status"] == "FAIL"
    assert step["timed_out"] is True
    assert step["timeout_s"] == SLEEP_S
    assert payload["failed"] == ["timeout-gate"]
    assert "| timeout-gate | FAIL |" in markdown
    assert f"{SLEEP_S:g}s" in markdown
    assert summary_line(result).strip().startswith("FAIL")
    assert f"timeout={SLEEP_S:g}s" in summary_line(result)
    assert compute_exit_code([result]) != 0
