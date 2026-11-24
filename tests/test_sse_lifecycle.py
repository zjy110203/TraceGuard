"""R5-4.9: SSE lifecycle against a real HTTP server (not just response headers).

Why this module exists
----------------------
The only previous SSE assertion checked status code + content type, so a fatal
defect passed: ``event_stream`` was a *sync* generator calling the coroutine
``request.is_disconnected()``. A coroutine object is always truthy, so the loop
broke immediately after replay — live events were never delivered and Python
logged ``coroutine 'Request.is_disconnected' was never awaited``.

These tests pin the behaviour the workbench actually relies on:

1. live events published after the connection opens reach the client;
2. a real execution's run.* events reach the client end to end;
3. ``Last-Event-ID`` replays only the events after the cursor;
4. closing the connection unsubscribes the queue (no leak, no growth);
5. a silent stream cannot hang a reader (hard total-duration + read timeouts);
6. a subscriber-queue overflow is *visible* as an SSE gap comment frame.

Independence contract (R5-4.9 §4.1)
-----------------------------------
Every test here creates the data it asserts on. ``test_last_event_id_...`` used
to read ``/api/v1/events`` and assume earlier tests had published something, so
running it alone failed on ``len(history) >= 3``. It now publishes its own
contiguous batch and picks its own cursor, and ``test_each_sse_test_is_order_
independent`` proves that batch works on a bus with no history at all.

Ports come from :mod:`tests.netutil`: a socket is bound once and handed to
uvicorn, so there is no "allocate → close → bind" window for another process
(or a parallel test) to steal the port.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from apps.api.main import build_app
from packages.core.config import Settings
from packages.execution.eventaudit import audit_event_sequences
from packages.execution.orchestrator import EventBus, RunEvent
from tests.netutil import serve_uvicorn

EVAL_TOKEN = "local-dev-eval-token-change-me"
CASE_DIR = Path("benchmark/cases")


# --------------------------------------------------------------- infrastructure


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _port_of(base: str) -> int:
    port = urlparse(base).port
    assert port is not None, base
    return port


def _health(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


def _settled_subscribers(bus: Any, settle_s: float = 1.0, timeout: float = 15.0) -> int:
    """Subscriber count once it has stopped changing.

    A previous stream's disconnect is detected asynchronously (the generator
    polls), so snapshotting the count straight after a test can catch a
    half-released subscriber and make a later equality assertion unsatisfiable.
    """
    deadline = time.monotonic() + timeout
    last = bus.subscriber_count
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        current = bus.subscriber_count
        if current != last:
            last, stable_since = current, time.monotonic()
        elif time.monotonic() - stable_since >= settle_s:
            return current
    raise AssertionError(f"subscriber count never settled (last={bus.subscriber_count})")


@pytest.fixture(scope="module")
def live_platform(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real TeamFlow + real platform API, both on pre-bound ephemeral ports."""
    work = tmp_path_factory.mktemp("sse")
    # ExitStack so both servers are torn down in reverse order and each one's
    # thread death is asserted by netutil.stop_uvicorn.
    with contextlib.ExitStack() as stack:
        tf_base = stack.enter_context(serve_uvicorn("apps.teamflow.main:app"))
        _wait_until(lambda: _health(tf_base), 20, "TeamFlow health")

        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{(work / 'traceguard.db').as_posix()}",
            teamflow_host="127.0.0.1",
            teamflow_port=_port_of(tf_base),
            eval_control_token=EVAL_TOKEN,
            workbench_enabled=True,
            workbench_workers=1,
            case_dir=str(CASE_DIR),
            exploration_data_dir=str(work / "explorations"),
            benchmark_data_dir=str(work / "benchmark"),
            chain_data_dir=str(work / "chains"),
            candidate_data_dir=str(work / "candidates"),
            blob_dir=str(work / "blobs"),
        )
        app = build_app(settings)
        api = stack.enter_context(serve_uvicorn(app))
        _wait_until(lambda: _health(api), 20, "platform health")
        yield {"api": api, "app": app, "bus": app.state.orchestrator.bus}


class _SSEReader(threading.Thread):
    """Reads an SSE stream with hard, traffic-independent time bounds.

    R5-4.9 §4.2, the defects this replaces::

        httpx.Client(timeout=None)              # connect could block forever
        for line in resp.iter_lines():          # no read timeout at all
            if time.monotonic() > deadline: ...  # only evaluated after a line

    With a silent server the loop never iterated, so the deadline never ran and
    the thread never exited. A hung test is worse than a failing one. Now:

    * ``connect``/``read`` timeouts are set on the client;
    * a watchdog timer ends the stream after ``max_seconds`` regardless of
      traffic, closes the response so ``iter_lines`` unblocks, and records
      ``timed_out`` (a timeout is *not* an error);
    * ``stop()`` lets a test end a stream it no longer needs;
    * ``done`` is always set and unexpected exceptions land in ``error``;
    * ``started_at``/``last_line_at``/``timed_out`` make a failure diagnosable.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stop_when: Callable[[list[dict[str, Any]]], bool] | None = None,
        stop_on_comment: bool = False,
        stop_when_comment: Callable[[list[str]], bool] | None = None,
        max_seconds: float = 45.0,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
    ) -> None:
        super().__init__(daemon=True, name="sse-reader")
        self.url = url
        self.headers = headers or {}
        self.stop_when = stop_when or (lambda frames: False)
        self.stop_on_comment = stop_on_comment
        self.stop_when_comment = stop_when_comment
        self.max_seconds = max_seconds
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

        self.frames: list[dict[str, Any]] = []
        # The ``id:`` line in effect for each data frame, ``None`` when the frame
        # carried no id at all. Added for the R5-5.1 cursor-protocol tests: a
        # frame without an id is a *deliberate* signal (the browser keeps the
        # previous cursor), so a reader that only keeps payloads cannot tell it
        # apart from a cursor that was silently reused.
        self.frame_ids: list[str | None] = []
        self.raw_comments: list[str] = []
        self.status: int | None = None
        self.content_type: str = ""
        self.error: BaseException | None = None
        # Diagnostics: a timeout is its own outcome, never an `error`.
        self.timed_out = False
        self.stop_requested = False
        self.started_at: float | None = None
        self.last_line_at: float | None = None
        self.finished_at: float | None = None
        self.close_errors: list[str] = []
        self.ready = threading.Event()
        self.done = threading.Event()

        self._pending_id: str | None = None

        self._client: httpx.Client | None = None
        self._response: httpx.Response | None = None
        self._release_lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        self.started_at = time.monotonic()
        self._timer = threading.Timer(self.max_seconds, self._on_deadline)
        self._timer.daemon = True
        self._timer.start()
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.read_timeout,
            pool=self.connect_timeout,
        )
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                self._client = client
                with client.stream("GET", self.url, headers=self.headers) as resp:
                    self._response = resp
                    self.status = resp.status_code
                    self.content_type = resp.headers.get("content-type", "")
                    self.ready.set()
                    if resp.status_code != 200:
                        return  # HTTP error body, not an event stream
                    if self.stop_requested:
                        return
                    for line in resp.iter_lines():
                        self.last_line_at = time.monotonic()
                        if self._consume(line):
                            break
                        if self.stop_requested:
                            break
        except httpx.TimeoutException:
            # A stalled connect/read is a timeout, not a test-framework error.
            if not self.stop_requested:
                self.timed_out = True
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            # Our own abort()/stop() closes the socket, which surfaces here as a
            # transport error. That is expected, not a failure.
            if not self.timed_out and not self.stop_requested:
                self.error = exc
        finally:
            if self._timer is not None:
                self._timer.cancel()
            self._release()
            self.finished_at = time.monotonic()
            self.done.set()

    def abort(self) -> None:
        """Watchdog entry point: mark the timeout and unblock the reader."""
        self.timed_out = True
        self._release()

    def stop(self, timeout: float = 10.0) -> None:
        """End the stream from another thread; safe while a read is blocking."""
        self.stop_requested = True
        self._release()
        if self._timer is not None:
            self._timer.cancel()
        self.join(timeout)

    # -- internals ---------------------------------------------------------

    def _on_deadline(self) -> None:
        self.abort()

    def _release(self) -> None:
        with self._release_lock:
            response, client = self._response, self._client
        # Drop the TCP connection first: see _shutdown_socket for why closing
        # only httpx's view of it is not enough.
        self._shutdown_socket(response)
        for target in (response, client):
            if target is None:
                continue
            try:
                target.close()
            except Exception as exc:  # noqa: BLE001 - closing is best effort by design
                self.close_errors.append(f"{type(target).__name__}: {type(exc).__name__}: {exc}")

    def _shutdown_socket(self, response: httpx.Response | None) -> None:
        """Kill the connection at the socket level, not just in httpx's state.

        ``Response.close()`` runs httpcore's state machine. With a read blocked in
        another thread on Windows it can leave the connection half-closed: the
        reader thread exits, but the server never sees FIN/RST, keeps the
        subscriber queue and the test "leaks" past its own end. ``SHUT_RDWR`` is
        unambiguous — the peer is told the connection is gone and any blocked
        read fails immediately.
        """
        if response is None:
            return
        stream = response.extensions.get("network_stream")
        sock = None
        if stream is not None:
            try:
                sock = stream.get_extra_info("socket")
            except Exception:  # noqa: BLE001 - backend may not expose a socket
                sock = None
        if not isinstance(sock, socket.socket):
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _consume(self, line: str) -> bool:
        """Handle one SSE line; return True when the reader should stop."""
        if line.startswith("id:"):
            self._pending_id = line[3:].strip()
            return False
        if line.startswith("data:"):
            self.frames.append(json.loads(line[5:].strip()))
            self.frame_ids.append(self._pending_id)
            # Reset per frame: the question is "did THIS frame carry an id".
            self._pending_id = None
            return self.stop_when(self.frames)
        if line.startswith(":"):
            self.raw_comments.append(line)
            if self.stop_on_comment:
                return True
            if self.stop_when_comment is not None and self.stop_when_comment(self.raw_comments):
                return True
        return False


def _assert_finished(reader: _SSEReader, timeout: float = 10.0) -> None:
    reader.join(timeout)
    assert not reader.is_alive(), "SSE reader thread did not exit"
    assert reader.done.is_set(), "SSE reader never set done"


def _sse_url(api: str) -> str:
    return f"{api}/api/v1/events/stream"


def _open_reader(reader: _SSEReader) -> _SSEReader:
    reader.start()
    assert reader.ready.wait(20), "stream did not open"
    return reader


# ------------------------------------------------------------ data preparation


def _publish_cursor_batch(bus: EventBus, count: int = 5, *, tag: str = "cursor") -> list[RunEvent]:
    """Publish an isolated, contiguous batch and prove it is self-consistent.

    This is the whole isolation fix for §4.1: the batch does not care what
    earlier tests published, and its contiguity is asserted here rather than
    assumed, so a cursor test built on it can run first, last, or alone.
    """
    run_id = f"run_r549_{tag}_{uuid.uuid4().hex[:8]}"
    events = [
        bus.publish("r549.cursor", run_id, "task_r549_cursor", index=index, tag=tag)
        for index in range(count)
    ]
    sequences = [event.sequence for event in events]
    assert sequences == list(range(sequences[0], sequences[0] + count)), sequences
    assert len({event.event_id for event in events}) == count
    assert len(set(sequences)) == count
    return events


def _same_tag(frames: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    return [frame for frame in frames if frame["payload"].get("tag") == tag]


# ---------------------------------------------------------------------- tests


def test_live_events_after_connect_are_pushed(live_platform: dict[str, Any]) -> None:
    """The bug: stream died right after replay, so nothing published later arrived."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)
    tag = uuid.uuid4().hex[:8]
    run_id = f"run_r549_live_{tag}"

    def saw_three_live(frames: list[dict[str, Any]]) -> bool:
        return len([f for f in frames if f["run_id"] == run_id]) >= 3

    reader = _open_reader(_SSEReader(_sse_url(api), stop_when=saw_three_live, max_seconds=20))
    assert reader.status == 200
    assert "text/event-stream" in reader.content_type

    # Only publish once the server has actually subscribed; otherwise the test
    # would pass via replay and prove nothing about live delivery.
    _wait_until(lambda: bus.subscriber_count > baseline, 10, "server-side subscription")

    for index in range(3):
        bus.publish("r549.synthetic", run_id, "task_r549", index=index, tag=tag)

    assert reader.done.wait(20), "live events were not pushed within 20s"
    _assert_finished(reader)
    assert reader.error is None, reader.error

    live = [f for f in reader.frames if f["run_id"] == run_id]
    assert [f["payload"]["index"] for f in live] == [0, 1, 2]
    assert [f["event_type"] for f in live] == ["r549.synthetic"] * 3
    # IDs are unique and ordered: Last-Event-ID cursors stay meaningful.
    assert len({f["event_id"] for f in live}) == 3
    assert [f["sequence"] for f in live] == sorted(f["sequence"] for f in live)


def test_real_execution_events_reach_the_stream(live_platform: dict[str, Any]) -> None:
    """A real run's lifecycle must be observable live (queued -> running -> finished)."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)

    reader = _open_reader(
        _SSEReader(
            _sse_url(api),
            stop_when=lambda frames: any(f["event_type"] == "run.finished" for f in frames),
            max_seconds=90,
        )
    )
    _wait_until(lambda: bus.subscriber_count > baseline, 10, "server-side subscription")

    with httpx.Client(base_url=api, timeout=20, trust_env=False) as client:
        created = client.post(
            "/api/v1/tasks",
            json={"task_id": "T-SSE-RUN", "case_id": "TC-CREATE-001", "title": "sse run"},
        )
        assert created.status_code == 201, created.text
        submitted = client.post("/api/v1/tasks/T-SSE-RUN/runs", json={})
        assert submitted.status_code == 202, submitted.text
        run_id = submitted.json()["run_id"]

    assert reader.done.wait(100), "run.finished never reached the stream"
    _assert_finished(reader)
    assert reader.error is None, reader.error

    events = [f for f in reader.frames if f["run_id"] == run_id]
    types = [f["event_type"] for f in events]
    assert "run.queued" in types, types
    assert "run.running" in types, types
    assert "run.finished" in types, types
    # Terminal event must be the last one for this run.
    assert types[-1] == "run.finished", types


def test_last_event_id_replays_only_later_events(live_platform: dict[str, Any]) -> None:
    """Self-contained: publish our own batch, take a middle cursor, reconnect.

    No dependency on earlier tests (R5-4.9 §4.1) — this passes when run alone.
    """
    api = live_platform["api"]
    bus = live_platform["bus"]

    batch = _publish_cursor_batch(bus, count=5, tag="replay")
    cursor = batch[1]
    expected = batch[2:]

    reader = _open_reader(
        _SSEReader(
            _sse_url(api),
            headers={"Last-Event-ID": cursor.event_id},
            stop_when=lambda frames: len(frames) >= len(expected),
            max_seconds=20,
        )
    )
    assert reader.done.wait(20), "replay did not deliver the expected events"
    _assert_finished(reader)
    assert reader.error is None, reader.error

    replayed = [f["event_id"] for f in reader.frames]
    # At-least-once, strictly after the cursor: no duplicates, no cursor itself,
    # and nothing earlier than the cursor was resurrected.
    assert replayed == [event.event_id for event in expected], replayed
    assert cursor.event_id not in replayed
    assert len(set(replayed)) == len(replayed)
    assert all(f["sequence"] > cursor.sequence for f in reader.frames)


def test_multiple_clients_each_receive_the_same_event(live_platform: dict[str, Any]) -> None:
    """Fan-out: one published event must reach every connected workbench tab."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)
    tag = uuid.uuid4().hex[:8]

    def saw_tag(frames: list[dict[str, Any]]) -> bool:
        return bool(_same_tag(frames, tag))

    first = _open_reader(_SSEReader(_sse_url(api), stop_when=saw_tag, max_seconds=20))
    second = _open_reader(_SSEReader(_sse_url(api), stop_when=saw_tag, max_seconds=20))
    _wait_until(lambda: bus.subscriber_count >= baseline + 2, 10, "both subscribers")

    bus.publish("r549.fanout", "run_r549_fanout", "task_r549", tag=tag)

    assert first.done.wait(20) and second.done.wait(20), "did not fan out to both clients"
    _assert_finished(first)
    _assert_finished(second)
    assert first.error is None and second.error is None

    tagged_first = _same_tag(first.frames, tag)
    tagged_second = _same_tag(second.frames, tag)
    assert tagged_first and tagged_second
    # Same persisted event (identical id) reaches both clients.
    assert tagged_first[0]["event_id"] == tagged_second[0]["event_id"]


def test_keep_alive_comment_keeps_an_idle_stream_open(
    live_platform: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent stream must stay open; the 15s keep-alive is shortened for the test."""
    from apps.api import workbench

    monkeypatch.setattr(workbench, "_SSE_KEEPALIVE_S", 0.5)
    api = live_platform["api"]
    with httpx.Client(base_url=api, timeout=20, trust_env=False) as client:
        max_before = max((e["sequence"] for e in client.get("/api/v1/events").json()), default=0)

    reader = _open_reader(
        _SSEReader(_sse_url(api), stop_on_comment=True, max_seconds=15, read_timeout=10)
    )
    assert reader.done.wait(15), "no keep-alive frame on an idle stream"
    _assert_finished(reader)
    assert reader.error is None, reader.error
    assert any("keep-alive" in comment for comment in reader.raw_comments), reader.raw_comments
    # Everything on the wire is replay; no live event was published during the test.
    assert all(f["sequence"] <= max_before for f in reader.frames)


def test_disconnect_unsubscribes_and_leaks_nothing(live_platform: dict[str, Any]) -> None:
    """Closing the TCP connection must remove the subscriber queue."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)

    host = urlparse(api).hostname or "127.0.0.1"
    port = _port_of(api)
    request = (
        "GET /api/v1/events/stream HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: keep-alive\r\n\r\n"
    )
    sock = socket.create_connection((host, port), timeout=10)
    try:
        sock.sendall(request.encode())
        _wait_until(lambda: bus.subscriber_count > baseline, 10, "subscription to appear")
    finally:
        sock.close()

    _wait_until(
        lambda: bus.subscriber_count <= baseline,
        15,
        "subscriber queue to be released after disconnect",
    )
    # Repeated connect/disconnect cycles must not accumulate subscribers.
    for _ in range(3):
        other = socket.create_connection((host, port), timeout=10)
        other.sendall(request.encode())
        time.sleep(0.2)  # let the server accept and subscribe this connection
        other.close()
    try:
        _wait_until(
            lambda: bus.subscriber_count <= baseline,
            15,
            "no subscriber accumulation across reconnects",
        )
    except AssertionError as exc:
        raise AssertionError(f"{exc} (baseline={baseline}, now={bus.subscriber_count})") from exc


# ------------------------------------------------------- §4.1 independence


def test_each_sse_test_is_order_independent(live_platform: dict[str, Any]) -> None:
    """The cursor test's data preparation must not need prior history at all."""
    bus = live_platform["bus"]
    batch = _publish_cursor_batch(bus, count=4, tag="standalone")
    assert [event.payload["index"] for event in batch] == [0, 1, 2, 3]
    assert audit_event_sequences([event.sequence for event in batch])["contiguous"] is True

    # Same preparation against a brand-new bus: empty history, sequence starts
    # at 1, and the batch is still contiguous and gap-free.
    fresh = EventBus()
    assert audit_event_sequences([]) == {
        "gaps": [],
        "duplicates": [],
        "out_of_order": [],
        "monotonic": True,
        "contiguous": True,
        "count": 0,
    }
    fresh_batch = _publish_cursor_batch(fresh, count=4, tag="fresh")
    assert [event.sequence for event in fresh_batch] == [1, 2, 3, 4]
    assert audit_event_sequences([event.sequence for event in fresh_batch]) == {
        "gaps": [],
        "duplicates": [],
        "out_of_order": [],
        "monotonic": True,
        "contiguous": True,
        "count": 4,
    }


# ------------------------------------------------- §4.2 reader time bounds


def test_reader_total_duration_is_bounded_even_when_the_stream_is_silent(
    live_platform: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that never sends a line must not block the reader forever.

    Keep-alive is pushed out of reach and the read timeout is generous, so the
    *only* way out is the total-duration watchdog. Before the fix this hung.
    """
    from apps.api import workbench

    monkeypatch.setattr(workbench, "_SSE_KEEPALIVE_S", 60.0)
    reader = _open_reader(
        _SSEReader(
            _sse_url(live_platform["api"]),
            max_seconds=2.0,
            connect_timeout=5.0,
            read_timeout=30.0,
        )
    )

    started = time.monotonic()
    assert reader.done.wait(6), "watchdog did not end a silent stream"
    elapsed = time.monotonic() - started
    _assert_finished(reader)
    assert elapsed < 6, elapsed
    assert reader.timed_out is True, "a watchdog abort must be reported as a timeout"
    assert reader.error is None, reader.error
    assert reader.started_at is not None and reader.finished_at is not None


def test_reader_times_out_on_a_keep_alive_only_stream(
    live_platform: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep-alive comments are not events: a data-starved stream must still end."""
    from apps.api import workbench

    monkeypatch.setattr(workbench, "_SSE_KEEPALIVE_S", 0.1)
    api = live_platform["api"]
    # Cursor at the newest event so the stream has nothing to replay: the only
    # traffic before the timeout is keep-alive comments.
    with httpx.Client(base_url=api, timeout=20, trust_env=False) as client:
        history = client.get("/api/v1/events").json()
    headers = {"Last-Event-ID": history[-1]["event_id"]} if history else {}

    reader = _open_reader(
        _SSEReader(
            _sse_url(api),
            headers=headers,
            max_seconds=3.0,
            connect_timeout=5.0,
            read_timeout=0.5,
        )
    )

    started = time.monotonic()
    assert reader.done.wait(5), "reader did not return within 5s"
    elapsed = time.monotonic() - started
    _assert_finished(reader)
    assert elapsed < 5, elapsed
    assert reader.timed_out is True
    assert reader.error is None, reader.error
    assert reader.raw_comments, "keep-alive frames should have been observed"


def test_reader_stop_interrupts_a_blocked_read(live_platform: dict[str, Any]) -> None:
    """A test must be able to close a stream it no longer needs (no hang)."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)
    # Cursor at the newest event: the server has nothing to replay, so the reader
    # parks in a blocking read on an idle stream — the case stop() must break.
    # (Closing a client mid-replay is a different scenario: on Windows the
    # server can stay wedged in a write to the half-dead socket for a while,
    # which is exactly why this test does not depend on that path.)
    with httpx.Client(base_url=api, timeout=20, trust_env=False) as client:
        history = client.get("/api/v1/events").json()
    headers = {"Last-Event-ID": history[-1]["event_id"]} if history else {}

    reader = _open_reader(
        _SSEReader(_sse_url(api), headers=headers, max_seconds=60.0, read_timeout=30.0)
    )
    _wait_until(lambda: bus.subscriber_count > baseline, 10, "server-side subscription")

    reader.stop(timeout=10)
    assert not reader.is_alive(), "stop() did not end a blocked reader"
    assert reader.done.is_set()
    assert reader.timed_out is False, "an explicit stop is not a timeout"
    assert reader.error is None, reader.error

    # `<=` not `==`: a previous test's disconnect may be detected in the
    # meantime, and a *lower* count is not a leak. A higher one is.
    try:
        _wait_until(
            lambda: bus.subscriber_count <= baseline,
            20,
            "subscriber release after reader.stop()",
        )
    except AssertionError as exc:
        raise AssertionError(
            f"{exc} (baseline={baseline}, now={bus.subscriber_count}, "
            f"stats={bus.subscriber_stats()}, close_errors={reader.close_errors})"
        ) from exc


# ------------------------------------------------- §4.3 gap detection


@pytest.mark.parametrize(
    ("sequences", "gaps", "duplicates", "out_of_order", "monotonic", "contiguous"),
    [
        # dense, healthy stream
        ([10, 11, 12, 13], [], [], [], True, True),
        # queue overflow / lost events: 3 and 4 missing
        ([1, 2, 5, 6], [{"from": 3, "to": 4, "count": 2}], [], [], True, False),
        # at-least-once replay delivered 2 twice
        ([1, 2, 2, 3], [], [2], [], False, False),
        # reconnect/redelivery out of order around a gap
        (
            [1, 3, 2, 4],
            [{"from": 2, "to": 2, "count": 1}],
            [],
            [{"index": 2, "previous": 3, "sequence": 2}],
            False,
            False,
        ),
        # nothing observed is not a failure
        ([], [], [], [], True, True),
    ],
    ids=["dense", "gap", "duplicate", "out-of-order", "empty"],
)
def test_audit_event_sequences(
    sequences: list[int],
    gaps: list[dict[str, int]],
    duplicates: list[int],
    out_of_order: list[dict[str, int]],
    monotonic: bool,
    contiguous: bool,
) -> None:
    verdict = audit_event_sequences(sequences)
    assert verdict["gaps"] == gaps
    assert verdict["duplicates"] == duplicates
    assert verdict["out_of_order"] == out_of_order
    assert verdict["monotonic"] is monotonic
    assert verdict["contiguous"] is contiguous
    assert verdict["count"] == len(sequences)


def test_overflowing_subscriber_queue_records_drops(live_platform: dict[str, Any]) -> None:
    """`queue.Full` must be counted, ranged, and reported — never swallowed."""
    bus = live_platform["bus"]
    before_total = bus.dropped_total
    queue = bus.subscribe(maxsize=2)
    try:
        published = [
            bus.publish("r549.overflow", "run_r549_overflow", "task_r549", index=index)
            for index in range(6)
        ]
        # Two fit; the remaining four are dropped and recorded as one range.
        assert queue.dropped == 4, queue.dropped
        assert queue.dropped_ranges == [(published[2].sequence, published[5].sequence)], (
            queue.dropped_ranges
        )
        assert queue.qsize() == 2
        assert bus.dropped_total >= before_total + 4
        stats = bus.subscriber_stats()
        assert any(entry["maxsize"] == 2 and entry["dropped"] == 4 for entry in stats), stats
        assert bus.dropped_ranges(), "bus-level union of drop ranges must be non-empty"
    finally:
        bus.unsubscribe(queue)


def test_gap_comment_frame_reaches_the_wire(
    live_platform: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loss signal must be an SSE *comment* (C05 consumers ignore it)."""
    from apps.api import workbench

    monkeypatch.setattr(workbench, "_SSE_QUEUE_MAXSIZE", 2)
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)
    before_total = bus.dropped_total

    reader = _open_reader(
        _SSEReader(
            _sse_url(api),
            stop_when_comment=lambda comments: any(": gap" in c for c in comments),
            max_seconds=30,
            read_timeout=10,
        )
    )
    _wait_until(lambda: bus.subscriber_count > baseline, 10, "server-side subscription")
    # Give the generator a poll cycle so its replay snapshot is behind us: the
    # burst must arrive as *live* events, otherwise replay would deliver them.
    time.sleep(0.4)

    for index in range(50):
        bus.publish("r549.gap", "run_r549_gap", "task_r549", index=index)

    assert reader.done.wait(30), "no gap comment frame reached the client"
    _assert_finished(reader)
    assert reader.error is None, reader.error
    assert any(": gap" in comment for comment in reader.raw_comments), (
        reader.raw_comments,
        len(reader.frames),
        reader.timed_out,
        bus.subscriber_stats(),
        bus.dropped_total - before_total,
    )
    gap_frames = [c for c in reader.raw_comments if ": gap" in c]
    assert "from=" in gap_frames[0] and "to=" in gap_frames[0] and "dropped=" in gap_frames[0]
    assert bus.dropped_total > before_total
