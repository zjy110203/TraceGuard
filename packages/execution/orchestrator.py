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
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from packages.core.logging import get_logger
from packages.evidence.run_events import CursorResolution, RunEventRecorder
from packages.execution.broker import Broker, Dispatcher, ExecutionJob, InProcessBroker
from packages.execution.locators import LocatorRegistry
from packages.execution.models import TestCaseVersion, latest_case_path, load_case
from packages.execution.runner import ExecutionAborted, LocalRunner
from packages.execution.tasks import RunService, RunStatus, RunView, TaskService

logger = get_logger(__name__)

# A slow subscriber drops events; keeping every single dropped sequence would be
# an unbounded growth path, while keeping none makes the loss invisible. Ranges
# are coalesced and capped: the newest 100 are enough to explain a gap.
_MAX_DROP_RANGES = 100
# A durability outage can happen more than once in a process lifetime. Keeping
# every failed delivery would be unbounded; a bounded list of DISJOINT intervals
# is enough to explain each outage without ever claiming the successful writes
# between two outages were undurable (R5-5.1-R R4).
_MAX_UNDURABLE_INTERVALS = 100
_DEFAULT_QUEUE_MAXSIZE = 1000
# In-memory history is a fast-replay cache, not the system of record (that is
# the event store, when a recorder is attached). It is capped so a long soak
# cannot grow without bound.
_MAX_MEMORY_HISTORY = 5000


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunEvent:
    """C05-shaped event pushed to SSE subscribers (at-least-once).

    Two positions, deliberately separate (R5-5.1 defect: they were the same
    variable, so a degraded write consumed a durable number and the event the
    database later stored under that number could not be told apart from it):

    ``sequence``
        The **durable position** (``stream_seq``) assigned by the store. ``None``
        means "this event was never persisted", and is the honest answer: a
        number exists only when a row exists. A busy process counter must never
        stand in for it, because that number belongs to a future row.

    ``delivery_seq``
        The **wire ordinal**: a per-process counter bumped for every published
        event, durable or not. It drives SSE ordering, gap detection and
        subscriber drop accounting. ``0`` means "not on this process's wire"
        (a frame rebuilt from the durable log during replay), because live
        ordinals start at 1.
    """

    event_id: str
    event_type: str
    run_id: str
    task_id: str
    sequence: int | None
    occurred_at: str
    payload: dict[str, Any]
    delivery_seq: int = 0

    @property
    def durable(self) -> bool:
        """Whether this event has a position that can be resumed from later."""
        return self.sequence is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["durable"] = self.durable
        return data


class SubscriberQueue(queue.Queue[RunEvent]):
    """Per-subscriber queue that *records* overflow instead of hiding it.

    ``queue.Queue(maxsize=...)`` + ``put_nowait`` raises ``queue.Full`` and the
    old bus swallowed it, so a slow SSE client silently missed events with no
    count, no trace and no way for the consumer to notice. The queue now keeps
    a drop counter and coalesced sequence ranges so the stream can tell the
    client exactly which sequences it lost.

    No `RunEvent` field is added or changed: the loss signal travels as an SSE
    comment frame, which C05 consumers already ignore by spec.
    """

    def __init__(self, maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        super().__init__(maxsize=maxsize)
        self._drop_lock = threading.Lock()
        self._dropped = 0
        self._dropped_ranges: list[tuple[int, int]] = []

    def note_drop(self, sequence: int) -> None:
        """Record that ``sequence`` was dropped for this subscriber."""
        with self._drop_lock:
            self._dropped += 1
            if self._dropped_ranges and sequence == self._dropped_ranges[-1][1] + 1:
                self._dropped_ranges[-1] = (self._dropped_ranges[-1][0], sequence)
                return
            if len(self._dropped_ranges) >= _MAX_DROP_RANGES:
                self._dropped_ranges.pop(0)
            self._dropped_ranges.append((sequence, sequence))

    @property
    def dropped(self) -> int:
        """Total events this subscriber never received."""
        with self._drop_lock:
            return self._dropped

    @property
    def dropped_ranges(self) -> list[tuple[int, int]]:
        """Inclusive sequence ranges that were dropped, oldest first (bounded)."""
        with self._drop_lock:
            return list(self._dropped_ranges)

    def drop_snapshot(self) -> tuple[int, list[tuple[int, int]]]:
        """Consistent ``(dropped, ranges)`` pair for diagnostics."""
        with self._drop_lock:
            return self._dropped, list(self._dropped_ranges)


class EventBus:
    """Fan-out bus with per-subscriber queues and a monotonic sequence.

    R5-5.1: an optional :class:`RunEventRecorder` makes the stream durable. The
    bus keeps its fan-out role; the recorder supplies the order. When a recorder
    is attached:

    * ``sequence`` is the store-assigned ``stream_seq``, so it stays monotonic
      across a restart (a new process does not restart at 1);
    * the stream can be replayed after a restart;
    * a failed write is **never** reported as durable — the event is still
      delivered live (see ``persistence_failure_policy``) and the degradation is
      counted and exposed, so "the stream is fine" is never claimed on the
      strength of a write that did not happen.
    """

    def __init__(
        self,
        recorder: RunEventRecorder | None = None,
        *,
        strict_persistence: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[SubscriberQueue] = []
        # Durable high-water mark: the last *confirmed* stream position. It only
        # advances on a successful write, so it is safe to publish as a resume
        # cursor at any time.
        self._sequence = 0
        # Wire ordinal: advances on every publish, persisted or not. Never
        # written back into ``_sequence`` — that is precisely the bug this
        # counter exists to prevent.
        self._delivery_seq = 0
        self._history: list[RunEvent] = []
        # Aggregate loss across every subscriber ever served by this bus.
        self.dropped_total = 0
        self._recorder = recorder
        # ``strict`` raises on a failed write (gates/tests); ``degrade`` keeps
        # executing but reports the gap in durability.
        self.strict_persistence = strict_persistence
        self.producer_instance_id = (
            recorder.producer_instance_id
            if recorder is not None
            else f"bus-{uuid.uuid4().hex[:12]}"
        )
        self.persistence_failures = 0
        self.persistence_last_error = ""
        # Durability hole accounting, in the WIRE domain (delivery ordinals).
        self.first_undurable_delivery: int | None = None
        self.last_undurable_delivery: int | None = None
        self.undurable_count = 0
        self.last_durable_delivery: int | None = None
        # Disjoint undurable intervals in the WIRE domain (delivery ordinals).
        # The single first/last pair above stays for backward compatibility but
        # cannot represent two outages separated by a successful write: it would
        # mark the recovered deliveries in between as a continuous hole. Each
        # interval is {first, last, count, recovered_at}; bounded so a long soak
        # cannot grow it without limit. (R5-5.1-R R4)
        self.undurable_intervals: list[dict[str, Any]] = []
        self._interval_open = False
        # Bumped on every new failure epoch so a stream can tell "already
        # announced" from "degraded again just now".
        self.persistence_epoch = 0
        # Epoch in which a write succeeded again, i.e. durability was restored.
        self.recovered_epoch: int | None = None
        self.recovered_at_delivery: int | None = None
        self.startup_report: dict[str, Any] = {}
        if recorder is not None:
            self.startup_report = recorder.start()
            self._sequence = int(self.startup_report.get("latest", 0))

    def subscribe(self, maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> SubscriberQueue:
        """Register a subscriber queue. ``maxsize`` exists for tests/gateways."""
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        q = SubscriberQueue(maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[RunEvent]) -> None:
        with self._lock:
            for index, existing in enumerate(self._subscribers):
                if existing is q:
                    del self._subscribers[index]
                    break

    @property
    def subscriber_count(self) -> int:
        """Live SSE subscribers — used to prove disconnect cleanup leaks nothing."""
        with self._lock:
            return len(self._subscribers)

    def publish(self, event_type: str, run_id: str, task_id: str, **payload: Any) -> RunEvent:
        with self._lock:
            event_id = f"runevt_{uuid.uuid4().hex[:12]}"
            occurred_at = _now()
            blob = dict(payload)
            # The ordinal is computed but only committed once the event is
            # actually delivered: a strict-mode rejection must not burn a
            # number and open a phantom gap in every live stream.
            delivery_seq = self._delivery_seq + 1
            sequence = self._durable_position(
                event_id, event_type, run_id, task_id, blob, occurred_at, delivery_seq
            )
            self._delivery_seq = delivery_seq
            event = RunEvent(
                event_id=event_id,
                event_type=event_type,
                run_id=run_id,
                task_id=task_id,
                sequence=sequence,
                occurred_at=occurred_at,
                payload=blob,
                delivery_seq=delivery_seq,
            )
            self._history.append(event)
            if len(self._history) > _MAX_MEMORY_HISTORY:
                del self._history[: len(self._history) - _MAX_MEMORY_HISTORY]
            dropped = self._fan_out(event)
        if dropped:
            logger.warning(
                "event_dropped_for_slow_subscriber: event_id=%s delivery_seq=%d "
                "durable=%s subscribers_affected=%d",
                event_id,
                event.delivery_seq,
                event.durable,
                dropped,
            )
        return event

    def _durable_position(
        self,
        event_id: str,
        event_type: str,
        run_id: str,
        task_id: str,
        payload: dict[str, Any],
        occurred_at: str,
        delivery_seq: int,
    ) -> int | None:
        """Durable position when persisted, ``None`` when it could not be stored.

        R5-5.1 fix: a failed write must not consume a number from the durable
        space. The old code bumped the same counter the store allocates from, so
        after recovery the database handed out a number that had already gone
        out on the wire with different content — the client's ``<= last_seq``
        dedupe then discarded the real, persisted event as "seen".

        Returning ``None`` keeps the two spaces disjoint by construction: no
        row → no position → no cursor. ``strict_persistence`` re-raises instead
        of reporting a degraded delivery.
        """
        if self._recorder is None:
            # Memory mode: the process counter *is* the only position there is.
            self._sequence += 1
            return self._sequence
        try:
            position = self._recorder.append(
                event_id=event_id,
                event_type=event_type,
                run_id=run_id,
                task_id=task_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        except Exception as exc:  # noqa: BLE001 - any write failure means "not durable"
            self.persistence_failures += 1
            self.persistence_last_error = f"{type(exc).__name__}: {exc}"[:300]
            self.persistence_epoch += 1
            logger.error(
                "event_persist_failed: event_id=%s event_type=%s run_id=%s "
                "delivery_seq=%s durable_high_water=%s error=%s",
                event_id,
                event_type,
                run_id,
                delivery_seq,
                self._sequence,
                self.persistence_last_error,
            )
            if self.strict_persistence:
                raise
            if self.first_undurable_delivery is None:
                self.first_undurable_delivery = delivery_seq
            self.last_undurable_delivery = delivery_seq
            self.undurable_count += 1
            # Open or extend a disjoint interval. A successful write closes the
            # previous interval (below), so two outages never merge into one.
            if not self._interval_open:
                self.undurable_intervals.append(
                    {
                        "first": delivery_seq,
                        "last": delivery_seq,
                        "count": 1,
                        "recovered_at": None,
                    }
                )
                if len(self.undurable_intervals) > _MAX_UNDURABLE_INTERVALS:
                    del self.undurable_intervals[0]
                self._interval_open = True
            else:
                current = self.undurable_intervals[-1]
                current["last"] = delivery_seq
                current["count"] += 1
            return None
        self._sequence = max(self._sequence, position)
        if self.persistence_failures and self.recovered_epoch != self.persistence_epoch:
            self.recovered_epoch = self.persistence_epoch
            self.recovered_at_delivery = delivery_seq
            # Close the interval that just recovered with THIS recovery point,
            # not the process-wide last one (which a later outage overwrites).
            if self._interval_open and self.undurable_intervals:
                self.undurable_intervals[-1]["recovered_at"] = delivery_seq
                self._interval_open = False
            logger.warning(
                "event_persist_recovered: epoch=%d after_failures=%d "
                "undurable_range=%s..%s durable_resumes_at=%d delivery_seq=%d "
                "intervals=%s",
                self.persistence_epoch,
                self.persistence_failures,
                self.first_undurable_delivery,
                self.last_undurable_delivery,
                position,
                delivery_seq,
                self.undurable_intervals,
            )
        self.last_durable_delivery = delivery_seq
        return position

    def _fan_out(self, event: RunEvent) -> int:
        affected = 0
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow consumer: drop for this subscriber only, but make the
                # loss observable so the SSE stream can emit a gap frame and
                # an operator can see it in dropped_total / subscriber_stats.
                # Recorded against the WIRE ordinal: a gap is a delivery fact,
                # not a durability fact.
                q.note_drop(event.delivery_seq)
                self.dropped_total += 1
                affected += 1
        return affected

    def history(self, after_delivery: int = 0) -> list[RunEvent]:
        """In-process frames after a wire ordinal.

        Memory mode is unchanged (ordinal == position there). In persisted mode
        this is a fast cache, not the system of record: callers that need the
        durable log use :meth:`replay_page`.
        """
        with self._lock:
            return [e for e in self._history if e.delivery_seq > after_delivery]

    def replay_after(self, after_sequence: int, limit: int = 500) -> list[RunEvent]:
        """History that survives a restart.

        With a recorder this reads the store (bounded by ``limit``); without one
        it is the in-memory list, i.e. exactly the pre-R5-5.1 behaviour.
        """
        if self._recorder is None:
            return self.history(after_sequence)[:limit]
        events, _ = self.replay_page(after_sequence, limit)
        return events

    def replay_page(self, after_sequence: int, limit: int = 500) -> tuple[list[RunEvent], int]:
        """One page of durable history plus the position it advanced to."""
        if self._recorder is None:
            events = self.history(after_sequence)[:limit]
            return events, (events[-1].delivery_seq if events else after_sequence)
        rows, position = self._recorder.replay_page(after_sequence, limit)
        return [self._row_to_event(row) for row in rows], position

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> RunEvent:
        """Rebuild a frame from the durable log.

        ``delivery_seq`` stays 0: the ordinal a previous process used for this
        event is gone with that process, and inventing one would let a replayed
        frame collide with a live ordinal. 0 reads as "not on this wire".
        """
        refs = row.get("context_refs") or {}
        return RunEvent(
            event_id=str(row["event_id"]),
            event_type=str(row["event_type"]),
            run_id=str(refs.get("run_id") or ""),
            task_id=str(refs.get("task_id") or ""),
            sequence=int(row["sequence"]),
            occurred_at=str(row["occurred_at"]),
            payload=dict(row.get("payload") or {}),
            delivery_seq=0,
        )

    def resolve_cursor(self, token: str) -> CursorResolution:
        """Map a ``Last-Event-ID`` to a trustworthy resume position."""
        if self._recorder is not None:
            return (
                self._recorder.resolve(token)
                if token
                else CursorResolution(
                    kind="first",
                    after=0,
                    latest=self._recorder.latest(),
                    oldest=None,
                    pruned_through=0,
                )
            )
        # Memory mode: the only cursors that exist are the ones in this process.
        if not token:
            with self._lock:
                return CursorResolution("first", 0, self._sequence, None, 0)
        with self._lock:
            latest = self._sequence
            for event in self._history:
                if event.event_id == token:
                    # Memory mode always has a position (the process counter).
                    return CursorResolution("ok", int(event.sequence or 0), latest, None, 0)
        return CursorResolution(
            "unknown",
            latest,
            latest,
            None,
            0,
            detail="event id not in this process's memory history (restarted?)",
        )

    def cursor_token(self, event: RunEvent) -> str | None:
        """Cursor to put in ``id:``, or ``None`` to emit no id at all.

        ``None`` is only returned for an event that was *not* persisted while a
        recorder is attached. Emitting a cursor for it would hand the client a
        resume point that either does not exist yet (so the real event stored
        under that number later would be skipped as "already seen") or never
        will. Omitting the ``id:`` line makes the browser keep the last durable
        cursor, so reconnecting resumes from the last point the server can
        actually prove it has.
        """
        if self._recorder is None:
            return event.event_id
        if event.sequence is None:
            return None
        return self._recorder.cursor_token(event.sequence)

    def durability_state(self) -> dict[str, Any]:
        """Honest report of whether this stream can be replayed later."""
        state = self.persistence_snapshot()
        state["dropped_total"] = self.dropped_total
        if self._recorder is not None:
            state.update(self._recorder.stats())
        return state

    def persistence_snapshot(self) -> dict[str, Any]:
        """Cheap (no DB) durability view for a per-poll SSE check.

        ``mode`` is *current* capability, not history: a successful write after
        a failure flips it back to ``durable`` while ``failures`` and the
        ``undurable_*`` range keep the hole visible. Reporting "degraded"
        forever would be as dishonest as reporting it never happened.
        """
        if self._recorder is None:
            mode = "memory"
        elif self.persistence_failures and self.recovered_epoch != self.persistence_epoch:
            mode = "degraded"
        else:
            mode = "durable"
        return {
            "mode": mode,
            "producer_instance_id": self.producer_instance_id,
            "sequence": self._sequence,
            "last_durable_sequence": self._sequence,
            "delivery_seq": self._delivery_seq,
            "failures": self.persistence_failures,
            "last_error": self.persistence_last_error,
            "undurable_count": self.undurable_count,
            # Disjoint per-outage intervals; authoritative for "which deliveries
            # were never persisted". The first/last pair above remains an
            # aggregate for backward-compatible consumers and may span multiple
            # outages (do not read it as one continuous hole).
            "undurable_intervals": [dict(i) for i in self.undurable_intervals],
            "undurable_first_delivery": self.first_undurable_delivery,
            "undurable_last_delivery": self.last_undurable_delivery,
            # Deprecated alias kept for consumers written against R5-5.1: it is
            # now the WIRE ordinal, never a durable position.
            "first_undurable_sequence": self.first_undurable_delivery,
            "last_durable_delivery": self.last_durable_delivery,
            "epoch": self.persistence_epoch,
            "recovered": self.recovered_epoch == self.persistence_epoch
            and bool(self.persistence_failures),
            "recovered_at_delivery": self.recovered_at_delivery,
        }

    def prune_events(self, keep_last: int) -> dict[str, Any]:
        """Apply the retention window now (ops/testing entry point)."""
        if self._recorder is None:
            raise RuntimeError("no durable event stream to prune")
        return self._recorder.store.prune_stream(keep_last)

    def latest_sequence(self) -> int:
        """Last confirmed durable position (replay target), NOT the wire tip."""
        with self._lock:
            return self._sequence

    def delivery_tip(self) -> int:
        """Highest wire ordinal handed out so far (gap accounting baseline)."""
        with self._lock:
            return self._delivery_seq

    def subscriber_stats(self) -> list[dict[str, Any]]:
        """Per-subscriber queue depth and drop accounting (diagnostics only)."""
        with self._lock:
            subs = list(self._subscribers)
        stats: list[dict[str, Any]] = []
        for q in subs:
            dropped, ranges = q.drop_snapshot()
            stats.append(
                {
                    "maxsize": q.maxsize,
                    "queued": q.qsize(),
                    "dropped": dropped,
                    "dropped_ranges": ranges,
                }
            )
        return stats

    def dropped_ranges(self) -> list[tuple[int, int]]:
        """Union of every live subscriber's drop ranges, merged and sorted.

        This is a cross-check for diagnostics: the authoritative answer for one
        connection is that connection's own queue (``SubscriberQueue``).
        """
        merged: list[tuple[int, int]] = []
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            merged.extend(q.dropped_ranges)
        merged.sort()
        out: list[tuple[int, int]] = []
        for lo, hi in merged:
            if out and lo <= out[-1][1] + 1:
                out[-1] = (out[-1][0], max(out[-1][1], hi))
            else:
                out.append((lo, hi))
        return out


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
        data_provider: Any | None = None,
        graph: Any | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.tasks = TaskService(session_factory)
        self.runs = RunService(session_factory)
        self.case_dir = case_dir
        self.base_url = teamflow_base_url
        self.eval_token = eval_token
        self.locators = locators
        self.trace_dir = trace_dir
        self.reset_before_run = reset_before_run
        self.data_provider = data_provider
        # Shared correlation graph: a chain run must write the same graph the
        # Graph page reads, otherwise its nodes are invisible in the product.
        self.graph = graph
        # An injected bus may carry a durable recorder (R5-5.1).
        self.bus = bus if bus is not None else EventBus()
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
        case_path = (
            self.case_dir / case_file
            if case_file
            else latest_case_path(self.case_dir, str(task["case_id"]))
        )
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

    def run_chain(
        self,
        case: TestCaseVersion,
        *,
        workspace_id: str = "ws-default",
        scenario_id: str = "",
        timeout_s: float = 180.0,
        after_run: Callable[[Any], dict[str, Any]] | None = None,
        fault_events: list[dict[str, Any]] | None = None,
        probe_requests: list[Any] | None = None,
    ) -> tuple[RunView, Any]:
        """Execute a FullChain under the standard Run lifecycle.

        Before R5-4.2 the chain ran first and registered a Run afterwards, so
        the run could not be observed, cancelled or retried while it executed
        and its id was invented after the fact. Now:

          Task -> Run (QUEUED) -> PREPARING -> RUNNING -> FINALIZING -> terminal

        ``after_run`` is called while the run is FINALIZING (evidence/event
        persistence). Its stages are recorded in the run report, so a
        persistence failure is visible on the run instead of being swallowed.
        """
        from packages.execution.pipeline import FullChain

        task_id = f"task_{case.case_id}"
        try:
            self.tasks.create(task_id, case.case_id, f"chain: {case.title}")
        except Exception:  # noqa: BLE001 - retry reuses the logical task
            logger.info("chain_task_exists: %s", task_id)

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
        self.bus.publish("run.queued", run_id, task_id, mode="full_chain", case=case.case_id)

        def progress(stage: str, status: str, detail: str) -> None:
            self.bus.publish(
                "run.stage", run_id, task_id, stage=stage, status=status, detail=detail
            )
            self.runs.update_progress(run_id, {"stage": stage, "status": status, "detail": detail})
            self.runs.heartbeat(run_id, self.worker_id)

        graph = self.graph
        if graph is None:
            # Writing evidence without the shared graph would produce nodes the
            # Graph page can never read: fail loudly instead of half-persisting.
            raise RuntimeError(
                "full-chain run requires the shared correlation graph; "
                "wire it via register_workbench(graph=...)"
            )
        chain = FullChain(
            base_url=self.base_url,
            graph=graph,
            eval_token=self.eval_token,
            locators=self.locators,
            data_provider=self.data_provider,
            progress=progress,
        )
        self.runs.transition(run_id, RunStatus.PREPARING, started_at=_now())
        self.bus.publish("run.preparing", run_id, task_id, case=case.case_id)
        self.runs.transition(run_id, RunStatus.RUNNING)
        self.bus.publish("run.running", run_id, task_id, attempt=view.attempt)

        report: Any
        try:
            report = chain.run(
                scenario_id or case.case_id,
                case,
                fault_events=fault_events,
                probe_requests=probe_requests,
                workspace_id=workspace_id,
                run_id_override=run_id,
                cancel_check=lambda: self.runs.is_cancel_requested(run_id),
            )
        except Exception as exc:
            # R5-4.6 §4.4: a chain that blows up must never leave its Run row
            # RUNNING. Finalize it as FAILED with the reason, then re-raise so
            # the caller sees the real error.
            logger.error("chain_execution_error: %s: %s", run_id, exc)
            self.runs.transition(run_id, RunStatus.FINALIZING)
            self.runs.finalize(
                run_id,
                status=RunStatus.FAILED,
                execution_status="FAILED",
                test_verdict="NOT_EVALUATED",
                report={"error": f"{type(exc).__name__}: {exc}"[:1000]},
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
            self.bus.publish("run.finished", run_id, task_id, status="FAILED", error=str(exc))
            raise

        self.runs.transition(run_id, RunStatus.FINALIZING)
        self.bus.publish(
            "run.finalizing", run_id, task_id, verdict=report.execution.get("test_verdict")
        )
        if report.steps:
            self.runs.append_steps(run_id, report.steps)

        if after_run is not None:
            try:
                persisted = after_run(report) or {}
            except Exception as exc:  # noqa: BLE001 - persistence failure is a finding
                # R5-4.7 §4.3: a persistence blow-up must still land as a
                # FAILED stage on the run — an empty {"error"} dict hid it.
                persisted = {
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "stages": [
                        {
                            "name": "persistence",
                            "status": "FAILED",
                            "detail": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    ],
                }
            from packages.execution.pipeline import ChainStage

            for entry in persisted.get("stages", []):
                stage = ChainStage(
                    str(entry.get("name", "persistence")),
                    str(entry.get("status", "FAILED")),
                    str(entry.get("detail", "")),
                )
                # The stage must be visible BOTH live (progress) and in the
                # final report the chain record aggregates.
                report.stages.append(stage)
                progress(stage.name, stage.status, stage.detail)
            report.persisted = persisted

        cancelled = bool(report.cancelled)
        exec_status = str(report.execution.get("execution_status") or "")
        # "Completed" means the chain ran to the end. A recorded execution error,
        # a blocked run or a non-clean execution status must not produce a
        # COMPLETED Run row even if a stage list looks complete.
        execution_ok = (
            bool(report.completed)
            and not cancelled
            and not report.execution.get("blocked")
            and not report.execution.get("error")
            and exec_status not in {"FAILED", "CANCELLED", "TIMED_OUT", "BLOCKED"}
        )
        if cancelled:
            status = RunStatus.CANCELLED
        elif execution_ok:
            status = RunStatus.COMPLETED
        else:
            status = RunStatus.FAILED

        final = self.runs.finalize(
            run_id,
            status=status,
            execution_status=str(report.execution.get("execution_status") or "FAILED"),
            test_verdict=str(report.execution.get("test_verdict") or "NOT_EVALUATED"),
            report=report.to_dict(),
            error=str(
                report.execution.get("blocked_reason")
                or report.execution.get("error")
                or ("" if execution_ok else "chain did not complete")
            )[:500],
        )
        self.bus.publish(
            "run.finished",
            run_id,
            task_id,
            status=final.status,
            verdict=final.test_verdict,
            chain_id=report.chain_id,
        )
        return final, report

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
                data_provider=self.data_provider,
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
