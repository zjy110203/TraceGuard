"""C05 Event Envelope and C06 Evidence Record (stage 2, illustrative schema).

The contract map marks formal JSON Schemas as pending freeze, so these are
faithful to the documented semantics but labelled illustrative. Key C05/C06
constraints honoured here:
  * occurred_at and recorded_at are SEPARATE.
  * sequence is per producer instance; idempotency_key makes delivery repeatable.
  * integrity_check and source_trust are SEPARATE fields.
  * derived evidence is flagged so it is never double-counted as raw evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    schema_version: str
    run_id: str
    step_id: str | None
    producer_id: str
    producer_instance_id: str
    sequence: int
    occurred_at: str
    recorded_at: str
    idempotency_key: str
    correlation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        run_id: str,
        step_id: str | None,
        producer_id: str,
        producer_instance_id: str,
        sequence: int,
        payload: dict[str, Any] | None = None,
        correlation_id: str = "",
        occurred_at: str | None = None,
    ) -> EventEnvelope:
        recorded = _now()
        occurred = occurred_at or recorded
        key = f"{run_id}:{producer_instance_id}:{sequence}"
        return cls(
            event_id=f"evt_{uuid4().hex[:12]}",
            event_type=event_type,
            schema_version="1.0.0-illustrative",
            run_id=run_id,
            step_id=step_id,
            producer_id=producer_id,
            producer_instance_id=producer_instance_id,
            sequence=sequence,
            occurred_at=occurred,
            recorded_at=recorded,
            idempotency_key=key,
            correlation_id=correlation_id,
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    evidence_type: str  # screenshot | dom_snapshot | network_response | api_state
    raw: bool  # raw vs derived
    source: str
    run_id: str
    step_id: str | None
    recorded_at: str
    summary: str
    attachment_ref: str = ""
    integrity_check: str = ""  # separate from source_trust
    source_trust: str = "observed"  # observed | independent_api | derived
    derived_from: tuple[str, ...] = ()
    redacted: bool = False
    retention: str = "run_scoped"
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        evidence_type: str,
        source: str,
        run_id: str,
        step_id: str | None,
        summary: str,
        raw: bool = True,
        attachment_ref: str = "",
        integrity_check: str = "",
        source_trust: str = "observed",
        derived_from: tuple[str, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> EvidenceRecord:
        return cls(
            evidence_id=f"evd_{uuid4().hex[:12]}",
            evidence_type=evidence_type,
            raw=raw,
            source=source,
            run_id=run_id,
            step_id=step_id,
            recorded_at=_now(),
            summary=summary,
            attachment_ref=attachment_ref,
            integrity_check=integrity_check,
            source_trust=source_trust,
            derived_from=derived_from,
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventCollector:
    """Collects events with per-instance sequence and idempotency keys."""

    def __init__(self, run_id: str, producer_id: str) -> None:
        self.run_id = run_id
        self.producer_instance_id = f"{producer_id}_{uuid4().hex[:8]}"
        self.producer_id = producer_id
        self._sequence = 0
        self._events: list[EventEnvelope] = []

    def emit(
        self,
        event_type: str,
        *,
        step_id: str | None = None,
        payload: dict[str, Any] | None = None,
        correlation_id: str = "",
    ) -> EventEnvelope:
        self._sequence += 1
        event = EventEnvelope.create(
            event_type=event_type,
            run_id=self.run_id,
            step_id=step_id,
            producer_id=self.producer_id,
            producer_instance_id=self.producer_instance_id,
            sequence=self._sequence,
            payload=payload,
            correlation_id=correlation_id,
        )
        self._events.append(event)
        return event

    def events(self) -> tuple[EventEnvelope, ...]:
        return tuple(self._events)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._events]
