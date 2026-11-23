"""Stage-4 Evidence Manager (C06, draft schema).

Invariants (v1.1 §3.2 and §阶段4):
  * Raw / Normalized / Derived evidence are strictly separated. A derived
    record MUST declare ``derived_from``; it can never masquerade as raw.
  * Integrity (SHA-256) and source_trust are SEPARATE fields. A hash proves
    byte equivalence, not trustworthiness of the source.
  * Redaction is explicit and never mutates the original hash of raw bytes.
  * Lifecycle: every record has a retention class; expired records are listed,
    not silently deleted.
  * Derived records record the rule/model version that produced them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.core.errors import ConflictError, ValidationError
from packages.evidence.storage import BlobStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EvidenceBase(DeclarativeBase):
    pass


class EvidenceRow(EvidenceBase):
    __tablename__ = "evd_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    layer: Mapped[str] = mapped_column(String, nullable=False)  # raw|normalized|derived
    evidence_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, default="", index=True, nullable=False)
    step_id: Mapped[str] = mapped_column(String, default="", nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)
    summary: Mapped[str] = mapped_column(String, default="", nullable=False)
    integrity_check: Mapped[str] = mapped_column(String, default="", nullable=False)
    # Content-addressed blob key (see packages/evidence/storage.py). Added in the
    # second round: the C06 dataclass always had this field but the row dropped it,
    # so attachment references were silently lost on persist.
    attachment_ref: Mapped[str] = mapped_column(String, default="", nullable=False, index=True)
    source_trust: Mapped[str] = mapped_column(String, default="observed", nullable=False)
    derived_from: Mapped[str] = mapped_column(String, default="", nullable=False)  # JSON list
    derivation_rule: Mapped[str] = mapped_column(String, default="", nullable=False)
    redacted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retention: Mapped[str] = mapped_column(String, default="run_scoped", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "layer": self.layer,
            "evidence_type": self.evidence_type,
            "source": self.source,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "recorded_at": self.recorded_at,
            "summary": self.summary,
            "integrity_check": self.integrity_check,
            "attachment_ref": self.attachment_ref,
            "source_trust": self.source_trust,
            "derived_from": json.loads(self.derived_from) if self.derived_from else [],
            "derivation_rule": self.derivation_rule,
            "redacted": bool(self.redacted),
            "retention": self.retention,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class IntegrityResult:
    evidence_id: str
    valid: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Retention classes (draft). Not a deletion policy — expiry is reported only.
RETENTION_RUN = "run_scoped"
RETENTION_EXPERIMENT = "experiment_scoped"
RETENTION_LONG = "long_term"


class EvidenceManager:
    """Stores and queries Evidence with strict layer separation."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    @staticmethod
    def sha256(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def add_raw(
        self,
        *,
        evidence_id: str,
        evidence_type: str,
        source: str,
        data: bytes,
        run_id: str = "",
        step_id: str = "",
        summary: str = "",
        source_trust: str = "observed",
        retention: str = RETENTION_RUN,
        payload: dict[str, Any] | None = None,
        blob_store: BlobStore | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Store raw evidence with a SHA-256 over the exact captured bytes.

        When a ``blob_store`` is supplied the bytes are persisted content-addressed
        and ``attachment_ref`` points at the blob key, so the payload itself stays
        small and the binary is retrievable.
        """
        body = dict(payload or {})
        body["byte_length"] = len(data)
        attachment_ref = ""
        if blob_store is not None:
            ref = blob_store.put(data, content_type=content_type)
            attachment_ref = ref.key
            body["blob"] = {
                "key": ref.key,
                "size": ref.size,
                "content_type": ref.content_type,
                "integrity_check": ref.integrity_check,
            }
        return self._add(
            evidence_id=evidence_id,
            layer="raw",
            evidence_type=evidence_type,
            source=source,
            run_id=run_id,
            step_id=step_id,
            summary=summary,
            integrity_check=self.sha256(data),
            source_trust=source_trust,
            derived_from=(),
            derivation_rule="",
            retention=retention,
            attachment_ref=attachment_ref,
            payload=body,
        )

    def add_derived(
        self,
        *,
        evidence_id: str,
        evidence_type: str,
        source: str,
        derived_from: tuple[str, ...],
        derivation_rule: str,
        summary: str = "",
        run_id: str = "",
        step_id: str = "",
        source_trust: str = "derived",
        payload: dict[str, Any] | None = None,
        retention: str = RETENTION_RUN,
    ) -> dict[str, Any]:
        """Derived evidence MUST name its sources and rule/model version."""
        if not derived_from:
            raise ConflictError("derived evidence requires non-empty derived_from")
        if not derivation_rule:
            raise ConflictError("derived evidence requires a derivation_rule/version")
        return self._add(
            evidence_id=evidence_id,
            layer="derived",
            evidence_type=evidence_type,
            source=source,
            run_id=run_id,
            step_id=step_id,
            summary=summary,
            integrity_check="",
            source_trust=source_trust,
            derived_from=derived_from,
            derivation_rule=derivation_rule,
            retention=retention,
            payload=dict(payload or {}),
        )

    def add_normalized(
        self,
        *,
        evidence_id: str,
        evidence_type: str,
        source: str,
        derived_from: tuple[str, ...],
        derivation_rule: str,
        summary: str = "",
        run_id: str = "",
        step_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Normalized view of raw evidence; also declares derivation."""
        return self.add_derived(
            evidence_id=evidence_id,
            evidence_type=evidence_type,
            source=source,
            derived_from=derived_from,
            derivation_rule=derivation_rule,
            summary=summary,
            run_id=run_id,
            step_id=step_id,
            source_trust="normalized",
            payload=payload,
        )

    def import_record(
        self, record: dict[str, Any], *, blob_store: BlobStore | None = None
    ) -> dict[str, Any]:
        """Persist an execution EvidenceRecord without re-deriving anything.

        The runner hashed the bytes it actually captured and labelled the record
        raw/derived and its trust level. Recomputing a hash here (as ``add_raw``
        does) would attach a digest to a JSON view rather than to the artifact,
        so the original integrity value is carried over verbatim.

        Binary captures arrive hex-encoded inside ``payload['image']``. When a
        ``blob_store`` is supplied the bytes are moved there content-addressed
        and only the reference stays in the JSON payload, so a few screenshots
        cannot bloat the evidence row.

        Idempotent: an already-persisted ``evidence_id`` is returned unchanged,
        which makes re-running one case safe.
        """
        evidence_id = str(record.get("evidence_id", ""))
        if not evidence_id:
            raise ConflictError("import_record requires an evidence_id")
        existing = self.get(evidence_id)
        if existing is not None:
            # R5-4.7: idempotency is content-aware. Re-importing the SAME
            # capture is a safe no-op; reusing an evidence_id for DIFFERENT
            # captured bytes (different integrity hash) is a conflict, not a
            # duplicate — silently keeping the old row would hide a swap.
            incoming_check = str(record.get("integrity_check") or "")
            existing_check = str(existing.get("integrity_check") or "")
            if incoming_check and existing_check and incoming_check != existing_check:
                raise ValidationError(
                    f"evidence_id {evidence_id!r} was reused with different content: "
                    f"stored {existing_check} != incoming {incoming_check}"
                )
            return existing
        derived_from = tuple(record.get("derived_from") or ())
        is_raw = bool(record.get("raw", True))
        if not is_raw and not derived_from:
            # Mirrors add_derived's invariant: derived must name its sources.
            raise ConflictError("derived evidence requires non-empty derived_from")
        payload = dict(record.get("payload") or {})
        attachment_ref = str(record.get("attachment_ref", ""))
        image_hex = payload.pop("image", None)
        if isinstance(image_hex, str) and image_hex:
            try:
                raw_bytes = bytes.fromhex(image_hex)
            except ValueError:
                payload["image_hex_invalid"] = True
            else:
                digest = self.sha256(raw_bytes)
                if blob_store is not None:
                    ref = blob_store.put(raw_bytes, content_type="image/png")
                    attachment_ref = ref.key
                    payload["blob"] = {"key": ref.key, "size": ref.size, "sha256": digest}
                payload["byte_length"] = len(raw_bytes)
                payload["binary_sha256"] = digest
                # R5-4.7: write the persisted state BACK into the caller's
                # record so the chain report (and the conditional byte-stripper)
                # can see whether the bytes are confirmed in the blob store.
                # The hex payload itself is NOT removed here — the report keeps
                # it until the strip step has VERIFIED the blob; otherwise a
                # failed or unverifiable write would silently destroy the only
                # recovery copy.
                in_record = record.get("payload")
                if isinstance(in_record, dict):
                    in_record.update(
                        {
                            k: payload[k]
                            for k in ("blob", "byte_length", "binary_sha256")
                            if k in payload
                        }
                    )
                    if "image_hex_invalid" in payload:
                        in_record["image_hex_invalid"] = True
        return self._add(
            evidence_id=evidence_id,
            layer="raw" if is_raw else "derived",
            evidence_type=str(record.get("evidence_type", "unknown")),
            source=str(record.get("source", "")),
            run_id=str(record.get("run_id", "")),
            step_id=str(record.get("step_id") or ""),
            summary=str(record.get("summary", "")),
            integrity_check=str(record.get("integrity_check", "")),
            source_trust=str(record.get("source_trust", "observed")),
            derived_from=derived_from,
            derivation_rule=str(record.get("derivation_rule", "")),
            retention=str(record.get("retention", RETENTION_RUN)),
            attachment_ref=attachment_ref,
            payload=payload,
        )

    def _add(self, **kwargs: Any) -> dict[str, Any]:
        derived = kwargs.pop("derived_from")
        kwargs["derived_from"] = json.dumps(list(derived))
        with self._sessions() as s:
            if s.scalar(
                select(EvidenceRow).where(EvidenceRow.evidence_id == kwargs["evidence_id"])
            ):
                raise ConflictError(f"evidence_id already exists: {kwargs['evidence_id']}")
            row = EvidenceRow(**kwargs)
            s.add(row)
            s.commit()
            return row.to_dict()

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        # evidence_id is a UNIQUE column, not the integer primary key; s.get()
        # targets the PK and would always miss here.
        with self._sessions() as s:
            row = s.scalar(select(EvidenceRow).where(EvidenceRow.evidence_id == evidence_id))
            return row.to_dict() if row else None

    def query(
        self,
        *,
        run_id: str | None = None,
        layer: str | None = None,
        source_trust: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._sessions() as s:
            stmt = select(EvidenceRow)
            if run_id is not None:
                stmt = stmt.where(EvidenceRow.run_id == run_id)
            if layer is not None:
                stmt = stmt.where(EvidenceRow.layer == layer)
            if source_trust is not None:
                stmt = stmt.where(EvidenceRow.source_trust == source_trust)
            rows = list(
                s.execute(stmt.order_by(EvidenceRow.recorded_at).limit(limit)).scalars().all()
            )
            return [r.to_dict() for r in rows]

    def verify(self, evidence_id: str, data: bytes) -> IntegrityResult:
        """Verify bytes against the recorded SHA-256. Trust is a separate axis."""
        record = self.get(evidence_id)
        if record is None:
            return IntegrityResult(evidence_id, False, "evidence not found")
        expected = record["integrity_check"]
        if not expected.startswith("sha256:"):
            return IntegrityResult(evidence_id, False, "no sha256 integrity recorded")
        actual = self.sha256(data)
        return IntegrityResult(
            evidence_id, actual == expected, f"expected={expected} actual={actual}"
        )

    def redact(self, evidence_id: str, fields: tuple[str, ...]) -> dict[str, Any] | None:
        """Mark redaction and remove named payload keys (raw hash unchanged)."""
        with self._sessions() as s:
            row = s.scalar(select(EvidenceRow).where(EvidenceRow.evidence_id == evidence_id))
            if row is None:
                return None
            payload = dict(row.payload)
            for key in fields:
                payload.pop(key, None)
            row.payload = payload
            row.redacted = 1
            s.commit()
            return row.to_dict()

    def expired(self, as_of: str | None = None) -> list[dict[str, Any]]:
        """Report records whose draft retention window has elapsed. No deletion."""
        now = as_of or _now()
        with self._sessions() as s:
            rows = list(s.execute(select(EvidenceRow)).scalars().all())
            cutoff = {
                RETENTION_RUN: 7 * 24 * 3600,
                RETENTION_EXPERIMENT: 90 * 24 * 3600,
                RETENTION_LONG: 365 * 24 * 3600,
            }
            now_dt = datetime.fromisoformat(now)
            out = []
            for r in rows:
                age = (now_dt - datetime.fromisoformat(r.recorded_at)).total_seconds()
                if age > cutoff.get(r.retention, cutoff[RETENTION_RUN]):
                    out.append(r.to_dict())
            return out

    def derived_sources(self, evidence_id: str) -> list[str]:
        record = self.get(evidence_id)
        return list(record["derived_from"]) if record else []
