"""Stage-4 acceptance: Event reliability + Evidence separation/integrity.

Covers the v1.1 阶段4 acceptance scenarios with real code:
  * idempotent producer delivery; conflict on key reuse with different payload;
  * consumer inbox: duplicate event -> one side effect; two consumers independent;
    crash/FAILED does not mark success;
  * outbox at-least-once (failed delivery stays pending, retry publishes);
  * sequence gaps are PENDING_CONFIRMATION, per producer instance, never 'lost';
  * workspace_id injected from trusted context (client payload cannot override);
  * raw/normalized/derived separation; derived must declare source+rule;
  * SHA-256 integrity check + redaction + lifecycle expiry report (no delete).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from packages.core.errors import ConflictError
from packages.evidence.events import (
    ConsumerInbox,
    Outbox,
    dedupe_events,
    detect_sequence_gaps,
    make_in_memory_events,
)
from packages.evidence.manager import RETENTION_RUN, EvidenceBase, EvidenceManager


@pytest.fixture()
def events() -> Any:
    store, factory = make_in_memory_events()
    return store, factory


@pytest.fixture()
def evidence() -> EvidenceManager:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    EvidenceBase.metadata.create_all(engine)
    return EvidenceManager(sessionmaker(bind=engine, expire_on_commit=False))


def _event(event_id: str, seq: int, producer: str = "pi-A", payload: dict[str, Any] | None = None):
    return {
        "event_id": event_id,
        "event_type": "test.event",
        "schema_version": "1.0.0-draft",
        "context_refs": {"workspace_id": "ws_x", "run_id": "r1"},
        "producer_id": "runner",
        "producer_instance_id": producer,
        "sequence": seq,
        "occurred_at": "2026-11-21T10:00:00Z",
        "observed_at": "2026-11-21T10:00:01Z",
        "idempotency_key": f"{producer}:{seq}",
        "payload": payload or {"v": seq},
    }


def test_producer_idempotency_and_conflict(events: Any) -> None:
    store, _factory = events
    store.register_producer("pi-A", "runner")
    a = store.append(
        event_id="evt_A1",
        event_type="step.completed",
        workspace_id="ws_x",
        producer_id="runner",
        producer_instance_id="pi-A",
        sequence=1,
        payload={"s": 1},
    )
    # duplicate delivery with same key+payload -> no new row
    b = store.append(
        event_id="evt_A1_dup",
        event_type="step.completed",
        workspace_id="ws_x",
        producer_id="runner",
        producer_instance_id="pi-A",
        sequence=1,
        payload={"s": 1},
    )
    assert b["event_id"] == a["event_id"]
    assert len(store.query(workspace_id="ws_x")) == 1
    # same key different payload -> conflict + audit
    with pytest.raises(ConflictError):
        store.append(
            event_id="evt_A1_bad",
            event_type="step.completed",
            workspace_id="ws_x",
            producer_id="runner",
            producer_instance_id="pi-A",
            sequence=1,
            payload={"s": 999},
        )
    assert any(a["action"] == "IDEMPOTENCY_CONFLICT" for a in store.audit_log())


def test_trusted_workspace_injection(events: Any) -> None:
    store, _factory = events
    store.register_producer("pi-W", "runner")
    ev = store.append(
        event_id="evt_W1",
        event_type="test",
        workspace_id="ws_trusted",
        producer_id="runner",
        producer_instance_id="pi-W",
        sequence=1,
        context_refs={"workspace_id": "ws_forged_by_client", "run_id": "r1"},
    )
    assert ev["context_refs"]["workspace_id"] == "ws_trusted"
    assert store.query(workspace_id="ws_forged_by_client") == []
    assert len(store.query(workspace_id="ws_trusted")) == 1


def test_consumer_inbox_idempotency_and_independence(events: Any) -> None:
    store, factory = events
    store.register_producer("pi-C", "runner")
    ev = store.append(
        event_id="evt_C1",
        event_type="x",
        workspace_id="ws_x",
        producer_id="runner",
        producer_instance_id="pi-C",
        sequence=1,
    )
    inbox = ConsumerInbox(factory)
    calls: list[str] = []

    def effect(e: dict[str, Any]) -> None:
        calls.append(e["event_id"])

    r1 = inbox.process(consumer_id="consumer-1", event=ev, side_effect=effect)
    r2 = inbox.process(consumer_id="consumer-1", event=ev, side_effect=effect)
    assert r1.outcome == "PROCESSED" and r2.outcome == "SKIPPED_DUPLICATE"
    assert calls == ["evt_C1"]
    # different consumer processes independently
    r3 = inbox.process(consumer_id="consumer-2", event=ev, side_effect=effect)
    assert r3.outcome == "PROCESSED"
    assert calls == ["evt_C1", "evt_C1"]


def test_consumer_failure_not_marked_success(events: Any) -> None:
    _store, factory = events
    inbox = ConsumerInbox(factory)

    def boom(_e: dict[str, Any]) -> None:
        raise RuntimeError("downstream unavailable")

    r = inbox.process(consumer_id="c-fail", event=_event("evt_F1", 1), side_effect=boom)
    assert r.outcome == "FAILED"
    rows = inbox.processed_events("c-fail")
    assert rows[0]["status"] == "FAILED"


def test_outbox_at_least_once(events: Any) -> None:
    store, _factory = events
    store.register_producer("pi-O", "runner")
    ev = store.append(
        event_id="evt_O1",
        event_type="x",
        workspace_id="ws_x",
        producer_id="runner",
        producer_instance_id="pi-O",
        sequence=1,
    )
    outbox = Outbox(store)
    assert len(outbox.pending()) == 1
    assert outbox.publish(ev["event_id"], lambda _e: False) is False
    assert len(outbox.pending()) == 1  # still pending after failed delivery
    assert outbox.publish(ev["event_id"], lambda _e: True) is True
    assert len(outbox.pending()) == 0


def test_sequence_gaps_pending_confirmation_per_instance() -> None:
    events = [
        _event("e1", 1, "pi-1"),
        _event("e2", 3, "pi-1"),  # gap at 2
        _event("e3", 5, "pi-2"),  # different instance, gap at 4 but separate
        _event("e4", 6, "pi-2"),
    ]
    reports = detect_sequence_gaps(events)
    by_inst = {r.producer_instance_id: r for r in reports}
    assert by_inst["pi-1"].missing_sequences == (2,)
    # pi-2 has 5,6 (contiguous) -> no gap; instances are never compared
    assert "pi-2" not in by_inst
    assert all(r.status == "PENDING_CONFIRMATION" for r in reports)
    assert "no conclusion is fabricated" in by_inst["pi-1"].note


def test_producer_restart_new_sequence_space(events: Any) -> None:
    store, _factory = events
    store.register_producer("pi-old", "worker")
    store.register_producer("pi-new", "worker")  # restart -> new instance id
    assert store.next_sequence("pi-old") == 1
    assert store.next_sequence("pi-new") == 1
    gaps = detect_sequence_gaps(store.query(workspace_id="ws_x"))
    assert gaps == []  # no cross-instance comparison


def test_dedupe_events() -> None:
    result = dedupe_events([_event("a", 1), _event("a_dup", 1), _event("b", 2)])
    assert len(result) == 2


def test_evidence_layers_and_integrity(evidence: EvidenceManager) -> None:
    raw_bytes = b"screenshot-bytes-123"
    raw = evidence.add_raw(
        evidence_id="evd_raw",
        evidence_type="screenshot",
        source="browser",
        data=raw_bytes,
        run_id="r1",
        source_trust="observed",
    )
    assert raw["layer"] == "raw" and raw["derived_from"] == []
    assert evidence.verify("evd_raw", raw_bytes).valid is True
    assert evidence.verify("evd_raw", b"tampered").valid is False

    norm = evidence.add_normalized(
        evidence_id="evd_norm",
        evidence_type="dom_summary",
        source="normalizer",
        derived_from=("evd_raw",),
        derivation_rule="dom-normalizer-v1",
        run_id="r1",
    )
    assert norm["layer"] == "derived" and norm["source_trust"] == "normalized"
    assert evidence.derived_sources("evd_norm") == ["evd_raw"]

    derived = evidence.add_derived(
        evidence_id="evd_derived",
        evidence_type="hypothesis",
        source="rule-engine",
        derived_from=("evd_norm",),
        derivation_rule="rule-v2.1",
        run_id="r1",
    )
    assert derived["layer"] == "derived"
    # raw layer query must not return derived records
    assert [r["evidence_id"] for r in evidence.query(run_id="r1", layer="raw")] == ["evd_raw"]


def test_derived_requires_provenance(evidence: EvidenceManager) -> None:
    with pytest.raises(ConflictError):
        evidence.add_derived(
            evidence_id="bad",
            evidence_type="x",
            source="x",
            derived_from=(),
            derivation_rule="v1",
        )
    with pytest.raises(ConflictError):
        evidence.add_derived(
            evidence_id="bad2",
            evidence_type="x",
            source="x",
            derived_from=("evd_raw",),
            derivation_rule="",
        )


def test_redaction_and_lifecycle(evidence: EvidenceManager) -> None:
    evidence.add_raw(
        evidence_id="evd_secret",
        evidence_type="api_state",
        source="independent_api",
        data=b"{}",
        run_id="r1",
        payload={"token": "super-secret", "project_count": 1},
        retention=RETENTION_RUN,
    )
    updated = evidence.redact("evd_secret", ("token",))
    assert updated["redacted"] is True
    assert "token" not in updated["payload"]
    assert updated["payload"]["project_count"] == 1

    # fresh record not expired; far-future time reports it without deleting
    assert evidence.expired() == []
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    assert any(r["evidence_id"] == "evd_secret" for r in evidence.expired(as_of=future))
    assert evidence.get("evd_secret") is not None  # still present, no deletion


def test_evidence_api_endpoints(tmp_path: Path) -> None:
    """Viewer/query endpoints are wired and reachable through the platform app.

    The DB lives under pytest's tmp_path so a mid-test failure can never leak a
    stray `_test_evidence_*.db` into the production `data/` directory.
    """
    from fastapi.testclient import TestClient

    from apps.api.main import build_app
    from packages.core.config import Settings

    db_file = tmp_path / "_test_evidence.db"
    settings = Settings(app_env="test", database_url=f"sqlite:///{db_file.as_posix()}")
    app = build_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/evidence/events").status_code == 200
        assert client.get("/api/v1/evidence/events/gaps").status_code == 200
        assert client.get("/api/v1/evidence/events/audit").status_code == 200
        assert client.get("/api/v1/evidence/records").status_code == 200
        assert client.get("/api/v1/evidence/records/nonexistent").status_code == 404
        assert client.get("/api/v1/evidence/graph/nodes").status_code == 200
        assert client.get("/api/v1/evidence/graph/edges").status_code == 200
        assert client.get("/api/v1/evidence/graph/subgraph/nonexistent").status_code == 200
    db_file.unlink(missing_ok=True)


def test_contract_registry_marks_c05_c06_draft() -> None:
    registry_path = Path("packages/contracts/registry.json")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in registry["contracts"]}
    assert by_id["C05"]["status"] == "draft"
    assert by_id["C06"]["status"] == "draft"
    assert by_id["C12"]["status"] == "draft"
    for cid in ("C05", "C06"):
        schema = json.loads((Path("packages/contracts") / by_id[cid]["schema_file"]).read_text())
        assert "DRAFT" in schema["title"] or "DRAFT" in schema["description"]
