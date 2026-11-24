"""R5-5.1 P1-1: a v2 cursor must prove *whose* position it is.

Every case runs against a real SQLite file. Two databases are created on disk on
purpose: both start counting at 1, so "position 2 exists here" is not evidence
that a cursor issued elsewhere belongs to this stream.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from packages.evidence.events import EventStore, create_event_tables
from packages.evidence.run_events import CURSOR_PREFIX, RunEventRecorder


def _store_at(path: Path) -> tuple[EventStore, sessionmaker[Session], Engine]:
    engine = create_engine(
        f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False}
    )
    create_event_tables(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return EventStore(factory), factory, engine


def _recorder(store: EventStore, instance: str) -> RunEventRecorder:
    recorder = RunEventRecorder(store, producer_instance_id=instance)
    recorder.start()
    return recorder


def _append(recorder: RunEventRecorder, index: int) -> int:
    return recorder.append(
        event_id=f"runevt_{recorder.producer_instance_id.replace('-', '')}{index:04d}",
        event_type="run.progress",
        run_id=f"run_{index}",
        task_id="task_1",
        payload={"index": index},
        occurred_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture()
def stream_a(tmp_path: Path) -> Iterator[dict[str, Any]]:
    store, factory, engine = _store_at(tmp_path / "a.db")
    yield {"store": store, "factory": factory, "dir": tmp_path, "engine": engine}
    engine.dispose()


def test_historical_instance_of_the_same_store_is_accepted(stream_a: dict[str, Any]) -> None:
    """A cursor from the instance that ran *before the restart* must still work.

    Requiring the token to name the current process instance would turn every
    restart into "unknown cursor" and silently drop the whole backlog.
    """
    before = _recorder(stream_a["store"], "bus-before")
    position = _append(before, 0)
    cursor = before.cursor_token(position)

    after = _recorder(stream_a["store"], "bus-after")
    assert after.producer_instance_id != before.producer_instance_id

    resolution = after.resolve(cursor)

    assert resolution.kind == "ok"
    assert resolution.after == position


def test_unknown_instance_is_rejected_even_with_a_valid_position(
    stream_a: dict[str, Any],
) -> None:
    recorder = _recorder(stream_a["store"], "bus-real")
    position = _append(recorder, 0)

    resolution = recorder.resolve(f"{CURSOR_PREFIX}{position}:bus-imaginary")

    assert resolution.kind == "unknown"
    assert resolution.reason == "unknown-instance"
    assert resolution.after == resolution.latest, "unknown must not replay history"


def test_same_position_from_another_database_is_not_evidence(
    stream_a: dict[str, Any], tmp_path: Path
) -> None:
    """The core of P1-1: identical numbers in two databases prove nothing."""
    store_a = stream_a["store"]
    store_b, _, engine_b = _store_at(tmp_path / "b.db")

    recorder_a = _recorder(store_a, "bus-alpha")
    recorder_b = _recorder(store_b, "bus-gamma")
    position_a = _append(recorder_a, 0)
    position_b = _append(recorder_b, 0)
    assert position_a == position_b == 1, "both databases count from 1 by design"

    foreign_cursor = recorder_a.cursor_token(position_a)

    # Position 1 exists in B as well, but it is B's event, not A's.
    resolution = recorder_b.resolve(foreign_cursor)
    engine_b.dispose()

    assert resolution.kind == "unknown"
    assert resolution.reason == "unknown-instance"


def test_position_beyond_latest_is_unknown(stream_a: dict[str, Any]) -> None:
    recorder = _recorder(stream_a["store"], "bus-real")
    _append(recorder, 0)

    resolution = recorder.resolve(f"{CURSOR_PREFIX}987654:bus-real")

    assert resolution.kind == "unknown"
    assert resolution.reason == "beyond-latest"


def test_pruned_position_is_expired_not_unknown(stream_a: dict[str, Any]) -> None:
    recorder = RunEventRecorder(stream_a["store"], producer_instance_id="bus-prune")
    recorder.start()
    positions = [_append(recorder, index) for index in range(5)]
    recorder.store.prune_stream(2)

    expired = recorder.resolve(recorder.cursor_token(positions[0]))

    assert expired.kind == "expired"
    assert expired.reason == "below-watermark"
    assert expired.after == 0, "an expired cursor replays what is still retained"
    assert expired.pruned_through >= positions[0]


@pytest.mark.parametrize(
    "token",
    [
        f"{CURSOR_PREFIX}",
        f"{CURSOR_PREFIX}abc",
        f"{CURSOR_PREFIX}1",
        f"{CURSOR_PREFIX}1:",
        f"{CURSOR_PREFIX}:bus-real",
        f"{CURSOR_PREFIX}1:bus-real:extra",
        f"{CURSOR_PREFIX}0:bus-real",
        f"{CURSOR_PREFIX}-3:bus-real",
        f"{CURSOR_PREFIX} 1:bus-real",
    ],
)
def test_malformed_cursors_are_unknown(stream_a: dict[str, Any], token: str) -> None:
    recorder = _recorder(stream_a["store"], "bus-real")
    _append(recorder, 0)

    resolution = recorder.resolve(token)

    assert resolution.kind == "unknown"
    assert resolution.reason == "malformed"
    assert resolution.after == resolution.latest


def test_legal_instance_paired_with_someone_elses_position_is_rejected(
    stream_a: dict[str, Any],
) -> None:
    """Both halves valid, combination forged: instance and position must agree."""
    owner = _recorder(stream_a["store"], "bus-owner")
    position = _append(owner, 0)
    # Register a second, legitimate instance that produced nothing.
    intruder = _recorder(stream_a["store"], "bus-intruder")
    assert intruder.resolve(owner.cursor_token(position)).kind == "ok"

    forged = intruder.resolve(f"{CURSOR_PREFIX}{position}:bus-intruder")

    assert forged.kind == "unknown"
    assert forged.reason == "owner-mismatch"
    assert forged.after == forged.latest
