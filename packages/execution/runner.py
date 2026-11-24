"""Local synchronous execution kernel (stage 2) — no Agent, no async platform.

Flow: load TestCaseVersion -> create context -> Playwright steps -> AUTO-COLLECT
observations from independent state API + browser -> evaluate Oracles -> aggregate
Verdict -> emit Run/Step report.

Four result dimensions stay separate: execution_status describes the process,
test_verdict describes correctness; diagnosis fields are left NOT_STARTED
(they belong to stage 7). Crucially, observations are collected by the runner
AFTER steps execute — they are never injected by the caller — so the Oracle
sees real post-execution state.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from packages.core.logging import get_logger
from packages.execution.browser import BrowserManager, StepExecution
from packages.execution.dataref import (
    NullTestDataProvider,
    TestDataProvider,
    is_placeholder,
    resolve_step_value,
)
from packages.execution.events import EventCollector
from packages.execution.locators import LocatorRegistry
from packages.execution.models import CleanupPolicy, TestCaseVersion, TestStep
from packages.execution.oracle import OracleEvaluator
from packages.execution.verdict import (
    AssertionResult,
    AssertionVerdict,
    ExecutionStatus,
    VerdictOutcome,
    aggregate_verdict,
)

logger = get_logger(__name__)


class ExecutionAborted(Exception):
    """Raised by an orchestrator abort hook to stop a run between steps."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Called before each step; the orchestrator checks cancel flags / deadlines.
AbortHook = Callable[[], None]


@dataclass
class StepReport:
    step_id: str
    action: str
    ok: bool
    detail: str
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence_ids"] = list(self.evidence_ids)
        return d


@dataclass
class RunReport:
    run_id: str
    case_id: str
    case_version: int
    started_at: str
    finished_at: str = ""
    execution_status: ExecutionStatus = ExecutionStatus.RUNNING
    test_verdict: str = ""
    diagnosis_status: str = "NOT_STARTED"
    diagnosis_outcome: str = "NOT_STARTED"
    steps: list[StepReport] = field(default_factory=list)
    assertion_results: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    verdict_notes: tuple[str, ...] = ()
    evidence: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    aborted_reason: str = ""
    # R5-4.1: a run whose sensitive inputs could not be bound is BLOCKED, not
    # "failed for an unknown reason" and certainly not "passed".
    blocked: bool = False
    blocked_reason: str = ""
    # Teardown outcome (browser/trace/playwright), reported separately from the
    # business verdict: see ``CleanupStatus``. Additive field — old readers that
    # ignore it are unaffected.
    cleanup: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_dict() for s in self.steps]
        d["verdict_notes"] = list(self.verdict_notes)
        return d


class LocalRunner:
    """Runs a TestCaseVersion synchronously and produces a RunReport."""

    def __init__(
        self,
        base_url: str,
        *,
        report_dir: Path | None = None,
        eval_token: str = "local-dev-eval-token-change-me",
        session_cookie: tuple[str, str] = ("tf_session", "tester"),
        locators: LocatorRegistry | None = None,
        trace_dir: Path | None = None,
        data_provider: TestDataProvider | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.report_dir = report_dir
        self.eval_token = eval_token
        self.session_cookie = session_cookie
        self.locators = locators
        self.trace_dir = trace_dir
        # Resolves ``value_ref`` secrets. The null provider is the safe default:
        # an unbound reference blocks the run instead of guessing a password.
        self.data_provider: TestDataProvider = data_provider or NullTestDataProvider()
        if report_dir:
            report_dir.mkdir(parents=True, exist_ok=True)

    def bind_steps(self, case: TestCaseVersion) -> tuple[list[TestStep], list[str]]:
        """Resolve every referenced input *before* a browser starts.

        Returns (steps-with-real-values, blocking problems). A placeholder or an
        unresolved ref is a hard problem: replaying ``«redacted»`` as a password
        is exactly the R5-3 defect this replaces.
        """
        bound: list[TestStep] = []
        problems: list[str] = []
        for step in case.steps:
            if not step.value_ref and not is_placeholder(step.value):
                bound.append(step)
                continue
            resolved = resolve_step_value(
                step_id=step.step_id,
                value=step.value,
                value_ref=step.value_ref,
                provider=self.data_provider,
            )
            if not resolved.ok:
                problems.append(resolved.reason)
                bound.append(step)
                continue
            bound.append(replace(step, value=resolved.value, value_ref=step.value_ref))
        return bound, problems

    def _blocked_report(
        self,
        case: TestCaseVersion,
        run_id: str,
        started: str,
        collector: EventCollector,
        reason: str,
    ) -> RunReport:
        """Honest terminal state: nothing executed, nothing evaluated, no PASS."""
        report = RunReport(
            run_id=run_id,
            case_id=case.case_id,
            case_version=case.version,
            started_at=started,
            blocked=True,
            blocked_reason=reason[:500],
        )
        collector.emit("run.blocked", payload={"reason": report.blocked_reason})
        results: list[AssertionResult] = []
        for binding in case.oracle_bindings:
            required = next(
                (a.required for a in case.assertions if a.assertion_id == binding.assertion_id),
                True,
            )
            results.append(
                AssertionResult(
                    assertion_id=binding.assertion_id,
                    required=required,
                    verdict=AssertionVerdict.NOT_EVALUATED,
                    oracle_available=False,
                    evaluated=False,
                    reason=f"BLOCKED: {report.blocked_reason}",
                )
            )
        outcome = aggregate_verdict(results, execution_completed=False)
        report.execution_status = ExecutionStatus.FAILED
        report.test_verdict = outcome.verdict.value
        report.assertion_results = [asdict(r) for r in results]
        report.verdict_notes = outcome.notes
        report.finished_at = datetime.now(UTC).isoformat()
        report.events = collector.to_dicts()
        self._persist(report)
        return report

    def _independent_state(self) -> dict[str, Any]:
        """Read the business-plane independent state API (Oracle source).

        This is NOT the eval control plane and never exposes Ground Truth.
        """
        try:
            resp = httpx.get(
                f"{self.base_url}/api/v1/state/independent",
                cookies={self.session_cookie[0]: self.session_cookie[1]},
                timeout=10,
                trust_env=False,
            )
            if resp.status_code == 200:
                return {"available": True, "value": resp.json()}
            return {"available": False}
        except Exception:  # noqa: BLE001
            return {"available": False}

    def _build_source_reader(self, ui_observations: dict[str, Any]):
        """Bind logical source refs to real observations collected this run."""

        def read(ref: str) -> Any | None:
            if ref.startswith("state."):
                state = self._independent_state()
                if not state["available"]:
                    return None
                key = ref.split(".", 1)[1]
                value = state["value"]
                return {"value": value.get(key)} if isinstance(value, dict) else None
            if ref.startswith("ui."):
                key = ref.split(".", 1)[1]
                return ui_observations.get(key)
            return None

        return read

    def run(
        self,
        case: TestCaseVersion,
        *,
        execution_completed: bool | None = None,
        abort_hook: AbortHook | None = None,
        run_id: str | None = None,
    ) -> RunReport:
        """Execute one version and return its report. Never mutates ``case``.

        ``run_id`` may be supplied by an orchestrator so the persisted report
        carries the SAME id as the canonical Run row (traceability C04). When
        omitted, a local id is generated for standalone CLI use.
        """
        run_id = run_id or f"run_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}_{case.case_id}"
        started = datetime.now(UTC).isoformat()
        collector = EventCollector(run_id, "local_runner")
        collector.emit("run.started", payload={"case": case.case_id, "version": case.version})

        steps, problems = self.bind_steps(case)
        if problems:
            # Nothing is executed with a placeholder or an unresolved secret.
            return self._blocked_report(case, run_id, started, collector, "; ".join(problems))

        report = RunReport(
            run_id=run_id,
            case_id=case.case_id,
            case_version=case.version,
            started_at=started,
        )

        ui_observations: dict[str, Any] = {}
        with BrowserManager(
            self.base_url,
            collector,
            locators=self.locators,
            trace_dir=self.trace_dir,
            run_id=run_id,
        ) as browser:
            for step in steps:
                if abort_hook is not None:
                    try:
                        abort_hook()
                    except ExecutionAborted as exc:
                        # Stop deterministically between steps. The run is
                        # finalized below with an independent CANCELLED status.
                        report.aborted_reason = exc.reason
                        break
                result: StepExecution = browser.execute_step(step, run_id)
                # StepReport is a generated-contract shape (C05), so a blocked
                # step is marked in ``detail`` rather than a new field — but it
                # is still never turned into a business observation below.
                report.steps.append(
                    StepReport(
                        step.step_id,
                        step.action,
                        result.ok,
                        f"BLOCKED: {result.blocked}" if result.blocked else result.detail,
                        result.evidence_ids,
                    )
                )
                if result.blocked:
                    # The page was gone before this step could run: publish no
                    # observation at all. Writing False here would let a browser
                    # crash masquerade as a confirmed business failure.
                    continue
                # Every step publishes its own outcome, not just assertions.
                #
                # The TestCase Compiler binds a compiled candidate's Oracle to
                # ``ui.<step>.passed`` on the explored click. If only assert_*
                # steps published observations, that Oracle could never resolve
                # and every compiled case would freeze at NOT_EVALUATED. The
                # claim stays narrow: "this explored action still succeeds",
                # which is exactly what the compiler says it verified.
                ui_observations[f"{step.step_id}.passed"] = result.ok
            browser.network_evidence(run_id)
            # Any request the page attempted against a non-approved origin is
            # part of the security record (R5-4.7 network gateway).
            browser.security_evidence(run_id)
            # Page-level observations: a business assertion may legitimately be
            # "we ended up on /projects", which no per-step flag can express.
            page = browser.page
            if page is not None:
                from urllib.parse import urlparse

                final_url = page.url
                ui_observations["url_path"] = urlparse(final_url).path or "/"
                ui_observations["final_url"] = final_url
                try:
                    ui_observations["page_title"] = page.title()
                except Exception:  # noqa: BLE001 - title is cosmetic
                    ui_observations["page_title"] = ""

        # Collect after the context closes: Playwright Trace evidence is
        # appended during BrowserManager.__exit__, so an in-block copy would
        # silently drop it.
        report.evidence.extend([e.to_dict() for e in browser.evidence()])

        # Cleanup accounting. The browser is gone, but nothing is lost:
        # ``collector.to_dicts()`` only runs at the end of this method, so every
        # teardown event emitted above (and by ``__exit__``) still lands in the
        # report. A degraded teardown is reported, never turned into a verdict:
        # a browser that will not close cannot make a passing test fail.
        if report.aborted_reason:
            browser.cleanup.mark_cancelled(report.aborted_reason)
            collector.emit(
                "run.cancelled",
                payload={"reason": browser.cleanup.cancellation_reason},
            )
        report.cleanup = browser.cleanup.to_dict()
        cleanup_note = ""
        if not browser.cleanup.is_clean:
            problems = browser.cleanup.problems()
            collector.emit("run.cleanup.degraded", payload={"problems": problems})
            cleanup_note = "cleanup degraded: " + "; ".join(problems)
            logger.warning("run_cleanup_degraded: %s", cleanup_note)

        completed = execution_completed if execution_completed is not None else True
        # An abort is an independent execution fact; it must never be masked
        # into a clean PASSED even if earlier assertions already evaluated.
        if report.aborted_reason:
            completed = False
        if browser.page_crashed:
            # A vanished page is an environment/execution failure, never a
            # silently "completed" run.
            completed = False
            collector.emit(
                "run.browser_crashed",
                payload={"detail": browser.crash_detail},
            )
        report.execution_status = ExecutionStatus.COMPLETED if completed else ExecutionStatus.FAILED

        oracle = OracleEvaluator(self._build_source_reader(ui_observations))
        assertion_results: list[AssertionResult] = []
        for binding in case.oracle_bindings:
            evaluation = oracle.evaluate(binding)
            required = next(
                (a.required for a in case.assertions if a.assertion_id == binding.assertion_id),
                True,
            )
            assertion_results.append(
                AssertionResult(
                    assertion_id=binding.assertion_id,
                    required=required,
                    verdict=evaluation.verdict,
                    oracle_available=evaluation.oracle_available,
                    oracle_conflict=evaluation.oracle_conflict,
                    evaluated=evaluation.evaluated,
                    reason="; ".join(evaluation.notes),
                )
            )
            collector.emit(
                "oracle.evaluated",
                step_id=binding.assertion_id,
                payload={"verdict": evaluation.verdict.value, "notes": list(evaluation.notes)},
            )

        report.observations = {
            "ui": ui_observations,
            "independent_state": self._independent_state().get("value"),
        }
        outcome: VerdictOutcome = aggregate_verdict(
            assertion_results, execution_completed=completed
        )
        report.test_verdict = outcome.verdict.value
        report.assertion_results = [asdict(r) for r in assertion_results]
        report.verdict_notes = outcome.notes
        if cleanup_note:
            # Tuples are immutable: rebuild rather than append. The note is
            # advisory and never rewrites the verdict.
            report.verdict_notes = (*report.verdict_notes, cleanup_note)
        report.finished_at = datetime.now(UTC).isoformat()
        report.events = collector.to_dicts()

        self._persist(report)
        return report

    def _persist(self, report: RunReport) -> None:
        if not self.report_dir:
            return
        path = self.report_dir / f"{report.run_id}.json"
        path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("run_report_written: %s", path)

    def cleanup(self, policy: CleanupPolicy) -> bool:
        """Best-effort state reset via eval control plane (harness-only)."""
        try:
            resp = httpx.post(
                f"{self.base_url}{policy.reset_endpoint}",
                headers={"X-Eval-Token": self.eval_token},
                timeout=10,
                trust_env=False,
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            logger.warning("cleanup_failed")
            return False
