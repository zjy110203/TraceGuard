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
            existing = s.scalar(select(EventRow).where(EventRow.idempotency_key == key))
            if existing is not None:
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
