"""R5-5.1: SSE recovery over real HTTP with a real restart.

The scenario is the product one: create events -> stream them -> the server
process goes away -> a **new** process opens the same database -> the client
reconnects with ``Last-Event-ID`` and continues without duplicates.

Two real uvicorn servers are started (sequentially, same database file), so
"restart" is an actual new app object with a new producer instance — not a
mocked bus.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from apps.api.main import build_app
from packages.core.config import Settings
from tests.netutil import serve_uvicorn
from tests.test_sse_lifecycle import _open_reader, _SSEReader, _sse_url

CASE_DIR = Path("benchmark/cases")
EVAL_TOKEN = "local-dev-eval-token-change-me"


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _health(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


def _settings(work: Path, *, teamflow_port: int, db_name: str = "traceguard.db") -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{(work / db_name).as_posix()}",
        teamflow_host="127.0.0.1",
        teamflow_port=teamflow_port,
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


@pytest.fixture(scope="module")
def teamflow(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    work = tmp_path_factory.mktemp("sse-recovery-tf")
    with serve_uvicorn("apps.teamflow.main:app") as base:
        _wait_until(lambda: _health(base), 30, "teamflow health")
        yield base
    del work


@contextlib.contextmanager
def _platform(work: Path, teamflow_base: str) -> Iterator[dict[str, Any]]:
    """Start one platform process (new app, same database file)."""
    port = urlparse(teamflow_base).port
    assert port is not None
    app = build_app(_settings(work, teamflow_port=port))
    with serve_uvicorn(app) as base:
        _wait_until(lambda: _health(base), 30, "platform health")
        yield {"api": base, "app": app, "bus": app.state.orchestrator.bus}


def _payload_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only real C05 run events (a stream.notice frame has no ``event_id``)."""
    return [frame for frame in frames if "event_id" in frame]


def _notice_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame for frame in frames if "code" in frame and "event_id" not in frame]


def test_history_and_cursor_survive_a_restart(tmp_path: Path, teamflow: str) -> None:
    with _platform(tmp_path, teamflow) as first:
        published = [
            first["bus"].publish("run.progress", "run_1", "task_1", index=index)
            for index in range(3)
        ]
        cursor = first["bus"].cursor_token(published[1])
        with httpx.Client(base_url=first["api"], timeout=10, trust_env=False) as client:
            stored = client.get("/api/v1/events").json()
        assert [row["payload"]["index"] for row in stored] == [0, 1, 2]

    # --- restart: a brand new app object over the same database file ---------
    with _platform(tmp_path, teamflow) as second:
        assert second["bus"].latest_sequence() == 3, "the new process must resume the stream"
        late = second["bus"].publish("run.progress", "run_1", "task_1", index=3)
        assert late.sequence == 4

        reader = _open_reader(
            _SSEReader(
                _sse_url(second["api"]),
                headers={"Last-Event-ID": cursor},
                stop_when=lambda frames: len(_payload_frames(frames)) >= 2,
                max_seconds=20,
            )
        )
        assert reader.done.wait(20), "replay after restart did not deliver the tail"
        reader.stop()
        assert reader.error is None, reader.error

        replayed = _payload_frames(reader.frames)
        # Exactly what came after the cursor: event 2 (index 2) then live index 3.
        assert [frame["payload"]["index"] for frame in replayed] == [2, 3]
        assert replayed[0]["event_id"] == published[2].event_id
        assert len({frame["event_id"] for frame in replayed}) == 2

        with httpx.Client(base_url=second["api"], timeout=10, trust_env=False) as client:
            durability = client.get("/api/v1/events/durability").json()
        assert durability["mode"] == "durable"
        assert durability["latest"] >= 4


def test_unknown_cursor_is_announced_and_not_replayed(tmp_path: Path, teamflow: str) -> None:
    with _platform(tmp_path, teamflow) as platform:
        for index in range(3):
            platform["bus"].publish("run.progress", "run_2", "task_2", index=index)

        reader = _open_reader(
            _SSEReader(
                _sse_url(platform["api"]),
                headers={"Last-Event-ID": "runevt_thisneverexisted"},
                stop_when=lambda frames: bool(_notice_frames(frames)),
                max_seconds=20,
            )
        )
        assert reader.done.wait(20), "no cursor-unknown notice"
        reader.stop()
        assert reader.error is None, reader.error

        notices = _notice_frames(reader.frames)
        assert notices and notices[0]["code"] == "cursor-unknown"
        assert notices[0]["latest"] == 3
        # The old behaviour replayed everything; that is exactly what must not
        # happen for a cursor the server cannot place.
        assert _payload_frames(reader.frames) == []


def test_expired_cursor_is_announced_and_replays_retained(
    tmp_path: Path, teamflow: str
) -> None:
    with _platform(tmp_path, teamflow) as platform:
        bus = platform["bus"]
        published = [
            bus.publish("run.progress", "run_3", "task_3", index=index) for index in range(4)
        ]
        old_cursor = bus.cursor_token(published[0])
        bus.prune_events(keep_last=1)

        reader = _open_reader(
            _SSEReader(
                _sse_url(platform["api"]),
                headers={"Last-Event-ID": old_cursor},
                stop_when=lambda frames: len(_payload_frames(frames)) >= 1,
                max_seconds=20,
            )
        )
        assert reader.done.wait(20), "no replay after an expired cursor"
        reader.stop()
        assert reader.error is None, reader.error

        notices = _notice_frames(reader.frames)
        assert notices and notices[0]["code"] == "cursor-expired"
        assert notices[0]["pruned_through"] >= 1
        replayed = _payload_frames(reader.frames)
        assert replayed, "an expired cursor must still replay what is retained"
        assert all(frame["sequence"] > notices[0]["pruned_through"] for frame in replayed)


def test_two_clients_replay_different_windows_without_crosstalk(
    tmp_path: Path, teamflow: str
) -> None:
    with _platform(tmp_path, teamflow) as platform:
        bus = platform["bus"]
        published = [
            bus.publish("run.progress", "run_4", "task_4", index=index) for index in range(5)
        ]
        first_cursor = bus.cursor_token(published[0])
        second_cursor = bus.cursor_token(published[3])

        first = _open_reader(
            _SSEReader(
                _sse_url(platform["api"]),
                headers={"Last-Event-ID": first_cursor},
                stop_when=lambda frames: len(_payload_frames(frames)) >= 4,
                max_seconds=20,
            )
        )
        second = _open_reader(
            _SSEReader(
                _sse_url(platform["api"]),
                headers={"Last-Event-ID": second_cursor},
                stop_when=lambda frames: len(_payload_frames(frames)) >= 1,
                max_seconds=20,
            )
        )
        assert first.done.wait(20) and second.done.wait(20), "one client did not finish replay"
        first.stop()
        second.stop()

        assert [frame["payload"]["index"] for frame in _payload_frames(first.frames)] == [
            1,
            2,
            3,
            4,
        ]
        assert [frame["payload"]["index"] for frame in _payload_frames(second.frames)] == [4]


def test_durability_endpoint_reports_degradation(
    tmp_path: Path, teamflow: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _platform(tmp_path, teamflow) as platform:
        bus = platform["bus"]
        recorder = bus._recorder  # noqa: SLF001 - test needs to inject a write failure
        assert recorder is not None

        def boom(**_kwargs: Any) -> Any:
            raise RuntimeError("simulated storage outage")

        monkeypatch.setattr(recorder.store, "append_stream", boom)
        bus.publish("run.progress", "run_5", "task_5", index=0)

        with httpx.Client(base_url=platform["api"], timeout=10, trust_env=False) as client:
            durability = client.get("/api/v1/events/durability").json()
            health = client.get("/health").json()

        assert durability["mode"] == "degraded"
        assert durability["failures"] == 1
        assert "storage outage" in durability["last_error"]
        # The database itself is fine: degrading the event stream must not be
        # reported as a database outage, and vice versa.
        assert health["db"]["ok"] is True
