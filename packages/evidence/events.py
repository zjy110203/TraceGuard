"""Stage-4 Event infrastructure (C05, draft schema).

Reliability semantics from v1.1 §阶段4 (implemented literally, not claimed as
exactly-once):

  * At-least-once delivery; consumer-side idempotency via Consumer Inbox.
  * Producer-side ``idempotency_key`` dedup; same key + different payload is a
    CONFLICT (rejected + audited), never silently overwritten.
  * ``sequence`` unique per ``(producer_instance_id, sequence)``; worker restart
    uses a NEW producer_instance_id, so old sequence space is never reused.
  * Gap detection is per producer instance and marks gaps as
    ``PENDING_CONFIRMATION`` (legal skip / in-flight / real loss are NOT
    distinguished) — the system never fabricates a "lost event" conclusion.
  * Outbox couples business commit + event publish; publish is at-least-once.
  * ``workspace_id`` is injected from TRUSTED server context, never from the
    untrusted event payload.

These are draft-contract implementations: field semantics follow C05, but the
JSON Schema is not frozen (C05 status remains draft until review).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.core.errors import ConflictError


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EventBase(DeclarativeBase):
    """Isolated Base so evidence tables don't collide with platform/teamflow."""


class EventRow(EventBase):
    __tablename__ = "ev_event"
    __table_args__ = (
        UniqueConstraint("producer_instance_id", "sequence", name="uq_ev_prod_seq"),
        UniqueConstraint("idempotency_key", name="uq_ev_idem"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String, default="1.0.0-draft", nullable=False)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    context_refs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    producer_id: Mapped[str] = mapped_column(String, nullable=False)
    producer_instance_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "context_refs": self.context_refs,
            "producer_id": self.producer_id,
            "producer_instance_id": self.producer_instance_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "published": bool(self.published),
        }


class InboxRow(EventBase):
    __tablename__ = "ev_inbox"
    __table_args__ = (
        UniqueConstraint("consumer_id", "event_id", name="uq_ev_inbox_consumer_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    consumer_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False)  # SUCCESS | FAILED
    detail: Mapped[str] = mapped_column(String, default="", nullable=False)
    processed_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)


class AuditRow(EventBase):
    __tablename__ = "ev_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)


class ProducerInstance(EventBase):
    __tablename__ = "ev_producer_instance"

    producer_instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    producer_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class StreamCursorRow(EventBase):
    """Durable, database-ordered position of one event in the stream.

    R5-5.1: ``EventRow.observed_at`` is a wall-clock string — two events in the
    same millisecond tie, and a clock adjustment can move it backwards, so it
    cannot be the ordering key for replay. This table is written in the same
    transaction as the event and gets its number from the database, which makes
    the order authoritative without inventing a second event store.

    It is a **cursor index over EventRow**, not a parallel store: every field
    that describes the event still lives in ``ev_event``.
    """

    __tablename__ = "ev_stream_cursor"

    stream_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)


class StreamStateRow(EventBase):
    """Singleton row holding the retention watermark (``pruned_through``)."""

    __tablename__ = "ev_stream_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pruned_through: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


@dataclass(frozen=True)
class InboxResult:
    """Outcome of a consumer processing one event."""

    event_id: str
    consumer_id: str
    outcome: str  # PROCESSED | SKIPPED_DUPLICATE | FAILED
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GapReport:
    """A sequence gap — never asserted as loss (PENDING_CONFIRMATION)."""

    producer_instance_id: str
    missing_sequences: tuple[int, ...]
    status: str = "PENDING_CONFIRMATION"
    note: str = (
        "gap may be a legal skip (rollback/sequence advance), an in-flight "
        "event, or real loss; no conclusion is fabricated"
    )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["missing_sequences"] = list(self.missing_sequences)
        return d


class EventStore:
    """Append-only store with producer-side idempotency and trusted context."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    def register_producer(self, producer_instance_id: str, producer_id: str) -> None:
        """Register a (new) producer instance. Sequence space starts fresh."""
        with self._sessions() as s:
            if s.get(ProducerInstance, producer_instance_id) is None:
                s.add(
                    ProducerInstance(
                        producer_instance_id=producer_instance_id, producer_id=producer_id
                    )
                )
                s.commit()

    def next_sequence(self, producer_instance_id: str) -> int:
        """Atomically allocate the next per-instance sequence."""
        with self._sessions() as s:
            row = s.get(ProducerInstance, producer_instance_id)
            if row is None:
                raise ConflictError(f"unregistered producer instance: {producer_instance_id}")
            row.last_sequence += 1
            s.commit()
            return int(row.last_sequence)

    def append(
        self,
        *,
        event_id: str,
        event_type: str,
        workspace_id: str,
        producer_id: str,
        producer_instance_id: str,
        sequence: int,
        context_refs: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append one event. ``workspace_id`` is TRUSTED context, not payload."""
        refs = dict(context_refs or {})
        # Server-trusted injection overrides any client-supplied value.
        refs["workspace_id"] = workspace_id
        now = _now()
        key = idempotency_key or f"{producer_instance_id}:{sequence}"
        blob = payload or {}
        with self._sessions() as s:
            existing = self._conflict_or_existing(s, key=key, event_type=event_type, blob=blob)
            if existing is not None:
                return existing.to_dict()  # duplicate delivery: no new row
            row = EventRow(
                event_id=event_id,
                event_type=event_type,
                workspace_id=workspace_id,
                context_refs=refs,
                producer_id=producer_id,
                producer_instance_id=producer_instance_id,
                sequence=sequence,
                occurred_at=occurred_at or now,
                observed_at=now,
                idempotency_key=key,
                payload=blob,
                published=0,
            )
            s.add(row)
            s.commit()
            return row.to_dict()

    def _conflict_or_existing(
        self, s: Session, *, key: str, event_type: str, blob: dict[str, Any]
    ) -> EventRow | None:
        """Return the already-stored row for ``key``, or raise on a real conflict."""
        existing = s.scalar(select(EventRow).where(EventRow.idempotency_key == key))
        if existing is None:
            return None
        if existing.payload != blob or existing.event_type != event_type:
            s.add(
                AuditRow(
                    action="IDEMPOTENCY_CONFLICT",
                    detail=f"key={key} event_type={event_type}",
                )
            )
            s.commit()
            raise ConflictError(
                "idempotency_key reused with different payload/event_type",
                details={"idempotency_key": key},
            )
        return existing

    def append_stream(
        self,
        *,
        event_id: str,
        event_type: str,
        workspace_id: str,
        producer_id: str,
        producer_instance_id: str,
        context_refs: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        """Append with a durable stream position, allocated in the same transaction.

        ``sequence`` is NOT passed in: it becomes the database-assigned
        ``stream_seq`` so the wire sequence a client saw and the row on disk can
        never disagree, and a process restart continues the same global order
        instead of restarting at 1.

        Idempotency: the caller supplies ``idempotency_key`` (the run-event
        recorder uses the event_id). Re-appending the same key returns the
        existing row and its stream position — no second row, no third position.
        """
        refs = dict(context_refs or {})
        refs["workspace_id"] = workspace_id
        now = _now()
        key = idempotency_key or f"{producer_instance_id}:{event_id}"
        blob = payload or {}
        with self._sessions() as s:
            existing = self._conflict_or_existing(s, key=key, event_type=event_type, blob=blob)
            if existing is not None:
                position = s.scalar(
                    select(StreamCursorRow.stream_seq).where(
                        StreamCursorRow.event_id == existing.event_id
                    )
                )
                if position is None:
                    # A pre-R5-5.1 row: give it a position instead of silently
                    # returning "no cursor" (which would look like loss).
                    cursor = StreamCursorRow(event_id=existing.event_id)
                    s.add(cursor)
                    s.flush()
                    position = int(cursor.stream_seq)
                    s.commit()
                return existing.to_dict(), int(position)
            cursor = StreamCursorRow(event_id=event_id)
            s.add(cursor)
            s.flush()  # assigns stream_seq from the database counter
            row = EventRow(
                event_id=event_id,
                event_type=event_type,
                workspace_id=workspace_id,
                context_refs=refs,
                producer_id=producer_id,
                producer_instance_id=producer_instance_id,
                sequence=int(cursor.stream_seq),
                occurred_at=occurred_at or now,
                observed_at=now,
                idempotency_key=key,
                payload=blob,
                published=0,
            )
            s.add(row)
            s.flush()
            self._sync_producer_sequence(s, producer_instance_id, int(cursor.stream_seq))
            s.commit()
            return row.to_dict(), int(cursor.stream_seq)

    @staticmethod
    def _sync_producer_sequence(s: Session, instance_id: str, sequence: int) -> None:
        """Keep the per-instance high-water mark truthful (diagnostics only)."""
        instance = s.get(ProducerInstance, instance_id)
        if instance is not None and instance.last_sequence < sequence:
            instance.last_sequence = sequence

    def query(
        self,
        *,
        run_id: str | None = None,
        event_type: str | None = None,
        workspace_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._sessions() as s:
            stmt = select(EventRow)
            if workspace_id is not None:
                stmt = stmt.where(EventRow.workspace_id == workspace_id)
            if event_type is not None:
                stmt = stmt.where(EventRow.event_type == event_type)
            rows = list(s.execute(stmt.order_by(EventRow.observed_at).limit(limit)).scalars().all())
            if run_id is not None:
                rows = [r for r in rows if r.context_refs.get("run_id") == run_id]
            return [r.to_dict() for r in rows]

    def by_id(self, event_id: str) -> dict[str, Any] | None:
        with self._sessions() as s:
            row = s.get(EventRow, event_id)
            return row.to_dict() if row else None

    def mark_published(self, event_id: str) -> None:
        with self._sessions() as s:
            row = s.get(EventRow, event_id)
            if row:
                row.published = 1
                s.commit()

    def audit_log(self) -> list[dict[str, Any]]:
        with self._sessions() as s:
            rows = s.execute(select(AuditRow).order_by(AuditRow.id)).scalars().all()
            return [
                {"action": r.action, "detail": r.detail, "recorded_at": r.recorded_at} for r in rows
            ]

    # ------------------------------------------------------------------ stream
    # R5-5.1: durable ordering / replay / retention. These read and write the
    # same EventRow rows — the cursor table only supplies an order.

    def latest_stream_seq(self) -> int:
        with self._sessions() as s:
            value = s.scalar(select(func.max(StreamCursorRow.stream_seq)))
            return int(value or 0)

    def oldest_stream_seq(self) -> int | None:
        with self._sessions() as s:
            value = s.scalar(select(func.min(StreamCursorRow.stream_seq)))
            return None if value is None else int(value)

    def stream_position(self, event_id: str) -> int | None:
        with self._sessions() as s:
            value = s.scalar(
                select(StreamCursorRow.stream_seq).where(StreamCursorRow.event_id == event_id)
            )
            return None if value is None else int(value)

    def pruned_through(self) -> int:
        with self._sessions() as s:
            state = s.get(StreamStateRow, 1)
            return int(state.pruned_through) if state is not None else 0

    def replay_stream(
        self, after: int, limit: int = 500, *, producer_id: str | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        """Events with ``stream_seq > after`` in durable order (oldest first).

        ``producer_id`` narrows the replay to one producer's stream. The run-event
        stream must do this: the event table is shared with the evidence plane
        (``chain_api`` writes evidence events with their own per-run sequence),
        and replaying those as run events both mixes two sequence spaces and
        makes "advance by sequence" ambiguous.
        """
        if limit <= 0:
            return []
        with self._sessions() as s:
            stmt = (
                select(StreamCursorRow.stream_seq, EventRow)
                .join(EventRow, EventRow.event_id == StreamCursorRow.event_id)
                .where(StreamCursorRow.stream_seq > after)
                .order_by(StreamCursorRow.stream_seq)
                .limit(limit)
            )
            if producer_id is not None:
                stmt = stmt.where(EventRow.producer_id == producer_id)
            rows = s.execute(stmt).all()
            return [(int(seq), row.to_dict()) for seq, row in rows]

    def backfill_stream_cursors(self) -> int:
        """Give pre-R5-5.1 rows a position. Migration, not a silent skip.

        Ordering uses ``(observed_at, sequence, event_id)`` only here: legacy rows
        have no better information, and the result is written once so replay is
        stable afterwards. The count is returned so the caller can log it.
        """
        with self._sessions() as s:
            rows = (
                s.execute(
                    select(EventRow)
                    .outerjoin(StreamCursorRow, StreamCursorRow.event_id == EventRow.event_id)
                    .where(StreamCursorRow.event_id.is_(None))
                    .order_by(EventRow.observed_at, EventRow.sequence, EventRow.event_id)
                )
                .scalars()
                .all()
            )
            for row in rows:
                s.add(StreamCursorRow(event_id=row.event_id))
            s.commit()
            return len(rows)

    def prune_stream(self, keep_last: int) -> dict[str, int]:
        """Drop events older than the retention window; keep the watermark.

        Only ``ev_event`` copies and their cursor rows are removed. Run rows,
        evidence, graph nodes and the audit log are never touched — a pruned
        event means "this old stream copy is gone", not "the run never happened".
        """
        if keep_last < 0:
            raise ValueError(f"keep_last must be >= 0, got {keep_last}")
        latest = self.latest_stream_seq()
        cutoff = latest - keep_last
        if cutoff <= 0:
            return {"pruned": 0, "cutoff": 0, "latest": latest}
        pruned = 0
        with self._sessions() as s:
            state = s.get(StreamStateRow, 1)
            if state is not None and int(state.pruned_through) >= cutoff:
                return {"pruned": 0, "cutoff": cutoff, "latest": latest}
            while True:
                event_ids = list(
                    s.scalars(
                        select(StreamCursorRow.event_id)
                        .where(StreamCursorRow.stream_seq <= cutoff)
                        .limit(500)
                    ).all()
                )
                if not event_ids:
                    break
                s.execute(delete(EventRow).where(EventRow.event_id.in_(event_ids)))
                s.execute(delete(StreamCursorRow).where(StreamCursorRow.event_id.in_(event_ids)))
                s.commit()
                pruned += len(event_ids)
            if state is None:
                s.add(StreamStateRow(id=1, pruned_through=cutoff))
            else:
                state.pruned_through = max(int(state.pruned_through), cutoff)
            s.commit()
        return {"pruned": pruned, "cutoff": cutoff, "latest": latest}

    def stream_stats(self) -> dict[str, Any]:
        with self._sessions() as s:
            retained = int(s.scalar(select(func.count()).select_from(StreamCursorRow)) or 0)
            latest = int(s.scalar(select(func.max(StreamCursorRow.stream_seq))) or 0)
            oldest = s.scalar(select(func.min(StreamCursorRow.stream_seq)))
            state = s.get(StreamStateRow, 1)
        return {
            "retained": retained,
            "latest": latest,
            "oldest": None if oldest is None else int(oldest),
            "pruned_through": int(state.pruned_through) if state is not None else 0,
        }


class Outbox:
    """At-least-once publish: pending events are retried until acknowledged."""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.store._sessions() as s:  # noqa: SLF001 - same infrastructure unit
            rows = (
                s.execute(select(EventRow).where(EventRow.published == 0).limit(limit))
                .scalars()
                .all()
            )
            return [r.to_dict() for r in rows]

    def publish(self, event_id: str, deliver: Callable[[dict[str, Any]], bool]) -> bool:
        """Deliver an event and mark published. Returns True on acknowledgement.

        If ``deliver`` fails, the row stays unpublished (at-least-once retry).
        Delivery may be repeated; consumers must be idempotent (ConsumerInbox).
        """
        event = self.store.by_id(event_id)
        if event is None:
            return False
        if deliver(event):
            self.store.mark_published(event_id)
            return True
        return False


class ConsumerInbox:
    """Consumer-side idempotency: one local side effect per (consumer, event)."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    def process(
        self,
        *,
        consumer_id: str,
        event: dict[str, Any],
        side_effect: Callable[[dict[str, Any]], None],
    ) -> InboxResult:
        event_id = str(event["event_id"])
        with self._sessions() as s:
            seen = s.scalar(
                select(InboxRow).where(
                    InboxRow.consumer_id == consumer_id, InboxRow.event_id == event_id
                )
            )
            if seen is not None:
                return InboxResult(event_id, consumer_id, "SKIPPED_DUPLICATE", seen.status)
            try:
                side_effect(event)  # case B external side effects must be idempotent downstream
                s.add(InboxRow(consumer_id=consumer_id, event_id=event_id, status="SUCCESS"))
                s.commit()
                return InboxResult(event_id, consumer_id, "PROCESSED")
            except Exception as exc:  # noqa: BLE001 - record failure, do not mark success
                s.rollback()
                s.add(
                    InboxRow(
                        consumer_id=consumer_id,
                        event_id=event_id,
                        status="FAILED",
                        detail=f"{type(exc).__name__}: {exc}"[:500],
                    )
                )
                s.commit()
                return InboxResult(
                    event_id, consumer_id, "FAILED", f"{type(exc).__name__}: {exc}"[:500]
                )

    def processed_events(self, consumer_id: str) -> list[dict[str, Any]]:
        with self._sessions() as s:
            rows = (
                s.execute(select(InboxRow).where(InboxRow.consumer_id == consumer_id))
                .scalars()
                .all()
            )
            return [
                {
                    "event_id": r.event_id,
                    "status": r.status,
                    "detail": r.detail,
                    "processed_at": r.processed_at,
                }
                for r in rows
            ]


def detect_sequence_gaps(events: list[dict[str, Any]]) -> list[GapReport]:
    """Detect gaps PER producer instance; never conclude 'lost'.

    Legal skips (rollback/sequence advance) and in-flight events are
    indistinguishable here, so every gap is PENDING_CONFIRMATION. Comparing
    across producer instances is explicitly avoided.
    """
    by_instance: dict[str, list[int]] = {}
    for e in events:
        by_instance.setdefault(str(e["producer_instance_id"]), []).append(int(e["sequence"]))
    reports: list[GapReport] = []
    for instance, seqs in by_instance.items():
        if not seqs:
            continue
        lo, hi = min(seqs), max(seqs)
        present = set(seqs)
        missing = tuple(n for n in range(lo, hi + 1) if n not in present)
        if missing:
            reports.append(GapReport(instance, missing))
    return reports


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Producer-side dedup by idempotency_key (first wins)."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for e in events:
        key = str(e.get("idempotency_key", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(e)
    return result


def create_event_tables(engine: Engine) -> None:
    EventBase.metadata.create_all(engine, checkfirst=True)


def make_in_memory_events() -> tuple[EventStore, Callable[[], Session]]:
    """Convenience for tests/tools: unique in-memory event DB."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    EventBase.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return EventStore(factory), factory
