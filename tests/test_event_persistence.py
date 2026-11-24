"""R5-5.1 durability tests against a real SQLite database.

No mocks for the storage path: every assertion reads back what the database
actually holds. The only injected failure is an explicit ``append_stream``
exception, which is the point of the "a failed write is never a success" tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from packages.evidence.events import (
    EventRow,
    EventStore,
    ProducerInstance,
    StreamCursorRow,
    create_event_tables,
)
from packages.evidence.run_events import CURSOR_PREFIX, RunEventRecorder
from packages.execution.orchestrator import EventBus


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[dict[str, Any]]:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'events.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    create_event_tables(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"engine": engine, "factory": factory, "store": EventStore(factory), "path": tmp_path}
    engine.dispose()


def _recorder(db: dict[str, Any], instance: str = "bus-test") -> RunEventRecorder:
    recorder = RunEventRecorder(db["store"], producer_instance_id=instance)
    recorder.start()
    return recorder


def _append(
    recorder: RunEventRecorder,
    index: int = 0,
    *,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[str, int]:
    event_id = event_id or f"runevt_test{index:04d}"
    position = recorder.append(
        event_id=event_id,
        event_type="run.progress",
        run_id=f"run_{index}",
        task_id="task_1",
        payload=payload or {"index": index},
        occurred_at="2026-01-01T00:00:00+00:00",
    )
    return event_id, position


def test_appended_event_is_readable_from_the_database(db: dict[str, Any]) -> None:
    recorder = _recorder(db)
    event_id, position = _append(recorder, 1)

    stored = db["store"].by_id(event_id)

    assert stored is not None
    assert position == 1
    assert stored["sequence"] == position
    assert stored["context_refs"]["run_id"] == "run_1"
    assert stored["context_refs"]["task_id"] == "task_1"
    assert stored["payload"] == {"index": 1}
    # Trusted context is written by the server, not copied from the payload.
    assert stored["context_refs"]["workspace_id"] == "ws-default"


def test_duplicate_event_id_does_not_create_a_second_event(db: dict[str, Any]) -> None:
    recorder = _recorder(db)
    event_id, first = _append(recorder, 1)
    _, second = _append(recorder, 1)

    rows = db["store"].replay_stream(0, 100)

    assert first == second
    assert len(rows) == 1
    assert rows[0][1]["event_id"] == event_id


def test_workspace_id_in_payload_cannot_override_trusted_context(db: dict[str, Any]) -> None:
    recorder = _recorder(db, instance="bus-trusted")
    event_id, _ = _append(recorder, 1, payload={"workspace_id": "ws-evil"})

    stored = db["store"].by_id(event_id)

    assert stored is not None
    # The trusted value wins; the payload's claim is inert data, never authority.
    assert stored["context_refs"]["workspace_id"] == "ws-default"
    assert stored["payload"]["workspace_id"] == "ws-evil"


def test_positions_are_monotonic_and_replay_is_ordered(db: dict[str, Any]) -> None:
    recorder = _recorder(db)
    positions = [_append(recorder, index)[1] for index in range(4)]

    assert positions == sorted(positions)
    assert len(set(positions)) == 4
    replayed = recorder.replay(0, 100)

    assert [row["payload"]["index"] for row in replayed] == [0, 1, 2, 3]
    assert [row["sequence"] for row in replayed] == positions


def test_restart_continues_the_sequence_and_keeps_history(db: dict[str, Any]) -> None:
    first = _recorder(db, instance="bus-before-restart")
    _append(first, 0)
    _append(first, 1)

    # Simulate a restart: a new process means a new producer instance id.
    second = _recorder(db, instance="bus-after-restart")
    assert second.latest() == 2

    _, position = _append(second, 2)

    assert position == 3, "a restart must not restart the stream at 1"
    assert [row["payload"]["index"] for row in second.replay(0, 100)] == [0, 1, 2]


def test_legacy_event_id_cursor_resolves_to_its_position(db: dict[str, Any]) -> None:
    recorder = _recorder(db)
    event_id, position = _append(recorder, 5)

    resolution = recorder.resolve(event_id)

    assert resolution.kind == "ok"
    assert resolution.after == position


def test_v2_cursor_resolves_and_out_of_range_is_unknown(db: dict[str, Any]) -> None:
    recorder = _recorder(db)
    _, position = _append(recorder, 0)

    ok = recorder.resolve(recorder.cursor_token(position))
    beyond = recorder.resolve(f"{CURSOR_PREFIX}9999:bus-elsewhere")

    assert ok.kind == "ok" and ok.after == position
    assert beyond.kind == "unknown"
    assert beyond.after == 1, "an unknown cursor must not replay from zero"
    assert "beyond" in beyond.detail


def test_malformed_cursor_is_unknown_not_crash(db: dict[str, Any]) -> None:
    recorder = _recorder(db)
    _append(recorder, 0)

    resolution = recorder.resolve(f"{CURSOR_PREFIX}not-a-number:x")

    assert resolution.kind == "unknown"
    assert resolution.after == 1


def test_retention_prunes_events_and_expires_old_cursors(db: dict[str, Any]) -> None:
    recorder = RunEventRecorder(db["store"], producer_instance_id="bus-prune", retention_events=2)
    recorder.start()
    positions = [_append(recorder, index)[1] for index in range(5)]

    recorder.store.prune_stream(2)

    stats = db["store"].stream_stats()
    assert stats["oldest"] == 4
    assert stats["pruned_through"] == 3
    assert stats["retained"] == 2

    expired = recorder.resolve(recorder.cursor_token(positions[0]))
    assert expired.kind == "expired"
    assert expired.after == 0, "an expired cursor replays what is still retained"
    assert [row["payload"]["index"] for row in recorder.replay(0, 100)] == [3, 4]


def test_prune_keeps_producer_registry(db: dict[str, Any]) -> None:
    recorder = _recorder(db, instance="bus-registry")
    for index in range(3):
        _append(recorder, index)

    db["store"].prune_stream(1)

    with db["factory"]() as session:
        instance = session.get(ProducerInstance, "bus-registry")
    assert instance is not None, "the audit trail of who produced events must survive"


def test_legacy_rows_without_cursor_are_backfilled(db: dict[str, Any]) -> None:
    """A pre-R5-5.1 row must get a position instead of vanishing from replay."""
    db["store"].append(
        event_id="runevt_legacy0001",
        event_type="run.legacy",
        workspace_id="ws-default",
        producer_id="traceguard.eventbus",
        producer_instance_id="bus-legacy",
        sequence=1,
        context_refs={"run_id": "run_legacy", "task_id": "task_legacy"},
        payload={"legacy": True},
    )
    with db["factory"]() as session:
        assert session.scalar(select(StreamCursorRow)) is None

    backfilled = db["store"].backfill_stream_cursors()

    assert backfilled == 1
    assert db["store"].stream_position("runevt_legacy0001") == 1
    assert [row["event_id"] for _, row in db["store"].replay_stream(0, 10)] == [
        "runevt_legacy0001"
    ]


# --------------------------------------------------------------------- EventBus


def test_bus_continues_after_restart_and_replays_as_run_events(db: dict[str, Any]) -> None:
    before = EventBus(_recorder(db, instance="bus-a"))
    first = before.publish("run.queued", "run_1", "task_1", attempt=1)
    assert first.sequence == 1

    after = EventBus(_recorder(db, instance="bus-b"))
    assert after.latest_sequence() == 1
    second = after.publish("run.running", "run_1", "task_1", attempt=1)

    assert second.sequence == 2
    replayed = after.replay_after(0)
    assert [event.event_type for event in replayed] == ["run.queued", "run.running"]
    assert replayed[0].run_id == "run_1"
    assert replayed[0].task_id == "task_1"
    assert after.durability_state()["mode"] == "durable"


def test_failed_write_degrades_and_is_never_reported_as_durable(
    db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(db)

    def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(recorder.store, "append_stream", boom)
    bus = EventBus(recorder)

    event = bus.publish("run.queued", "run_1", "task_1")

    state = bus.durability_state()
    assert state["mode"] == "degraded"
    assert state["failures"] == 1
    assert "disk full" in state["last_error"]
    assert state["first_undurable_sequence"] == event.sequence
    # The event still reaches live clients (execution must not stop)...
    assert [e.event_id for e in bus.history()] == [event.event_id]
    # ...but nothing was written, so nothing can be replayed later.
    assert bus.replay_after(0) == []


def test_strict_mode_raises_instead_of_pretending(
    db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _recorder(db)

    def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(recorder.store, "append_stream", boom)
    bus = EventBus(recorder, strict_persistence=True)

    with pytest.raises(RuntimeError, match="database is gone"):
        bus.publish("run.queued", "run_1", "task_1")
    assert bus.history() == [], "a rejected publish must not appear as delivered"


def test_memory_mode_is_unchanged(db: dict[str, Any]) -> None:
    bus = EventBus()

    first = bus.publish("run.queued", "run_1", "task_1")
    second = bus.publish("run.running", "run_1", "task_1")

    assert (first.sequence, second.sequence) == (1, 2)
    assert bus.durability_state()["mode"] == "memory"
    assert bus.resolve_cursor(first.event_id).kind == "ok"
    assert bus.resolve_cursor("runevt_missing").kind == "unknown"
    assert bus.replay_after(0) == [first, second]


def test_persistence_never_writes_outside_the_event_tables(db: dict[str, Any]) -> None:
    """Sanity: one append = one event row + one cursor row, nothing else."""
    recorder = _recorder(db)
    _append(recorder, 0)

    with db["factory"]() as session:  # type: Session
        assert session.query(EventRow).count() == 1
        assert session.query(StreamCursorRow).count() == 1


# ------------------------------------------------- R5-5.1 defect #2 regression


def test_replay_page_advances_by_position_not_by_event_sequence(db: dict[str, Any]) -> None:
    """A row whose own ``sequence`` is smaller than its position must not trap replay.

    ``chain_api`` writes evidence events with a per-run sequence through
    ``EventStore.append``; once they get a stream position, ``sequence`` and
    position disagree. A replay loop that advances by ``sequence`` re-reads the
    same page forever (it blocked the whole API event loop in the browser gate).
    """
    recorder = _recorder(db)
    _append(recorder, 0)
    _append(recorder, 1)

    # A foreign producer writing with its own sequence space (the chain shape).
    db["store"].append(
        event_id="runevt_chain0001",
        event_type="evidence.captured",
        workspace_id="ws-default",
        producer_id="traceguard.eventbus",
        producer_instance_id="run-legacy-instance",
        sequence=1,
        context_refs={"run_id": "run_chain", "task_id": "task_chain"},
        payload={"kind": "screenshot"},
    )
    db["store"].backfill_stream_cursors()

    rows, position = recorder.replay_page(0, 100)

    assert len(rows) == 3
    assert position == 3
    assert rows[-1]["sequence"] == 1, "the row keeps its own sequence"
    # The next page is empty and does not re-deliver the same rows.
    assert recorder.replay_page(position, 100) == ([], position)


def test_replay_ignores_other_producers_events(db: dict[str, Any]) -> None:
    """The run-event stream must not replay the evidence plane's events."""
    recorder = _recorder(db)
    _append(recorder, 0)
    db["store"].append(
        event_id="runevt_evidence0001",
        event_type="evidence.captured",
        workspace_id="ws-default",
        producer_id="local_runner",
        producer_instance_id="runner-1",
        sequence=99,
        context_refs={"run_id": "run_x", "task_id": "task_x"},
        payload={"kind": "dom"},
    )
    db["store"].backfill_stream_cursors()

    rows = recorder.replay(0, 100)

    assert [row["event_type"] for row in rows] == ["run.progress"]
    assert all(row["producer_id"] == "traceguard.eventbus" for row in rows)
