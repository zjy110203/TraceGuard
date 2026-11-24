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
4. closing the connection unsubscribes the queue (no leak, no growth).

They run against uvicorn on dynamically allocated ports so they can be repeated
and run in parallel without ``Address already in use``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from apps.api.main import build_app
from packages.core.config import Settings

EVAL_TOKEN = "local-dev-eval-token-change-me"
CASE_DIR = Path("benchmark/cases")


# --------------------------------------------------------------- infrastructure


def _free_port() -> int:
    """Bind :0 and read back the port; the OS guarantees it is unused now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


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


def _serve(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def _stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=15)
    assert not thread.is_alive(), "uvicorn server did not stop"


@pytest.fixture(scope="module")
def live_platform(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[dict[str, Any], None, None]:
    """A real TeamFlow + real platform API, both on OS-assigned ports."""
    work = tmp_path_factory.mktemp("sse")
    tf_port = _free_port()
    api_port = _free_port()
    tf_base = f"http://127.0.0.1:{tf_port}"

    tf_server, tf_thread = _serve("apps.teamflow.main:app", tf_port)  # type: ignore[arg-type]
    api_server: uvicorn.Server | None = None
    api_thread: threading.Thread | None = None
    try:
        _wait_until(
            lambda: _health(tf_base),
            timeout=20,
            what="TeamFlow health",
        )
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{(work / 'traceguard.db').as_posix()}",
            teamflow_host="127.0.0.1",
            teamflow_port=tf_port,
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
        api_server, api_thread = _serve(app, api_port)
        api_base = f"http://127.0.0.1:{api_port}"
        _wait_until(lambda: _health(api_base), timeout=20, what="platform health")
        yield {
            "api": api_base,
            "app": app,
            "bus": app.state.orchestrator.bus,
        }
    finally:
        if api_server is not None and api_thread is not None:
            _stop(api_server, api_thread)
        _stop(tf_server, tf_thread)


def _health(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


class _SSEReader(threading.Thread):
    """Reads an SSE stream until ``stop_when`` is satisfied (or the deadline)."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stop_when: Callable[[list[dict[str, Any]]], bool] | None = None,
        stop_on_comment: bool = False,
        max_seconds: float = 45.0,
    ) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.headers = headers or {}
        self.stop_when = stop_when or (lambda frames: False)
        self.stop_on_comment = stop_on_comment
        self.max_seconds = max_seconds
        self.frames: list[dict[str, Any]] = []
        self.raw_comments: list[str] = []
        self.status: int | None = None
        self.content_type: str = ""
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self.done = threading.Event()

    def run(self) -> None:
        deadline = time.monotonic() + self.max_seconds
        try:
            with httpx.Client(timeout=None, trust_env=False) as client:
                with client.stream("GET", self.url, headers=self.headers) as resp:
                    self.status = resp.status_code
                    self.content_type = resp.headers.get("content-type", "")
                    self.ready.set()
                    for line in resp.iter_lines():
                        if line.startswith("data:"):
                            self.frames.append(json.loads(line[5:].strip()))
                            if self.stop_when(self.frames):
                                break
                        elif line.startswith(":"):
                            self.raw_comments.append(line)
                            if self.stop_on_comment:
                                break
                        if time.monotonic() > deadline:
                            break
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            self.error = exc
        finally:
            self.done.set()


def _sse_url(api: str) -> str:
    return f"{api}/api/v1/events/stream"


def _read_tail(api: str, headers: dict[str, str] | None = None, n: int = 1) -> _SSEReader:
    reader = _SSEReader(_sse_url(api), headers=headers, stop_when=lambda f: len(f) >= n)
    reader.start()
    assert reader.ready.wait(20), "stream did not open"
    return reader


# ---------------------------------------------------------------------- tests


def test_live_events_after_connect_are_pushed(live_platform: dict[str, Any]) -> None:
    """The bug: stream died right after replay, so nothing published later arrived."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = bus.subscriber_count

    reader = _SSEReader(
        _sse_url(api),
        stop_when=lambda frames: len(frames) >= 3,
        max_seconds=20,
    )
    reader.start()
    assert reader.ready.wait(20), "stream did not open"
    assert reader.status == 200
    assert "text/event-stream" in reader.content_type

    # Only publish once the server has actually subscribed; otherwise the test
    # would pass via replay and prove nothing about live delivery.
    _wait_until(lambda: bus.subscriber_count > baseline, 10, "server-side subscription")

    for index in range(3):
        bus.publish("r549.synthetic", f"run_r549_{index}", "task_r549", index=index)

    assert reader.done.wait(20), "live events were not pushed within 20s"
    assert reader.error is None, reader.error
    assert [f["payload"]["index"] for f in reader.frames] == [0, 1, 2]
    assert [f["event_type"] for f in reader.frames] == ["r549.synthetic"] * 3
    # IDs are unique and ordered: Last-Event-ID cursors stay meaningful.
    assert len({f["event_id"] for f in reader.frames}) == 3
    assert [f["sequence"] for f in reader.frames] == sorted(f["sequence"] for f in reader.frames)


def test_real_execution_events_reach_the_stream(live_platform: dict[str, Any]) -> None:
    """A real run's lifecycle must be observable live (queued -> running -> finished)."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = bus.subscriber_count

    reader = _SSEReader(
        _sse_url(api),
        stop_when=lambda frames: any(f["event_type"] == "run.finished" for f in frames),
        max_seconds=90,
    )
    reader.start()
    assert reader.ready.wait(20), "stream did not open"
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
    assert reader.error is None, reader.error
    events = [f for f in reader.frames if f["run_id"] == run_id]
    types = [f["event_type"] for f in events]
    assert "run.queued" in types, types
    assert "run.running" in types, types
    assert "run.finished" in types, types
    # Terminal event must be the last one for this run.
    assert types[-1] == "run.finished", types


def test_last_event_id_replays_only_later_events(live_platform: dict[str, Any]) -> None:
    api = live_platform["api"]
    with httpx.Client(base_url=api, timeout=20, trust_env=False) as client:
        history = client.get("/api/v1/events").json()
    assert len(history) >= 3, "expected prior events from earlier tests"
    cursor = history[1]
    later = [e for e in history if e["sequence"] > cursor["sequence"]]

    reader = _read_tail(api, headers={"Last-Event-ID": cursor["event_id"]}, n=len(later))
    assert reader.done.wait(20), "replay did not deliver the expected events"
    assert reader.error is None, reader.error
    replayed = [f["event_id"] for f in reader.frames]
    # At-least-once: replay starts strictly after the cursor, no duplicates.
    assert all(f["sequence"] > cursor["sequence"] for f in reader.frames)
    assert len(set(replayed)) == len(replayed)
    assert cursor["event_id"] not in replayed


def test_multiple_clients_each_receive_the_same_event(live_platform: dict[str, Any]) -> None:
    """Fan-out: one published event must reach every connected workbench tab."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)

    first = _SSEReader(_sse_url(api), stop_when=lambda f: any(x["payload"].get("tag") for x in f))
    second = _SSEReader(_sse_url(api), stop_when=lambda f: any(x["payload"].get("tag") for x in f))
    first.start()
    second.start()
    assert first.ready.wait(20) and second.ready.wait(20)
    _wait_until(lambda: bus.subscriber_count >= baseline + 2, 10, "both subscribers")

    bus.publish("r549.fanout", "run_r549_fanout", "task_r549", tag="same")

    assert first.done.wait(20) and second.done.wait(20), "did not fan out to both clients"
    assert first.error is None and second.error is None
    tagged_first = [f for f in first.frames if f["payload"].get("tag") == "same"]
    tagged_second = [f for f in second.frames if f["payload"].get("tag") == "same"]
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
    reader = _SSEReader(_sse_url(api), stop_on_comment=True, max_seconds=15)
    reader.start()
    assert reader.ready.wait(20)
    assert reader.done.wait(15), "no keep-alive frame on an idle stream"
    assert reader.error is None, reader.error
    assert any("keep-alive" in comment for comment in reader.raw_comments), reader.raw_comments
    # Everything on the wire is replay; no live event was published during the test.
    assert all(f["sequence"] <= max_before for f in reader.frames)


def test_disconnect_unsubscribes_and_leaks_nothing(live_platform: dict[str, Any]) -> None:
    """Closing the TCP connection must remove the subscriber queue."""
    api = live_platform["api"]
    bus = live_platform["bus"]
    baseline = _settled_subscribers(bus)

    host_port = api.removeprefix("http://")
    host, port = host_port.split(":")
    sock = socket.create_connection((host, int(port)), timeout=10)
    try:
        request = (
            "GET /api/v1/events/stream HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: keep-alive\r\n\r\n"
        )
        sock.sendall(request.encode())
        _wait_until(lambda: bus.subscriber_count > baseline, 10, "subscription to appear")
    finally:
        sock.close()

    _wait_until(
        lambda: bus.subscriber_count == baseline,
        15,
        "subscriber queue to be released after disconnect",
    )
    # Repeated connect/disconnect cycles must not accumulate subscribers.
    for _ in range(3):
        other = socket.create_connection((host, int(port)), timeout=10)
        other.sendall(request.encode())
        time.sleep(0.2)  # let the server accept and subscribe this connection
        other.close()
    try:
        _wait_until(
            lambda: bus.subscriber_count == baseline,
            15,
            "no subscriber accumulation across reconnects",
        )
    except AssertionError as exc:
        raise AssertionError(f"{exc} (baseline={baseline}, now={bus.subscriber_count})") from exc
