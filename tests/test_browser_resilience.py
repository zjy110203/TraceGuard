"""R5-4.9: a browser that dies must not be reported as a business failure.

Historical CI failure: ``Chromium Page.screenshot: Protocol error`` at
``test_each_case_runs_independently[3]``. The old code let the protocol error
propagate out of evidence capture, so an action that had already succeeded was
recorded as a FAILED step — and on a dead page every later step spent its full
timeout before failing with a transport message.

The contract pinned here:

1. a screenshot/dom capture error is an EVIDENCE GAP, not a step failure;
2. a closed/crashed page makes steps BLOCKED (no observation published), so the
   Oracle cannot resolve them as "observed false";
3. the run is marked as not-completed, so the verdict is INCONCLUSIVE /
   NOT_EVALUATED — never PASSED and never a fabricated business FAILED;
4. teardown is reported as its own structured dimension (``CleanupStatus``):
   a browser that will not close, or a trace that will not stop, is visible in
   the report and logged, but it never rewrites the business verdict and never
   escapes the ``with`` block.
"""

from __future__ import annotations

import http.server
from pathlib import Path

import pytest

from packages.execution.browser import BrowserManager, StepExecution
from packages.execution.events import EventCollector
from packages.execution.models import (
    AssertionSpec,
    OracleBinding,
    OracleSpec,
    TestCaseVersion,
    TestIntent,
    TestStep,
)
from packages.execution.runner import ExecutionAborted, LocalRunner
from packages.execution.verdict import ExecutionStatus
from tests.netutil import serve_http

for _model in (TestCaseVersion, TestIntent, TestStep):
    _model.__test__ = False

PAGE = """<html><body>
  <button id="btn" onclick="document.getElementById('done').style.display='block'">go</button>
  <div id="done" style="display:none">done</div>
</body></html>"""


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _collector() -> EventCollector:
    return EventCollector("run_res", "test")


def _oracle(assertion_id: str, source_ref: str) -> OracleBinding:
    return OracleBinding(
        assertion_id=assertion_id,
        primary=OracleSpec(
            oracle_id=f"o-{assertion_id}",
            source_type="ui",
            source_ref=source_ref,
            expected_value=True,
            approved_by="human",
            approved=True,
            source="test",
        ),
    )


def _case() -> TestCaseVersion:
    return TestCaseVersion(
        case_id="TC-RESILIENCE-001",
        version=1,
        title="local page smoke",
        intent=TestIntent(intent_id="i-res", description="click and see done"),
        steps=(
            TestStep("s1", "navigate", "/"),
            TestStep("s2", "click", "#btn"),
            TestStep("s3", "assert_visible", "#done"),
        ),
        assertions=(
            AssertionSpec("a1", "navigation succeeded", required=True),
            AssertionSpec("a2", "click succeeded", required=True),
        ),
        oracle_bindings=(_oracle("a1", "ui.s1.passed"), _oracle("a2", "ui.s2.passed")),
        compiled_from="manual",
        approved_by="human",
    )


def test_screenshot_error_is_an_evidence_gap_not_a_step_failure() -> None:
    collector = _collector()
    with serve_http(_PageHandler) as base:
        with BrowserManager(base, collector) as browser:
            assert browser.page is not None

            def _boom(*_args: object, **_kwargs: object) -> bytes:
                # A capture failure that is NOT the target disappearing.
                raise RuntimeError("Protocol error (Page.screenshot): Unable to capture")

            browser.page.screenshot = _boom  # type: ignore[method-assign]
            result = browser.execute_step(TestStep("s1", "navigate", f"{base}/"), "run_res")

            assert result.ok, result.detail
            assert result.blocked == ""
            evidence_types = [e.evidence_type for e in browser.evidence()]
            assert "screenshot" not in evidence_types  # honest: nothing was captured
            assert "dom_snapshot" in evidence_types  # the rest of the capture still ran
            assert not browser.page_crashed  # a capture glitch is not a crash

    events = [e["event_type"] for e in collector.to_dicts()]
    assert "evidence.screenshot.failed" in events
    assert "step.error" not in events
    # The gap is structured, and it is NOT a cleanup defect: the browser was
    # healthy all along, only one capture failed.
    assert browser.cleanup.evidence_errors
    assert any("screenshot" in problem for problem in browser.cleanup.problems())
    assert browser.cleanup.page_crashed is False
    assert browser.cleanup.is_clean


def test_closed_page_blocks_steps_instead_of_burning_timeouts() -> None:
    collector = _collector()
    with serve_http(_PageHandler) as base:
        with BrowserManager(base, collector) as browser:
            first = browser.execute_step(TestStep("s1", "navigate", f"{base}/"), "run_res")
            assert first.ok, first.detail

            assert browser.browser is not None
            browser.browser.close()  # the page is gone mid-run

            blocked = browser.execute_step(TestStep("s2", "click", "#btn"), "run_res")
            assert not blocked.ok
            assert blocked.blocked, "a step that could not run must be marked blocked"
            assert "unavailable" in blocked.blocked or "closed" in blocked.blocked
            assert browser.page_crashed

            again = browser.execute_step(TestStep("s3", "assert_visible", "#done"), "run_res")
            assert again.blocked, "every later step must also be blocked"

    events = [e["event_type"] for e in collector.to_dicts()]
    assert "browser.page.crashed" in events
    assert events.count("step.blocked") == 2
    # The lost page is structured too, and the resource teardown itself was
    # clean (the browser handle closed twice without a teardown error).
    assert browser.cleanup.page_crashed is True
    assert browser.cleanup.page_crash_detail
    assert browser.cleanup.is_clean


def test_crashed_run_is_not_a_passed_or_failed_business_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_execute = BrowserManager.execute_step
    calls = {"n": 0}

    def _crash_after_first(self: BrowserManager, step: TestStep, run_id: str) -> StepExecution:
        result = real_execute(self, step, run_id)
        calls["n"] += 1
        if calls["n"] == 1 and self.browser is not None:
            self.browser.close()  # simulate the renderer vanishing after step 1
        return result

    monkeypatch.setattr(BrowserManager, "execute_step", _crash_after_first)

    with serve_http(_PageHandler) as base:
        runner = LocalRunner(base, report_dir=tmp_path / "reports")
        report = runner.run(_case())

    assert report.execution_status is ExecutionStatus.FAILED
    assert report.test_verdict in {"INCONCLUSIVE", "NOT_EVALUATED"}, report.test_verdict
    assert report.test_verdict != "PASSED"
    assert report.test_verdict != "FAILED"

    blocked_steps = [s for s in report.steps if s.detail.startswith("BLOCKED")]
    assert blocked_steps, "the crash must be visible per step"
    # a1 was genuinely observed, a2 never was — that distinction is the point
    results = {r["assertion_id"]: r for r in report.assertion_results}
    assert results["a1"]["verdict"] == "PASSED"
    assert results["a2"]["verdict"] != "FAILED"
    event_types = [e["event_type"] for e in report.events]
    assert "run.browser_crashed" in event_types
    # A renderer crash is an execution fact, not a teardown defect: it is
    # reported in its own field and must not be relabelled "cleanup degraded".
    assert report.cleanup["page_crashed"] is True
    assert report.cleanup["page_crash_detail"]
    assert report.cleanup["is_clean"] is True
    assert "run.cleanup.degraded" not in event_types


def test_healthy_run_of_the_same_case_still_passes(tmp_path: Path) -> None:
    """Control: the crash above is the only reason the verdict changed."""
    with serve_http(_PageHandler) as base:
        runner = LocalRunner(base, report_dir=tmp_path / "reports")
        report = runner.run(_case())

    assert report.execution_status is ExecutionStatus.COMPLETED
    assert report.test_verdict == "PASSED", report.verdict_notes
    # The control run must also be structurally clean — otherwise the cleanup
    # assertions above would be vacuous.
    assert report.cleanup["is_clean"] is True
    assert report.cleanup["problems"] == []
    assert report.to_dict()["cleanup"]["is_clean"] is True
    assert "run.cleanup.degraded" not in [e["event_type"] for e in report.events]


def test_clean_run_leaves_a_clean_cleanup_status(tmp_path: Path) -> None:
    """Scenario 1: every resource is released and the status says so."""
    with serve_http(_PageHandler) as base:
        runner = LocalRunner(base, report_dir=tmp_path / "reports")
        report = runner.run(_case())

    cleanup = report.cleanup
    assert cleanup["is_clean"] is True
    assert cleanup["problems"] == []
    assert cleanup["browser_closed"] is True
    assert cleanup["browser_close_error"] == ""
    assert cleanup["trace_stopped"] is True  # no trace requested: nothing running
    assert cleanup["playwright_stopped"] is True
    assert cleanup["playwright_stop_error"] == ""
    assert cleanup["page_crashed"] is False
    assert cleanup["execution_cancelled"] is False
    assert report.execution_status is ExecutionStatus.COMPLETED
    assert report.test_verdict == "PASSED", report.verdict_notes


def test_browser_close_failure_is_visible_but_never_changes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 2: a browser that will not close is reported, not fatal."""

    def _close_boom(self: BrowserManager) -> None:
        raise RuntimeError("close boom")

    monkeypatch.setattr(BrowserManager, "_close_browser", _close_boom)

    with serve_http(_PageHandler) as base:
        runner = LocalRunner(base, report_dir=tmp_path / "reports")
        report = runner.run(_case())  # must NOT raise

    cleanup = report.cleanup
    assert "close boom" in cleanup["browser_close_error"]
    assert cleanup["browser_closed"] is False
    assert cleanup["is_clean"] is False
    assert any("browser_close" in problem for problem in cleanup["problems"])
    assert cleanup["playwright_stopped"] is True, "one failure must not block the next step"

    events = report.events
    event_types = [e["event_type"] for e in events]
    assert "browser.close.failed" in event_types
    assert "run.cleanup.degraded" in event_types
    degraded = next(e for e in events if e["event_type"] == "run.cleanup.degraded")
    assert degraded["payload"]["problems"] == cleanup["problems"]

    # Business result untouched: a teardown defect cannot fail a passing test.
    assert report.execution_status is ExecutionStatus.COMPLETED
    assert report.test_verdict == "PASSED", report.verdict_notes
    assert any("cleanup degraded" in note for note in report.verdict_notes)


def test_trace_stop_failure_is_a_reported_cleanup_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 4: a trace that will not stop costs the evidence, not the run."""

    def _trace_boom(self: BrowserManager) -> None:
        raise RuntimeError("trace boom")

    monkeypatch.setattr(BrowserManager, "_stop_trace", _trace_boom)

    with serve_http(_PageHandler) as base:
        runner = LocalRunner(base, report_dir=tmp_path / "reports", trace_dir=tmp_path / "traces")
        report = runner.run(_case())

    cleanup = report.cleanup
    assert "trace boom" in cleanup["trace_stop_error"]
    assert cleanup["trace_stopped"] is False
    assert cleanup["browser_closed"] is True, "a trace failure must not keep the browser alive"
    assert cleanup["is_clean"] is False

    evidence_types = [e["evidence_type"] for e in report.evidence]
    assert "playwright_trace" not in evidence_types
    event_types = [e["event_type"] for e in report.events]
    assert "evidence.trace.failed" in event_types
    assert "run.cleanup.degraded" in event_types

    assert report.execution_status is ExecutionStatus.COMPLETED
    assert report.test_verdict == "PASSED", report.verdict_notes


def test_cleanup_failures_never_escape_the_with_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Scenario 6: close AND playwright.stop fail — the report still survives."""
    real_stop = BrowserManager._stop_playwright

    def _close_boom(self: BrowserManager) -> None:
        raise RuntimeError("close boom")

    def _stop_boom(self: BrowserManager, exc: tuple[object, ...]) -> None:
        # Release the real driver first, then fail: the point is that the
        # recorded error cannot escape, not that a process is leaked.
        real_stop(self, exc)
        raise RuntimeError("playwright boom")

    monkeypatch.setattr(BrowserManager, "_close_browser", _close_boom)
    monkeypatch.setattr(BrowserManager, "_stop_playwright", _stop_boom)

    with serve_http(_PageHandler) as base:
        runner = LocalRunner(base, report_dir=tmp_path / "reports")
        report = runner.run(_case())  # the ``with`` exit must not raise

    cleanup = report.cleanup
    assert "close boom" in cleanup["browser_close_error"]
    assert "playwright boom" in cleanup["playwright_stop_error"]
    assert cleanup["is_clean"] is False
    assert len(cleanup["problems"]) >= 2
    assert report.execution_status is ExecutionStatus.COMPLETED
    assert report.test_verdict == "PASSED", report.verdict_notes
    event_types = [e["event_type"] for e in report.events]
    assert "browser.close.failed" in event_types
    assert "browser.playwright.failed" in event_types

    captured = capsys.readouterr()
    assert "Exception ignored" not in captured.out + captured.err


def test_cancelled_run_is_reported_without_a_fake_pass(tmp_path: Path) -> None:
    """Scenario 7: an abort is an execution fact, visible in the cleanup status."""

    def _always_abort() -> None:
        raise ExecutionAborted("operator cancelled")

    with serve_http(_PageHandler) as base:
        runner = LocalRunner(base, report_dir=tmp_path / "reports")
        report = runner.run(_case(), abort_hook=_always_abort)

    cleanup = report.cleanup
    assert cleanup["execution_cancelled"] is True
    assert "operator cancelled" in cleanup["cancellation_reason"]
    # Cancellation explains why the run stopped; it is not a teardown defect.
    assert cleanup["is_clean"] is True
    assert report.execution_status is ExecutionStatus.FAILED
    assert report.test_verdict != "PASSED"
    event_types = [e["event_type"] for e in report.events]
    assert "run.cancelled" in event_types
    assert "run.cleanup.degraded" not in event_types
