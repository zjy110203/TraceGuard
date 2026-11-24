"""Verdict semantics of ``scripts/verify_all.py``.

These tests are deliberately pure: every verdict is computed from a constructed
``StepResult``, so no gate (npm, mypy, pytest, a browser) is ever launched here.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_all import (
    PYTEST_EMPTY_EXIT_CODES,
    Step,
    StepResult,
    _pytest,
    _run,
    compute_exit_code,
    summary_line,
    write_report,
)

STAMP = "20260101-000000"
STARTED = "2026-01-01T00:00:00"
FINISHED = "2026-01-01T00:00:01"


def gate(
    *,
    name: str = "gate",
    kind: str = "test",
    exit_code: int = 0,
    passed: int | None = None,
    skipped: int = 0,
    blocked_evidence: str = "",
    evidence: str = "",
    ran: bool = True,
    empty_exit_codes: tuple[int, ...] = (),
) -> StepResult:
    return StepResult(
        name=name,
        command="pytest -q",
        exit_code=exit_code,
        duration_s=0.5,
        started=STARTED,
        finished=FINISHED,
        passed=passed,
        log="gate.log",
        skipped=skipped,
        kind=kind,
        blocked_evidence=blocked_evidence,
        evidence=evidence,
        ran=ran,
        empty_exit_codes=empty_exit_codes,
    )


def test_command_gate_without_counts_passes_and_is_never_empty() -> None:
    result = gate(kind="command", exit_code=0, passed=None)

    assert result.status == "PASS"
    assert result.status != "EMPTY"
    assert result.ok is True


def test_all_skipped_is_skip_and_not_ok() -> None:
    result = gate(exit_code=0, passed=0, skipped=3)

    assert result.status == "SKIP"
    assert result.ok is False


def test_nothing_collected_is_empty_and_not_ok() -> None:
    result = gate(exit_code=0, passed=0, skipped=0)

    assert result.status == "EMPTY"
    assert result.ok is False


def test_real_passes_are_pass() -> None:
    result = gate(exit_code=0, passed=5, skipped=1)

    assert result.status == "PASS"
    assert result.ok is True


def test_plain_failure_fails_and_is_not_blocked() -> None:
    result = gate(exit_code=1, passed=2, blocked_evidence="")

    assert result.status == "FAIL"
    assert result.status != "BLOCKED"
    assert result.ok is False


def test_blocking_evidence_turns_failure_into_blocked() -> None:
    evidence = "ModuleNotFoundError: No module named 'playwright'"
    result = gate(exit_code=1, passed=0, blocked_evidence=evidence)

    assert result.status == "BLOCKED"
    assert result.ok is False
    assert result.blocked_evidence
    assert evidence in result.blocked_evidence


def test_exit_code_uses_every_result_not_only_failures() -> None:
    passing = gate(name="pytest", exit_code=0, passed=4)
    empty = gate(name="security", exit_code=0, passed=0, skipped=0)

    assert compute_exit_code([passing]) == 0
    assert compute_exit_code([passing, empty]) != 0


def test_console_and_both_reports_agree_on_status(tmp_path: Path) -> None:
    results = [
        gate(name="pytest", exit_code=0, passed=3),
        gate(name="pytest-skip", exit_code=0, passed=0, skipped=2),
        gate(name="pytest-empty", exit_code=0, passed=0),
        gate(
            name="mypy",
            kind="command",
            exit_code=1,
            blocked_evidence="No module named 'pydantic'",
        ),
        gate(name="ruff", kind="command", exit_code=1),
        gate(name="browser", ran=False),
    ]

    json_path, md_path = write_report(tmp_path, mode="all", stamp=STAMP, results=results)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    reported = {str(step["name"]): str(step["status"]) for step in payload["steps"]}
    md_rows = {
        line.split("|")[1].strip(): line.split("|")[2].strip()
        for line in md_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("| ") and line.count("|") >= 8
    }

    for result in results:
        expected = result.status
        assert expected in {"PASS", "SKIP", "EMPTY", "BLOCKED", "FAIL", "NOT_RUN"}
        assert reported[result.name] == expected
        assert md_rows[result.name] == expected
        assert summary_line(result).strip().startswith(expected)
    assert compute_exit_code(results) != 0
    assert payload["failed"] == ["pytest-skip", "pytest-empty", "mypy", "ruff", "browser"]


def test_pytest_exit_five_means_empty_not_fail() -> None:
    step = _pytest(["-q"])
    assert step.kind == "test"
    assert 5 in step.empty_exit_codes

    collected_nothing = gate(
        exit_code=5,
        passed=0,
        skipped=0,
        empty_exit_codes=PYTEST_EMPTY_EXIT_CODES,
        evidence="no tests ran in 0.01s",
    )
    assert collected_nothing.status == "EMPTY"
    assert collected_nothing.ok is False
    assert collected_nothing.evidence

    usage_error = gate(exit_code=4, passed=None, empty_exit_codes=PYTEST_EMPTY_EXIT_CODES)
    assert usage_error.status == "FAIL"


def test_not_run_participates_in_exit_code() -> None:
    passing = gate(name="pytest", exit_code=0, passed=1)
    queued = gate(name="browser", ran=False)

    assert queued.status == "NOT_RUN"
    assert queued.ok is False
    assert compute_exit_code([passing, queued]) != 0


def test_missing_executable_is_blocked_not_a_traceback(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-a-real-tool"
    result = _run(Step("ghost", [str(missing)]), tmp_path, {})

    assert result.status == "BLOCKED"
    assert result.ok is False
    assert result.blocked_evidence
