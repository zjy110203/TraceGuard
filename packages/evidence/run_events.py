"""Durable run-event recorder (R5-5.1).

Adapts the **existing** :class:`packages.evidence.events.EventStore` to the run
event stream the execution bus emits. Two deliberate constraints:

* no second store — every write goes through ``EventStore.append_stream``, so
  idempotency, trusted ``workspace_id`` and per-producer sequence semantics are
  the ones already implemented and tested;
* no import of ``packages.execution`` — the recorder speaks plain dicts, so the
  execution layer can depend on it without an import cycle.

Cursor format
-------------
Wire cursor (what SSE puts in ``id:``)::

    v2:<stream_seq>:<producer_instance_id>

In persisted mode the per-instance ``sequence`` *is* the stream position
(``EventStore.append_stream`` assigns it from the same counter), so encoding
both would be redundant; the instance id is kept for provenance.

Legacy cursors (``runevt_xxxxxxxx``, i.e. a raw event_id) are still accepted:
they resolve through the cursor table. A legacy cursor whose row was pruned can
only be reported as unknown — with the retention watermark attached, so the
operator can tell "the server forgot" from "you sent garbage".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.evidence.events import EventStore

CURSOR_PREFIX = "v2:"
PRODUCER_ID = "traceguard.eventbus"


class EventPersistenceError(RuntimeError):
    """Raised in strict mode when an event could not be made durable."""


@dataclass(frozen=True)
class CursorResolution:
    """Where a client's ``Last-Event-ID`` points, and whether it is trustworthy.

    ``kind`` is one of ``first`` (no cursor), ``ok``, ``unknown``, ``expired``.
    ``after`` is the position to replay *after*:
    ``ok`` -> the cursor itself; ``expired`` -> 0 (replay everything retained);
    ``unknown`` -> ``latest`` (replay nothing, so nothing is delivered twice).
    """

    kind: str
    after: int
    latest: int
    oldest: int | None
    pruned_through: int
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind in ("first", "ok")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "after": self.after,
            "latest": self.latest,
            "oldest": self.oldest,
            "pruned_through": self.pruned_through,
            "detail": self.detail,
        }


class RunEventRecorder:
    """Durable, ordered, idempotent history for run events."""

    def __init__(
        self,
        store: EventStore,
        *,
        producer_instance_id: str,
        workspace_id: str = "ws-default",
        retention_events: int = 20000,
        prune_every: int = 500,
    ) -> None:
        self.store = store
        self.producer_instance_id = producer_instance_id
        self.workspace_id = workspace_id
        self.retention_events = retention_events
        self.prune_every = prune_every
        self._appends_since_prune = 0

    # ------------------------------------------------------------------ setup
    def start(self) -> dict[str, Any]:
        """Register this producer instance, migrate legacy rows, apply retention."""
        self.store.register_producer(self.producer_instance_id, PRODUCER_ID)
        backfilled = self.store.backfill_stream_cursors()
        pruned = self.store.prune_stream(self.retention_events)
        return {
            "producer_instance_id": self.producer_instance_id,
            "backfilled": backfilled,
            "pruned": pruned["pruned"],
            **self.store.stream_stats(),
        }

    # ------------------------------------------------------------------ write
    def append(
        self,
        *,
        event_id: str,
        event_type: str,
        run_id: str,
        task_id: str,
        payload: dict[str, Any],
        occurred_at: str,
    ) -> int:
        """Append one event and return its durable position.

        ``idempotency_key`` is the event_id: appending the same event twice can
        never create a second row or a second position.
        """
        _, position = self.store.append_stream(
            event_id=event_id,
            event_type=event_type,
            workspace_id=self.workspace_id,
            producer_id=PRODUCER_ID,
            producer_instance_id=self.producer_instance_id,
            context_refs={"run_id": run_id, "task_id": task_id},
            payload=payload,
            occurred_at=occurred_at,
            idempotency_key=f"run:{event_id}",
        )
        self._appends_since_prune += 1
        if self.prune_every and self._appends_since_prune >= self.prune_every:
            self._appends_since_prune = 0
            self.store.prune_stream(self.retention_events)
        return position

    # ------------------------------------------------------------------- read
    def latest(self) -> int:
        return self.store.latest_stream_seq()

    def replay(self, after: int, limit: int = 500) -> list[dict[str, Any]]:
        return self.replay_page(after, limit)[0]

    def replay_page(self, after: int, limit: int = 500) -> tuple[list[dict[str, Any]], int]:
        """One replay page and the position it reached.

        The position is the durable ``stream_seq``, **not** the event's own
        ``sequence``: for rows written by other producers the two differ, and
        advancing a cursor by the wrong one loops forever (R5-5.1 defect #2).
        """
        page = self.store.replay_stream(after, limit, producer_id=PRODUCER_ID)
        if not page:
            return [], after
        return [row for _, row in page], max(position for position, _ in page)

    def stats(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "producer_instance_id": self.producer_instance_id,
            "retention_events": self.retention_events,
            **self.store.stream_stats(),
        }

    # ----------------------------------------------------------------- cursor
    def cursor_token(self, stream_seq: int) -> str:
        return f"{CURSOR_PREFIX}{stream_seq}:{self.producer_instance_id}"

    def resolve(self, token: str) -> CursorResolution:
        latest = self.store.latest_stream_seq()
        oldest = self.store.oldest_stream_seq()
        watermark = self.store.pruned_through()

        def resolution(kind: str, after: int, detail: str = "") -> CursorResolution:
            return CursorResolution(
                kind=kind,
                after=after,
                latest=latest,
                oldest=oldest,
                pruned_through=watermark,
                detail=detail,
            )

        if token.startswith(CURSOR_PREFIX):
            parts = token[len(CURSOR_PREFIX) :].split(":")
            try:
                stream_seq = int(parts[0])
            except (IndexError, ValueError):
                return resolution("unknown", latest, "malformed v2 cursor")
            if stream_seq < watermark:
                return resolution(
                    "expired",
                    0,
                    f"cursor position {stream_seq} is older than the retention "
                    f"watermark {watermark}; replaying retained history",
                )
            if stream_seq > latest:
                return resolution(
                    "unknown",
                    latest,
                    f"cursor position {stream_seq} is beyond this store's latest "
                    f"{latest}; it was issued by a different database or a reset",
                )
            return resolution("ok", stream_seq)

        # Legacy cursor: a raw event_id.
        position = self.store.stream_position(token)
        if position is None:
            return resolution(
                "unknown",
                latest,
                "event id not found in the cursor table; it may have been pruned "
                f"(retention watermark {watermark})",
            )
        if position < watermark:
            return resolution(
                "expired",
                0,
                f"event {token} is older than the retention watermark {watermark}; "
                "replaying retained history",
            )
        return resolution("ok", position)
