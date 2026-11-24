"""R5-5.1 P0-2: a persistence outage must not steal a durable sequence number.

The defect this pins down
--------------------------
``EventBus._next_sequence`` used one counter for two different jobs: the durable
stream position and the wire sequence. While the database was failing it handed
out local numbers (say 101, 102); when the database came back it allocated the
*same* numbers to different events. A client whose ``last_seq`` was 102 then
treated the real, persisted event at 101 as "already seen" and skipped it, and
a reconnect with the fabricated cursor skipped it again. Persisted events
disappeared without a single error.

How the failure is produced
---------------------------
Not with a mock. A second SQLite connection takes a real ``BEGIN IMMEDIATE``
write lock on the same database file, so the store's own ``INSERT`` fails with
``OperationalError: database is locked`` exactly as it would during a disk or
lock outage. The event stream is consumed over real HTTP with the same reader
the other SSE tests use. Nothing in the delivery path is stubbed.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.main import build_app
from packages.core.config import Settings
from packages.evidence.events import EventStore, create_event_tables
from packages.evidence.run_events import RunEventRecorder
from packages.execution.orchestrator import EventBus
from tests.netutil import serve_uvicorn
from tests.test_sse_lifecycle import _open_reader, _sse_url, _SSEReader

CASE_DIR = Path("benchmark/cases")
EVAL_TOKEN = "local-dev-eval-token-change-me"
# Short busy timeout so a locked database fails in ~0.1s instead of blocking the
# publish path for the default five seconds.
LOCK_TIMEOUT = "0.05"


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


def _settings(work: Path, *, teamflow_port: int) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{(work / 'traceguard.db').as_posix()}?timeout={LOCK_TIMEOUT}",
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
    with serve_uvicorn("apps.teamflow.main:app") as base:
        _wait_until(lambda: _health(base), 30, "teamflow health")
        yield base


@contextlib.contextmanager
def _platform(work: Path, teamflow_base: str) -> Iterator[dict[str, Any]]:
    port = urlparse(teamflow_base).port
    assert port is not None
    app = build_app(_settings(work, teamflow_port=port))
    with serve_uvicorn(app) as base:
        _wait_until(lambda: _health(base), 30, "platform health")
        yield {"api": base, "app": app, "bus": app.state.orchestrator.bus}


@contextlib.contextmanager
def _write_outage(db_path: Path) -> Iterator[None]:
    """Real write lock on the real database file: writers fail, readers work."""
    holder = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield
    finally:
        with contextlib.suppress(sqlite3.Error):
            holder.execute("ROLLBACK")
        holder.close()


def _payload_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame for frame in frames if "event_id" in frame]


def _notice_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame for frame in frames if "code" in frame and "event_id" not in frame]


def test_cursor_and_delivery_identities_stay_disjoint_across_an_outage(
    tmp_path: Path, teamflow: str
) -> None:
    """The full scenario from the review: 100 + 2 (outage) + 3 (recovered)."""
    db_path = tmp_path / "traceguard.db"
    with _platform(tmp_path, teamflow) as platform:
        bus: EventBus = platform["bus"]
        api: str = platform["api"]

        # ---- 1. 100 events, all persisted ---------------------------------
        baseline = [
            bus.publish("run.progress", "run_1", "task_1", index=index) for index in range(100)
        ]
        assert all(event.durable for event in baseline)
        assert [event.sequence for event in baseline] == list(range(1, 101))
        assert [event.delivery_seq for event in baseline] == list(range(1, 101))

        # A live client attached at the tip: it must see everything after this,
        # including the events that cannot be persisted.
        reader = _open_reader(
            _SSEReader(
                _sse_url(api),
                headers={"Last-Event-ID": bus.cursor_token(baseline[-1]) or ""},
                stop_when=lambda frames: len(_payload_frames(frames)) >= 5,
                max_seconds=40,
            )
        )

        # ---- 2. outage: 2 events that cannot be written -------------------
        with _write_outage(db_path):
            undurable = [
                bus.publish("run.progress", "run_1", "task_1", index=100),
                bus.publish("run.progress", "run_1", "task_1", index=101),
            ]
            degraded_state = bus.durability_state()

        for event in undurable:
            assert event.durable is False
            assert event.sequence is None, "no durable number may exist without a row"
            assert bus.cursor_token(event) is None, "no cursor may be fabricated"
        assert [event.delivery_seq for event in undurable] == [101, 102]
        assert degraded_state["mode"] == "degraded"
        assert degraded_state["failures"] == 2
        assert degraded_state["undurable_count"] == 2
        assert degraded_state["undurable_first_delivery"] == 101
        assert degraded_state["undurable_last_delivery"] == 102
        assert degraded_state["last_durable_sequence"] == 100

        # ---- 3./4. recovery, then 3 more events ---------------------------
        recovered = [
            bus.publish("run.progress", "run_1", "task_1", index=index) for index in (102, 103, 104)
        ]

        # ---- 5. identity and order ---------------------------------------
        assert [event.sequence for event in recovered] == [101, 102, 103], (
            "the database resumes at 101 — a number the undurable events must never have occupied"
        )
        assert [event.delivery_seq for event in recovered] == [103, 104, 105]
        everything = baseline + undurable + recovered
        assert len({event.event_id for event in everything}) == len(everything)
        assert len({event.delivery_seq for event in everything}) == len(everything)
        durable_positions = [e.sequence for e in everything if e.sequence is not None]
        assert len(set(durable_positions)) == len(durable_positions), (
            "two events sharing a durable position is the defect being fixed"
        )

        # ---- 6. what the live client actually received --------------------
        assert reader.done.wait(40), "live stream did not deliver the outage tail"
        reader.stop()
        assert reader.error is None, reader.error
        live = _payload_frames(reader.frames)
        assert [frame["payload"]["index"] for frame in live] == [100, 101, 102, 103, 104]
        assert len({frame["event_id"] for frame in live}) == 5, "no duplicate delivery"

        ids = [
            frame_id
            for frame_id, frame in zip(reader.frame_ids, reader.frames, strict=True)
            if "event_id" in frame
        ]
        instance = bus.producer_instance_id
        # The two undurable frames carry no id at all: the browser keeps the last
        # durable cursor instead of adopting a number that does not exist yet.
        assert ids[0] is None and ids[1] is None
        assert ids[2] == f"v2:101:{instance}"
        assert ids[3] == f"v2:102:{instance}"
        assert ids[4] == f"v2:103:{instance}"

        # The event the old code silently dropped is present on the wire.
        persisted_at_101 = [frame for frame in live if frame["sequence"] == 101]
        assert [frame["payload"]["index"] for frame in persisted_at_101] == [102]

        # ---- 7./8. disconnect, reconnect with the last durable cursor -----
        resumed = _open_reader(
            _SSEReader(
                _sse_url(api),
                headers={"Last-Event-ID": f"v2:100:{instance}"},
                stop_when=lambda frames: len(_payload_frames(frames)) >= 3,
                max_seconds=40,
            )
        )
        assert resumed.done.wait(40), "reconnect did not replay the recovered tail"
        resumed.stop()
        assert resumed.error is None, resumed.error
        replayed = _payload_frames(resumed.frames)
        assert [frame["payload"]["index"] for frame in replayed] == [102, 103, 104], (
            "resuming from the last durable position must replay every persisted "
            "event after it — and must not invent the two undurable ones"
        )
        assert all(frame["durable"] is True for frame in replayed)

        # ---- 9./10. no duplicate, no silent loss, state is explicit -------
        with httpx.Client(base_url=api, timeout=10, trust_env=False) as client:
            state = client.get("/api/v1/events/durability").json()
            history = client.get("/api/v1/events", params={"limit": 500}).json()

        assert state["mode"] == "durable", "a successful write restores durability"
        assert state["recovered"] is True
        assert state["recovered_at_delivery"] == 103
        assert state["failures"] == 2
        assert state["undurable_count"] == 2
        assert state["undurable_first_delivery"] == 101
        assert state["undurable_last_delivery"] == 102
        assert state["last_durable_sequence"] == 103
        stored = [row for row in history if row["event_type"] == "run.progress"]
        assert [row["payload"]["index"] for row in stored] == list(range(100)) + [102, 103, 104]
        assert all(row["durable"] for row in stored)


def test_strict_mode_aborts_and_does_not_burn_a_position(tmp_path: Path) -> None:
    """``strict`` must fail loudly and leave no hole in the durable space."""
    db_path = tmp_path / "strict.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}?timeout={LOCK_TIMEOUT}",
        connect_args={"check_same_thread": False},
    )
    create_event_tables(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    recorder = RunEventRecorder(EventStore(factory), producer_instance_id="bus-strict")
    bus = EventBus(recorder, strict_persistence=True)
    try:
        first = bus.publish("run.queued", "run_1", "task_1", index=0)
        assert first.sequence == 1

        with _write_outage(db_path):
            with pytest.raises(Exception) as excinfo:  # noqa: B017 - driver-specific
                bus.publish("run.queued", "run_1", "task_1", index=1)
            assert "locked" in str(excinfo.value).lower()
            # A rejected publish is not a delivery: no ordinal, no history entry.
            assert bus.delivery_tip() == 1
            assert [event.event_id for event in bus.history()] == [first.event_id]

        after = bus.publish("run.queued", "run_1", "task_1", index=2)
        assert after.sequence == 2, "no number was burned by the failed attempt"
        assert after.delivery_seq == 2, "and no wire ordinal was burned either"
        assert bus.replay_after(0)[-1].sequence == 2
    finally:
        engine.dispose()


def test_degrade_mode_keeps_delivering_while_the_write_fails(tmp_path: Path) -> None:
    """``degrade`` must not stop execution, and must not pretend either."""
    db_path = tmp_path / "degrade.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}?timeout={LOCK_TIMEOUT}",
        connect_args={"check_same_thread": False},
    )
    create_event_tables(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    recorder = RunEventRecorder(EventStore(factory), producer_instance_id="bus-degrade")
    bus = EventBus(recorder)
    queue = bus.subscribe()
    try:
        with _write_outage(db_path):
            event = bus.publish("run.queued", "run_1", "task_1", index=0)
            assert event.durable is False
            assert bus.replay_after(0) == [], "nothing to replay, and it says so"
            # The event still reached the live subscriber.
            assert queue.get_nowait().event_id == event.event_id
            snapshot = bus.persistence_snapshot()
            assert snapshot["mode"] == "degraded"
            assert snapshot["last_durable_sequence"] == 0
            assert snapshot["undurable_first_delivery"] == 1

        second = bus.publish("run.queued", "run_1", "task_1", index=1)
        assert second.sequence == 1, "the recovered write takes the first position"
        assert second.delivery_seq == 2
        snapshot = bus.persistence_snapshot()
        assert snapshot["mode"] == "durable"
        assert snapshot["recovered"] is True
        assert snapshot["undurable_count"] == 1
    finally:
        bus.unsubscribe(queue)
        engine.dispose()


def test_two_independent_outages_keep_separate_undurable_intervals(tmp_path: Path) -> None:
    """R5-5.1-R R4: a successful write between two faults splits the holes.

    The single first/last pair is now only an aggregate and must NOT be read as
    one continuous undurable range: the deliveries in between were persisted
    and are replayable. Each outage gets its own bounded interval, closed at
    the delivery where that specific outage recovered.
    """
    db_path = tmp_path / "two-outages.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}?timeout={LOCK_TIMEOUT}",
        connect_args={"check_same_thread": False},
    )
    create_event_tables(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    recorder = RunEventRecorder(EventStore(factory), producer_instance_id="bus-two-outages")
    bus = EventBus(recorder)
    try:
        [bus.publish("run.progress", "r", "t", index=i) for i in range(100)]

        with _write_outage(db_path):
            f1 = [bus.publish("run.progress", "r", "t", index=i) for i in (100, 101)]
        assert all(not e.durable for e in f1)
        middle = [bus.publish("run.progress", "r", "t", index=i) for i in (102, 103, 104)]
        assert all(e.durable for e in middle)  # the successful writes really persisted

        with _write_outage(db_path):
            f2 = [bus.publish("run.progress", "r", "t", index=i) for i in (105, 106, 107)]
        assert all(not e.durable for e in f2)
        tail = [bus.publish("run.progress", "r", "t", index=i) for i in range(108, 113)]

        snapshot = bus.persistence_snapshot()
        intervals = snapshot["undurable_intervals"]
        assert len(intervals) == 2, snapshot
        # Intervals are in the delivery/wire domain.
        assert intervals[0] == {
            "first": 101,
            "last": 102,
            "count": 2,
            "recovered_at": 103,
        }, intervals[0]
        assert intervals[1] == {
            "first": 106,
            "last": 108,
            "count": 3,
            "recovered_at": 109,
        }, intervals[1]
        # The durably-persisted deliveries between the outages belong to no hole.
        recovered_deliveries = {e.delivery_seq for e in middle}
        covered = {d for iv in intervals for d in range(iv["first"], iv["last"] + 1)}
        assert covered.isdisjoint(recovered_deliveries), (
            "a successful write between outages must not be labelled undurable"
        )
        # Aggregate fields remain honest and backward compatible.
        assert snapshot["undurable_first_delivery"] == 101
        assert snapshot["undurable_last_delivery"] == 108
        assert snapshot["undurable_count"] == 5
        assert snapshot["recovered_at_delivery"] == 109
        # Total durable positions stay contiguous: 100 + 3 + 5 = 108 events.
        durable = [e.sequence for e in bus.history() + tail if e.sequence is not None]
        assert len(set(durable)) == 108
        assert set(durable) == set(range(1, 109))
    finally:
        engine.dispose()
