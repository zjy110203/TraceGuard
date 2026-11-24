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
from packages.execution.orchestrator import Orchestrator, RunEvent, SubscriberQueue

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["workbench"])

# SSE pacing: poll the subscriber queue without blocking the event loop, and
# emit a comment frame after this much silence so proxies keep the link open.
_SSE_POLL_S = 0.2
_SSE_KEEPALIVE_S = 15.0
# Bounded per-connection buffer. Overflow is recorded by the bus and reported to
# the client as a gap comment frame instead of being lost silently.
_SSE_QUEUE_MAXSIZE = 1000


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
def event_history(after_sequence: int = 0, request: Request = None) -> list[dict[str, Any]]:  # type: ignore[assignment]
    orch: Orchestrator = request.app.state.orchestrator  # type: ignore[union-attr]
    return [e.to_dict() for e in orch.bus.history(after_sequence)]


@router.get("/events/stream")
def event_stream(request: Request) -> StreamingResponse:
    """SSE stream of real orchestration events. Consumer replays from history
    using the ``Last-Event-ID`` header (at-least-once, idempotent by event_id).

    The generator MUST be async: ``request.is_disconnected()`` is a coroutine.
    Calling it from a sync generator returns a (always truthy) coroutine object,
    which terminated the stream right after replay and left live events
    undelivered (found by R5-4.9 audit; the old test only asserted headers).

    A sequence gap (queue overflow for this subscriber, or events the server has
    no history for) is announced with an SSE **comment** frame
    ``: gap from=<a> to=<b> dropped=<n>``, where ``dropped`` counts the missing
    sequences this subscriber's queue actually recorded as dropped (0 means the
    loss did not come from this queue — e.g. history lost on restart). Comments
    are ignored by every spec-compliant client, so no `RunEvent` field and no
    event type is added (C05 stays intact); a debugging client can still surface
    the loss.

    Two detection paths, one signal:

    * a delivered event whose sequence jumps ahead of the cursor (something was
      lost in between);
    * an idle poll where the subscriber queue *recorded* dropping sequences.
      Without this, a loss that happens after the last delivered event would
      stay invisible until — if ever — the next event is published.
    """

    orch = _orchestrator(request)
    last_id = request.headers.get("last-event-id", "")
    after_seq = 0
    if last_id:
        for e in orch.bus.history():
            if e.event_id == last_id:
                after_seq = e.sequence
                break
        else:
            # Unknown/expired cursor: the old behaviour was to fall back to 0 and
            # replay the whole (in-process) history. Keep that at-least-once
            # behaviour, but say so — silence here is what made a restart look
            # like "the stream is fine".
            logger.warning("sse_unknown_cursor: %s; replaying from sequence 0", last_id)

    async def generate() -> AsyncIterator[str]:
        # Subscribe INSIDE the generator: if the client is gone before the body
        # iterator is ever entered, Starlette never runs this function, so an
        # eager subscribe() would leak a queue per aborted connection (proved by
        # test_disconnect_unsubscribes_and_leaks_nothing).
        q = orch.bus.subscribe(maxsize=_SSE_QUEUE_MAXSIZE)
        last_seq = after_seq
        try:
            # Replay missed events first (at-least-once), then continue live.
            for event in orch.bus.history(last_seq):
                last_seq = event.sequence
                yield _sse(event)

            last_emit = time.monotonic()
            # Highest sequence already announced as missing. Kept separate from
            # `last_seq` (highest sequence actually delivered): a sequence may be
            # legitimately absent without ever being delivered.
            announced_seq = last_seq
            while True:
                if await request.is_disconnected():
                    break
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
                last_seq = event.sequence
                last_emit = time.monotonic()
                yield _sse(event)
        finally:
            orch.bus.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse(event: RunEvent) -> str:
    payload = json.dumps(event.to_dict(), ensure_ascii=False)
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"


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
) -> Orchestrator:
    """Wire the orchestrator into an app and include the router."""
    from packages.execution.tasks import create_task_tables

    create_task_tables(app.state.engine)
    locators_path = case_dir / "locators.json"
    locators = LocatorRegistry.from_file(locators_path) if locators_path.exists() else None
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
