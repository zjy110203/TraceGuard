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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from packages.core.logging import get_logger
from packages.execution.browser import BrowserManager, StepExecution
from packages.execution.events import EventCollector
from packages.execution.locators import LocatorRegistry
from packages.execution.models import CleanupPolicy, TestCaseVersion
from packages.execution.oracle import OracleEvaluator
from packages.execution.verdict import (
    AssertionResult,
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.report_dir = report_dir
        self.eval_token = eval_token
        self.session_cookie = session_cookie
        self.locators = locators
        self.trace_dir = trace_dir
        if report_dir:
            report_dir.mkdir(parents=True, exist_ok=True)

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
            for step in case.steps:
                if abort_hook is not None:
                    try:
                        abort_hook()
                    except ExecutionAborted as exc:
                        # Stop deterministically between steps. The run is
                        # finalized below with an independent CANCELLED status.
                        report.aborted_reason = exc.reason
                        break
                result: StepExecution = browser.execute_step(step, run_id)
                report.steps.append(
                    StepReport(
                        step.step_id, step.action, result.ok, result.detail, result.evidence_ids
                    )
                )
                # UI observations come from assert steps: their pass/fail is a
                # direct observation of what the browser actually saw.
                if step.action.startswith("assert_"):
                    ui_observations[f"{step.step_id}.passed"] = result.ok
            browser.network_evidence(run_id)

        # Collect after the context closes: Playwright Trace evidence is
        # appended during BrowserManager.__exit__, so an in-block copy would
        # silently drop it.
        report.evidence.extend([e.to_dict() for e in browser.evidence()])

        completed = execution_completed if execution_completed is not None else True
        # An abort is an independent execution fact; it must never be masked
        # into a clean PASSED even if earlier assertions already evaluated.
        if report.aborted_reason:
            completed = False
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
