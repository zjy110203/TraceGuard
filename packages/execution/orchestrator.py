"""Stage-3 execution orchestrator — in-process worker pool + SSE event bus.

HONEST SUBSTITUTION NOTICE
--------------------------
v1.1 stage 3 specifies Redis + Celery + SSE + PostgreSQL. This environment has
no Redis/Celery installed and no Docker daemon (verified at session start).
Rather than fabricate a Celery integration, this module implements the SAME
observable orchestration semantics with a bounded in-process ThreadPoolExecutor:

  * async submit -> QUEUED -> PREPARING -> RUNNING -> FINALIZING -> terminal;
  * every attempt is a distinct append-only Run;
  * timeout enforced by a deadline checked between steps;
  * cancellation via a cooperative flag checked between steps;
  * heartbeat timestamps updated per step (restart detection = stale heartbeat);
  * at-least-once SSE events with monotonic sequence + idempotency keys;
  * worker identity is explicit and changes after restart (fencing analogue).

This is a real, runnable implementation, NOT a mock. It is documented as a
dev/test substitution for Celery/Redis; production deployment still requires
the approved external broker. Do not claim Celery/Redis are integrated.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from packages.core.logging import get_logger
from packages.execution.broker import Broker, Dispatcher, ExecutionJob, InProcessBroker
from packages.execution.locators import LocatorRegistry
from packages.execution.models import TestCaseVersion, load_case
from packages.execution.runner import ExecutionAborted, LocalRunner
from packages.execution.tasks import RunService, RunStatus, RunView, TaskService

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunEvent:
    """C05-shaped event pushed to SSE subscribers (at-least-once)."""

    event_id: str
    event_type: str
    run_id: str
    task_id: str
    sequence: int
    occurred_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """Fan-out bus with per-subscriber queues and a monotonic sequence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[RunEvent]] = []
        self._sequence = 0
        self._history: list[RunEvent] = []

    def subscribe(self) -> queue.Queue[RunEvent]:
        q: queue.Queue[RunEvent] = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[RunEvent]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event_type: str, run_id: str, task_id: str, **payload: Any) -> RunEvent:
        with self._lock:
            self._sequence += 1
            event = RunEvent(
                event_id=f"runevt_{uuid.uuid4().hex[:12]}",
                event_type=event_type,
                run_id=run_id,
                task_id=task_id,
                sequence=self._sequence,
                occurred_at=_now(),
                payload=dict(payload),
            )
            self._history.append(event)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # Slow consumer: drop for this subscriber only. At-least-once
                    # replay is available from history (consumer replay cursor).
                    pass
        return event

    def history(self, after_sequence: int = 0) -> list[RunEvent]:
        with self._lock:
            return [e for e in self._history if e.sequence > after_sequence]


class Orchestrator:
    """Submits Runs, drives them to terminal states and publishes events."""

    def __init__(
        self,
        *,
        session_factory: Any,
        case_dir: Path,
        teamflow_base_url: str,
        eval_token: str,
        max_workers: int = 2,
        locators: LocatorRegistry | None = None,
        trace_dir: Path | None = None,
        reset_before_run: bool = True,
        broker: Broker | None = None,
    ) -> None:
        self.tasks = TaskService(session_factory)
        self.runs = RunService(session_factory)
        self.case_dir = case_dir
        self.base_url = teamflow_base_url
        self.eval_token = eval_token
        self.locators = locators
        self.trace_dir = trace_dir
        self.reset_before_run = reset_before_run
        self.bus = EventBus()
        self.worker_id = f"inproc-{uuid.uuid4().hex[:8]}"
        self.worker_count = max_workers
        # Dispatch goes through a broker so at-least-once delivery, visibility
        # timeout, duplicate suppression and dead letters are explicit rather
        # than implied by an executor's internal queue.
        self.broker: Broker = broker or InProcessBroker()
        self.dispatcher = Dispatcher(
            self.broker,
            self._handle_job,
            is_settled=self._is_settled,
            workers=max_workers,
        )
        self.dispatcher.start()
        self._start_times: dict[str, float] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        task_id: str,
        *,
        case_file: str | None = None,
        timeout_s: float = 60.0,
    ) -> RunView:
        """Create a NEW Run and schedule it. Never reuses an existing Run."""
        task = self.tasks.get(task_id)
        case_path = self.case_dir / (case_file or f"{task['case_id']}.json")
        case: TestCaseVersion = load_case(case_path)
        run_id = f"run_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:6]}"
        view = self.runs.create_run(
            run_id=run_id,
            task_id=task_id,
            case_id=case.case_id,
            case_version=case.version,
            timeout_s=timeout_s,
            worker_id=self.worker_id,
        )
        with self._lock:
            self._start_times[run_id] = time.monotonic()
        self.bus.publish("run.queued", run_id, task_id, attempt=view.attempt)
        job = ExecutionJob(
            job_id=run_id,
            run_id=run_id,
            task_id=task_id,
            case_id=case.case_id,
            case_file=case_path.name,
            timeout_s=timeout_s,
            attempt=view.attempt,
            created_at=_now(),
        )
        if not self.broker.enqueue(job):
            # A duplicate enqueue means this run was already dispatched; the
            # Run row is the authority, so this is a no-op rather than a rerun.
            logger.warning("duplicate dispatch suppressed for %s", run_id)
        return view

    def _handle_job(self, job: ExecutionJob) -> None:
        """Dispatcher entry point: reload the case, then execute."""
        case_path = self.case_dir / job.case_file
        case: TestCaseVersion = load_case(case_path)
        self._execute(job.run_id, job.task_id, case)

    def _is_settled(self, job: ExecutionJob) -> bool:
        """True when a redelivered job has already reached a terminal state."""
        try:
            return self.runs.get(job.run_id).status in RunStatus.TERMINAL
        except Exception:  # noqa: BLE001 - unknown run must not be re-executed blindly
            return False

    def shutdown(self, wait: bool = True) -> None:
        """Stop dispatching. Runs already executing finish; queued ones stay queued.

        ``wait`` preserves the executor-era signature so existing callers keep working.
        """
        self.dispatcher.stop(timeout=10.0 if wait else 0.1)

    def _abort_hook(self, run_id: str, task_id: str, timeout_s: float):
        def check() -> None:
            if self.runs.is_cancel_requested(run_id):
                self.bus.publish("run.cancelled", run_id, task_id)
                raise ExecutionAborted("cancel requested")
            started = self._start_times.get(run_id, time.monotonic())
            if time.monotonic() - started > timeout_s:
                self.bus.publish("run.timeout", run_id, task_id)
                raise ExecutionAborted("timeout exceeded")
            self.runs.heartbeat(run_id, self.worker_id)

        return check

    def _execute(self, run_id: str, task_id: str, case: TestCaseVersion) -> None:
        try:
            self.runs.transition(run_id, RunStatus.PREPARING, started_at=_now())
            self.bus.publish("run.preparing", run_id, task_id, case=case.case_id)
            if self.reset_before_run:
                # Repeatable experiment precondition: clear faults + reset data
                # before every run (stage 1 requirement). Best-effort; a failed
                # reset surfaces as a run failure, never a false PASS.
                try:
                    with httpx.Client(base_url=self.base_url, timeout=10, trust_env=False) as c:
                        h = {"X-Eval-Token": self.eval_token}
                        c.post("/__eval__/faults/clear", headers=h)
                        c.post("/__eval__/data/reset", headers=h)
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"pre-run reset failed: {exc}") from exc
            self.runs.transition(run_id, RunStatus.RUNNING)
            self.bus.publish("run.running", run_id, task_id, attempt=self.runs.get(run_id).attempt)

            view = self.runs.get(run_id)
            runner = LocalRunner(
                self.base_url,
                locators=self.locators,
                trace_dir=self.trace_dir,
                eval_token=self.eval_token,
            )
            report = runner.run(
                case,
                abort_hook=self._abort_hook(run_id, task_id, view.timeout_s),
                run_id=run_id,  # report must carry the canonical Run id (C04)
            )
            self.runs.append_steps(run_id, [s.to_dict() for s in report.steps])

            self.runs.transition(run_id, RunStatus.FINALIZING)
            self.bus.publish("run.finalizing", run_id, task_id, verdict=report.test_verdict)

            cancelled = bool(report.aborted_reason and "cancel" in report.aborted_reason.lower())
            timed_out = bool(report.aborted_reason and "timeout" in report.aborted_reason.lower())
            if cancelled:
                status = RunStatus.CANCELLED
            elif timed_out:
                status = RunStatus.TIMED_OUT
            elif report.execution_status.value == "COMPLETED":
                status = RunStatus.COMPLETED
            else:
                status = RunStatus.FAILED

            final = self.runs.finalize(
                run_id,
                status=status,
                execution_status=report.execution_status.value,
                test_verdict=report.test_verdict,
                report=report.to_dict(),
                error=report.aborted_reason,
            )
            self.bus.publish(
                "run.finished",
                run_id,
                task_id,
                status=final.status,
                verdict=final.test_verdict,
            )
        except Exception as exc:  # noqa: BLE001 - orchestrator must finalize on any error
            logger.error("run_execution_error: %s: %s", run_id, exc)
            try:
                current = self.runs.get(run_id)
                if current.status not in (RunStatus.TERMINAL,):
                    self.runs.finalize(
                        run_id,
                        status=RunStatus.FAILED,
                        execution_status="FAILED",
                        test_verdict="NOT_EVALUATED",
                        report={"error": f"{type(exc).__name__}: {exc}"[:1000]},
                        error=f"{type(exc).__name__}: {exc}"[:1000],
                    )
                    self.bus.publish(
                        "run.finished", run_id, task_id, status="FAILED", error=str(exc)
                    )
            except Exception:  # noqa: BLE001
                logger.exception("run_finalize_failed: %s", run_id)

    def cancel(self, run_id: str) -> RunView:
        view = self.runs.request_cancel(run_id)
        if view.status not in RunStatus.TERMINAL:
            self.bus.publish("run.cancel_requested", run_id, view.task_id)
        return view

    def history(self, task_id: str) -> list[RunView]:
        return self.runs.history(task_id)
