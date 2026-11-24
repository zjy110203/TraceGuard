"""Stage-3 workbench REST + SSE routes.

Exposes projects(TeamFlow) / TestCase / Task / Run / Step / report / history
and a live SSE stream. Task->Run separation is reflected in the URLs:
``POST /tasks`` creates a logical task; ``POST /tasks/{id}/runs`` creates a NEW
run every call (retry is never a PUT on the old run).
"""

from __future__ import annotations

import asyncio
import json
import queue
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from packages.core.logging import get_logger
from packages.execution.eventaudit import count_dropped_in_range
from packages.execution.locators import LocatorRegistry
from packages.execution.models import TestCaseVersion, load_cases_dir
from packages.execution.orchestrator import EventBus, Orchestrator, RunEvent, SubscriberQueue

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["workbench"])

# SSE pacing: poll the subscriber queue without blocking the event loop, and
# emit a comment frame after this much silence so proxies keep the link open.
_SSE_POLL_S = 0.2
_SSE_KEEPALIVE_S = 15.0
# Bounded per-connection buffer. Overflow is recorded by the bus and reported to
# the client as a gap comment frame instead of being lost silently.
_SSE_QUEUE_MAXSIZE = 1000
# Replay is read in bounded batches so a large retained history does not
# block the event loop or other clients.
_SSE_REPLAY_BATCH = 500


class CreateTaskBody(BaseModel):
    task_id: str
    case_id: str
    title: str


class SubmitBody(BaseModel):
    case_file: str | None = None
    timeout_s: float = 60.0


def _orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


@router.get("/cases")
def list_cases(request: Request) -> list[dict[str, Any]]:
    cases: tuple[TestCaseVersion, ...] = load_cases_dir(request.app.state.case_dir)
    return [
        {
            "case_id": c.case_id,
            "version": c.version,
            "title": c.title,
            "steps": len(c.steps),
            "assertions": len(c.assertions),
            "compiled_from": c.compiled_from,
            # Approval provenance lets the UI show the human-review gate
            # without a per-case detail request.
            "approved_by": c.approved_by,
        }
        for c in cases
    ]


@router.post("/tasks", status_code=201)
def create_task(body: CreateTaskBody, request: Request) -> dict[str, Any]:
    return _orchestrator(request).tasks.create(body.task_id, body.case_id, body.title)


@router.get("/tasks")
def list_tasks(request: Request) -> list[dict[str, Any]]:
    return _orchestrator(request).tasks.list()


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    return _orchestrator(request).tasks.get(task_id)


@router.post("/tasks/{task_id}/runs", status_code=202)
def submit_run(task_id: str, body: SubmitBody, request: Request) -> dict[str, Any]:
    view = _orchestrator(request).submit(
        task_id, case_file=body.case_file, timeout_s=body.timeout_s
    )
    return view.to_dict()


@router.get("/tasks/{task_id}/runs")
def run_history(task_id: str, request: Request) -> list[dict[str, Any]]:
    return [v.to_dict() for v in _orchestrator(request).history(task_id)]


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    orch = _orchestrator(request)
    view = orch.runs.get(run_id)
    data = view.to_dict()
    data["steps"] = orch.runs.steps(run_id)
    return data


@router.get("/runs/{run_id}/report")
def get_report(run_id: str, request: Request) -> dict[str, Any]:
    return _orchestrator(request).runs.report(run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    return _orchestrator(request).cancel(run_id).to_dict()


@router.get("/events")
def event_history(
    after_sequence: int = 0,
    limit: int = 5000,
    request: Request = None,  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """Event history — durable when persistence is on, in-memory otherwise."""
    orch: Orchestrator = request.app.state.orchestrator  # type: ignore[union-attr]
    return [e.to_dict() for e in orch.bus.replay_after(after_sequence, limit)]


@router.get("/events/durability")
def event_durability(request: Request) -> dict[str, Any]:
    """Whether the stream can be replayed later, and which part cannot.

    A separate endpoint from ``/health`` on purpose: the database can be healthy
    while event persistence is degraded (a failed write must not be reported as
    a database outage, and a healthy database must not imply a durable stream).
    """
    return _orchestrator(request).bus.durability_state()


@router.get("/events/stream")
def event_stream(request: Request) -> StreamingResponse:
    """SSE stream of real orchestration events.

    R5-5.1 changes, all backward compatible at the transport level:

    * ``id:`` carries a durable cursor (``v2:<stream_seq>:<instance>``) when the
      bus is persisted, and the event_id otherwise. The server accepts **both**
      formats on reconnect, so an old tab with a stored ``event_id`` still works.
    * an unknown cursor is no longer replayed from 0 (that silently re-delivered
      the whole history). It is announced as ``stream.notice`` and live-only
      streaming resumes from the current tip.
    * an expired cursor (older than the retention window) replays everything
      retained and announces ``cursor-expired``.
    * a failed durable write is announced as ``persist-degraded``; the stream
      keeps flowing but never claims that part of it can be replayed.

    ``stream.notice`` is a transport control event with a JSON body. It is not a
    C05 ``RunEvent``: the event contract is untouched, and clients that do not
    know the event name simply ignore it.

    A sequence gap (queue overflow for this subscriber, or events the server has
    no history for) is announced with an SSE **comment** frame
    ``: gap from=<a> to=<b> dropped=<n>`` (unchanged, for existing clients) plus
    a ``stream.notice{code:"gap"}`` frame for the UI.

    The generator MUST be async: ``request.is_disconnected()`` is a coroutine.
    Calling it from a sync generator returns a (always truthy) coroutine object,
    which terminated the stream right after replay and left live events
    undelivered (found by R5-4.9 audit).
    """

    orch = _orchestrator(request)
    last_id = request.headers.get("last-event-id", "")
    resolution = orch.bus.resolve_cursor(last_id)
    after_seq = resolution.after
    if not resolution.ok:
        # Loud on purpose: the old code logged at warning level and then quietly
        # replayed everything, which made a restart look like a healthy stream.
        logger.warning(
            "sse_cursor_%s: token=%s after=%d latest=%d watermark=%d detail=%s",
            resolution.kind,
            last_id,
            resolution.after,
            resolution.latest,
            resolution.pruned_through,
            resolution.detail,
        )

    async def generate() -> AsyncIterator[str]:
        # Subscribe INSIDE the generator: if the client is gone before the body
        # iterator is ever entered, Starlette never runs this function, so an
        # eager subscribe() would leak a queue per aborted connection (proved by
        # test_disconnect_unsubscribes_and_leaks_nothing).
        q = orch.bus.subscribe(maxsize=_SSE_QUEUE_MAXSIZE)
        last_seq = after_seq
        try:
            # Only a *doubtful* cursor is announced. A normal resume must not add
            # frames: an unchanged client would otherwise see noise on every
            # reconnect, and replay determinism would depend on the header.
            if resolution.kind in ("unknown", "expired"):
                yield _sse_notice(
                    "cursor-" + resolution.kind,
                    {
                        "requested": last_id,
                        "resume_after": resolution.after,
                        "latest": resolution.latest,
                        "oldest_retained": resolution.oldest,
                        "pruned_through": resolution.pruned_through,
                        "detail": resolution.detail,
                    },
                )

            # Replay in bounded batches: a 20k-event history must not be read
            # into memory in one query, and other clients must not wait for it.
            target = orch.bus.latest_sequence()
            while True:
                batch, position = orch.bus.replay_page(last_seq, _SSE_REPLAY_BATCH)
                if not batch:
                    break  # nothing left to replay; continue with live events
                if position <= last_seq:
                    # Defensive: a page that does not advance the cursor would
                    # spin here and block the event loop (this happened when an
                    # event's own sequence was used instead of its position).
                    logger.error(
                        "sse_replay_no_progress: after=%d position=%d batch=%d",
                        last_seq,
                        position,
                        len(batch),
                    )
                    break
                for event in batch:
                    yield _sse(event, orch.bus.cursor_token(event))
                last_seq = position
                if last_seq >= target:
                    break

            last_emit = time.monotonic()
            # Highest sequence already announced as missing. Kept separate from
            # `last_seq` (highest sequence actually delivered): a sequence may be
            # legitimately absent without ever being delivered.
            announced_seq = last_seq
            announced_epoch = -1
            while True:
                if await request.is_disconnected():
                    break
                durability = orch.bus.persistence_snapshot()
                if durability["mode"] == "degraded" and durability["epoch"] != announced_epoch:
                    announced_epoch = int(durability["epoch"])
                    yield _sse_notice("persist-degraded", durability)
                try:
                    event = q.get_nowait()
                except queue.Empty:
                    known = _contiguous_drop_gap(q, last_seq, announced_seq)
                    if known is not None:
                        gap_start, gap_end = known
                        covered = count_dropped_in_range(q.dropped_ranges, gap_start, gap_end)
                        announced_seq = gap_end
                        last_emit = time.monotonic()
                        logger.warning(
                            "sse_sequence_gap: reason=queue_overflow from=%d to=%d "
                            "missing=%d queue_dropped=%d covered_by_queue_drops=%d "
                            "bus_dropped_total=%d",
                            gap_start,
                            gap_end,
                            gap_end - gap_start + 1,
                            q.dropped,
                            covered,
                            orch.bus.dropped_total,
                        )
                        yield _sse_gap(gap_start, gap_end, covered)
                        yield _sse_notice(
                            "gap",
                            {
                                "from": gap_start,
                                "to": gap_end,
                                "missing": gap_end - gap_start + 1,
                                "queue_dropped": q.dropped,
                                "covered_by_queue_drops": covered,
                                "reason": "queue_overflow",
                                "recoverable": True,
                            },
                        )
                        continue
                    if time.monotonic() - last_emit >= _SSE_KEEPALIVE_S:
                        yield ": keep-alive\n\n"
                        last_emit = time.monotonic()
                    # Cooperative sleep instead of blocking queue.get(): keeps
                    # the event loop free for other clients and for disconnects.
                    await asyncio.sleep(_SSE_POLL_S)
                    continue
                if event.sequence <= last_seq:
                    # Published between subscribe() and the replay above: it is
                    # already on the wire, so delivering it twice would break
                    # "unique ids per connection" for Last-Event-ID consumers.
                    continue
                if event.sequence > last_seq + 1:
                    gap_start = max(last_seq + 1, announced_seq + 1)
                    gap_end = event.sequence - 1
                    if gap_start <= gap_end:
                        covered = count_dropped_in_range(q.dropped_ranges, gap_start, gap_end)
                        announced_seq = gap_end
                        logger.warning(
                            "sse_sequence_gap: reason=sequence_jump expected=%d got=%d "
                            "missing=%d queue_dropped=%d covered_by_queue_drops=%d "
                            "bus_dropped_total=%d",
                            gap_start,
                            event.sequence,
                            gap_end - gap_start + 1,
                            q.dropped,
                            covered,
                            orch.bus.dropped_total,
                        )
                        yield _sse_gap(gap_start, gap_end, covered)
                        yield _sse_notice(
                            "gap",
                            {
                                "from": gap_start,
                                "to": gap_end,
                                "missing": gap_end - gap_start + 1,
                                "queue_dropped": q.dropped,
                                "covered_by_queue_drops": covered,
                                "reason": "sequence_jump",
                                "recoverable": True,
                            },
                        )
                last_seq = event.sequence
                last_emit = time.monotonic()
                yield _sse(event, orch.bus.cursor_token(event))
        finally:
            orch.bus.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse(event: RunEvent, cursor: str | None = None) -> str:
    payload = json.dumps(event.to_dict(), ensure_ascii=False)
    # The cursor is opaque to clients; it is what they hand back in
    # Last-Event-ID. Falling back to the event_id keeps memory-mode identical.
    return f"id: {cursor or event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"


def _sse_notice(code: str, data: dict[str, Any]) -> str:
    """Transport-level control frame (NOT a C05 event)."""
    payload = json.dumps({"code": code, **data}, ensure_ascii=False, default=str)
    return f"event: stream.notice\ndata: {payload}\n\n"


def _sse_gap(start: int, end: int, dropped: int) -> str:
    """Comment frame announcing missing sequences (never a data frame)."""
    return f": gap from={start} to={end} dropped={dropped}\n\n"


def _contiguous_drop_gap(
    q: SubscriberQueue, last_seq: int, announced_seq: int
) -> tuple[int, int] | None:
    """First recorded drop range that is contiguous with what we know.

    Only a range starting exactly at the next expected sequence is announced: a
    hole means the lower part was merely queued/in flight, and calling it lost
    would libel an event the client is about to receive.
    """
    start = max(last_seq, announced_seq) + 1
    for low, high in q.dropped_ranges:
        if high < start:
            continue  # already delivered or already announced
        if low > start:
            return None  # a hole: wait for the queued part to arrive
        return start, high
    return None


def register_workbench(
    app: Any,
    *,
    session_factory: Any,
    case_dir: Path,
    teamflow_base_url: str,
    eval_token: str,
    max_workers: int = 2,
    trace_dir: Path | None = None,
    data_provider: Any | None = None,
    graph: Any | None = None,
    event_persistence: bool = True,
    workspace_id: str = "ws-default",
    event_retention_events: int = 20000,
) -> Orchestrator:
    """Wire the orchestrator into an app and include the router.

    ``event_persistence`` (R5-5.1) attaches the durable event store so the SSE
    history survives a restart and ``Last-Event-ID`` can be resumed. Passing
    ``False`` keeps the pre-R5-5.1 in-memory stream, which the tests rely on.
    """
    import uuid

    from packages.evidence.events import EventStore, create_event_tables
    from packages.evidence.run_events import RunEventRecorder
    from packages.execution.tasks import create_task_tables

    create_task_tables(app.state.engine)
    locators_path = case_dir / "locators.json"
    locators = LocatorRegistry.from_file(locators_path) if locators_path.exists() else None
    bus = None
    if event_persistence:
        create_event_tables(app.state.engine)
        recorder = RunEventRecorder(
            EventStore(session_factory),
            producer_instance_id=f"bus-{uuid.uuid4().hex[:12]}",
            workspace_id=workspace_id,
            retention_events=event_retention_events,
        )
        bus = EventBus(recorder)
        logger.info(
            "event_persistence_enabled",
            extra={"extra_fields": dict(bus.startup_report)},
        )
    orch = Orchestrator(
        session_factory=session_factory,
        case_dir=case_dir,
        teamflow_base_url=teamflow_base_url,
        eval_token=eval_token,
        max_workers=max_workers,
        locators=locators,
        trace_dir=trace_dir,
        data_provider=data_provider,
        graph=graph,
        bus=bus,
    )
    app.state.orchestrator = orch
    app.state.case_dir = case_dir
    # Diagnosis reads authoritative fault evidence from the eval plane; without
    # these two keys it silently degrades to "no fault evidence" (found by
    # auditing the live instance after the second round shipped).
    app.state.teamflow_base_url = teamflow_base_url
    app.state.eval_token = eval_token
    app.include_router(router)
    return orch
