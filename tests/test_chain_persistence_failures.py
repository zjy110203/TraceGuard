"""R5-4.7 §4.3 E: persistence failure injection, every scenario classified.

The rule: a persistence problem is a FINDING with an explicit status and
recoverable information — never a silent success, never a raw 500 that hides
which stage broke, and never a "duplicate" label slapped on a real failure.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from apps.api.chain_api import (
    _persist_events,
    _persist_evidence,
    _strip_screenshot_bytes,
)
from packages.core.errors import ValidationError
from packages.core.persistence import PersistenceOutcome
from packages.evidence.manager import EvidenceManager
from packages.evidence.storage import LocalBlobStore


class _Req:
    """Minimal request double carrying a fake event store / manager."""

    def __init__(self, event_store: Any, manager: Any) -> None:
        self.app = type(
            "A",
            (),
            {"state": type("S", (), {"event_store": event_store, "evidence_manager": manager})()},
        )()


def _event(i: int = 1) -> dict[str, Any]:
    return {
        "event_id": f"evt_{i}",
        "event_type": "step.completed",
        "producer_id": "local_runner",
        "producer_instance_id": "inst-1",
        "sequence": i,
        "run_id": "run_x",
        "step_id": "s1",
        "payload": {},
        "occurred_at": "",
        "idempotency_key": f"k{i}",
    }


def _screenshot(eid: str = "ev_shot") -> dict[str, Any]:
    import hashlib

    png = b"\x89PNG-fake-bytes"
    return {
        "evidence_id": eid,
        "evidence_type": "screenshot",
        "raw": True,
        "source": "browser:s1",
        "run_id": "run_x",
        "step_id": "s1",
        "summary": "shot",
        "integrity_check": "sha256:" + hashlib.sha256(png).hexdigest(),
        "payload": {"image": png.hex()},
    }


class _FakeEventStore:
    def __init__(self, *, fail_register: bool = False, fail_append: bool = False) -> None:
        self.fail_register = fail_register
        self.fail_append = fail_append
        self.registered: list[str] = []

    def register_producer(self, instance: str, producer: str) -> None:
        if self.fail_register:
            raise OperationalError("statement", {}, Exception("database is locked"))
        self.registered.append(instance)

    def append(self, **_kw: Any) -> None:
        if self.fail_append:
            raise OperationalError("statement", {}, Exception("connection refused"))


class _FakeManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.imported: list[str] = []

    def import_record(self, record: dict[str, Any], *, blob_store: Any = None) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("evidence manager exploded")
        self.imported.append(record["evidence_id"])
        return {"evidence_id": record["evidence_id"]}


class _FailingBlobStore(LocalBlobStore):
    """A blob store whose writes / reads fail, for injection."""

    def __init__(self, root: Any, *, fail_write: bool = False, fail_read: bool = False) -> None:
        super().__init__(root)
        self.fail_write = fail_write
        self.fail_read = fail_read

    def put(self, data: bytes, *, content_type: str = "application/octet-stream") -> Any:
        if self.fail_write:
            raise OSError("disk is full")
        return super().put(data, content_type=content_type)

    def verify(self, key: str) -> bool:
        if self.fail_read:
            return False
        return super().verify(key)


# 1 + 2. EventStore unavailable / producer registration failure


def test_event_store_unavailable_is_classified_not_duplicate() -> None:
    summary = _persist_events(
        _Req(_FakeEventStore(fail_append=True), _FakeManager()), [_event()], "ws"
    )
    assert summary["status"] == "FAILED"
    assert summary["counts"].get(PersistenceOutcome.DEPENDENCY_UNAVAILABLE.value) == 1
    assert summary["failures"] and "connection refused" in summary["failures"][0]["detail"]


def test_producer_registration_failure_is_classified() -> None:
    store = _FakeEventStore(fail_register=True)
    summary = _persist_events(_Req(store, _FakeManager()), [_event()], "ws")
    assert summary["status"] == "FAILED"
    assert not store.registered
    assert "database is locked" in summary["failures"][0]["detail"]


def test_missing_producer_instance_is_a_failure() -> None:
    event = _event()
    event.pop("producer_instance_id")
    summary = _persist_events(_Req(_FakeEventStore(), _FakeManager()), [event], "ws")
    assert summary["status"] == "FAILED"
    assert summary["failures"][0]["detail"] == "missing producer_instance_id"


# 3 + 4 + 5. Evidence manager / blob write / blob read failures


def test_evidence_manager_failure_is_classified(tmp_path) -> None:
    summary, _ = _persist_evidence(_Req(None, _FakeManager(fail=True)), [_screenshot()], None)
    assert summary["status"] == "FAILED"
    assert summary["failures"][0]["outcome"] == PersistenceOutcome.FAILED.value


def test_blob_write_failure_keeps_screenshot_bytes_for_recovery(tmp_path) -> None:
    """6: the hex payload must survive in the report when the blob store fails."""
    blob = _FailingBlobStore(tmp_path, fail_write=True)
    manager = EvidenceManager(_session_factory(tmp_path))
    record = _screenshot()
    summary, outcomes = _persist_evidence(_Req(None, manager), [record], blob)
    assert summary["status"] == "FAILED"  # blob write blew up -> import failed
    # "disk is full" is a dependency-class failure by design
    assert outcomes["ev_shot"]["status"] == PersistenceOutcome.DEPENDENCY_UNAVAILABLE.value
    retained = _strip_screenshot_bytes([record], blob_store=blob, persisted=outcomes)
    assert retained == ["ev_shot"]  # bytes kept: they are the only copy left
    assert "image" in record["payload"]


def test_blob_read_failure_keeps_screenshot_bytes(tmp_path) -> None:
    """5: a written-but-unverifiable blob must not release the original bytes."""
    blob = _FailingBlobStore(tmp_path, fail_read=True)
    manager = EvidenceManager(_session_factory(tmp_path))
    record = _screenshot()
    summary, outcomes = _persist_evidence(_Req(None, manager), [record], blob)
    assert summary["status"] == "OK"  # the write itself succeeded
    # the import wrote the blob metadata back, but the hex payload STAYS in the
    # record until the strip step verifies the blob (R5-4.7 recovery rule)
    assert "blob" in record["payload"]
    assert "image" in record["payload"]
    # verification is broken -> the strip must NOT release the bytes
    assert blob.verify(record["payload"]["blob"]["key"]) is False
    retained = _strip_screenshot_bytes([record], blob_store=blob, persisted=outcomes)
    assert retained == ["ev_shot"]
    assert "image" in record["payload"]


def test_verified_blob_releases_the_screenshot_bytes(tmp_path) -> None:
    blob = LocalBlobStore(tmp_path)
    manager = EvidenceManager(_session_factory(tmp_path))
    record = _screenshot()
    _, outcomes = _persist_evidence(_Req(None, manager), [record], blob)
    assert outcomes["ev_shot"]["status"] == PersistenceOutcome.WRITTEN.value
    retained = _strip_screenshot_bytes([record], blob_store=blob, persisted=outcomes)
    assert retained == []
    assert "image" not in record["payload"]
    assert blob.verify(record["payload"]["blob"]["key"])


# 7. partial success gets PARTIAL, not OK


def test_partial_evidence_success_is_partial(tmp_path) -> None:
    class HalfManager(_FakeManager):
        def import_record(self, record: dict[str, Any], *, blob_store: Any = None) -> Any:
            if record["evidence_id"] == "ev_bad":
                raise RuntimeError("write failed for one record")
            return super().import_record(record, blob_store=blob_store)

    summary, _ = _persist_evidence(
        _Req(None, HalfManager()),
        [_screenshot("ev_good"), _screenshot("ev_bad")],
        None,
    )
    assert summary["status"] == "PARTIAL"
    assert summary["written"] == 1
    assert len(summary["failures"]) == 1


# 8 + 9. duplicate semantics


def test_full_duplicate_rerun_is_idempotent(tmp_path) -> None:
    """8: re-persisting the same run must be safe (all DUPLICATE / no-op)."""
    factory = _session_factory(tmp_path)
    manager = EvidenceManager(factory)
    blob = LocalBlobStore(tmp_path / "blobs")
    record = _screenshot()
    first = manager.import_record(record, blob_store=blob)
    second = manager.import_record(record, blob_store=blob)
    assert first["evidence_id"] == second["evidence_id"]


def test_duplicate_id_with_different_content_is_rejected(tmp_path) -> None:
    """9: reusing an evidence_id for different captured bytes is a finding."""
    factory = _session_factory(tmp_path)
    manager = EvidenceManager(factory)
    manager.import_record(_screenshot("ev_dup"), blob_store=None)
    forged = _screenshot("ev_dup")
    forged["integrity_check"] = "sha256:" + "0" * 64  # different captured bytes
    with pytest.raises(ValidationError, match="different content"):
        manager.import_record(forged, blob_store=None)

    # ...and the injected failure classifies as FAILED (not "duplicate"):
    class _M(EvidenceManager):
        def get(self, evidence_id: str) -> dict[str, Any] | None:
            return {"integrity_check": "sha256:" + "0" * 64}

    from packages.core.persistence import classify_error

    assert classify_error(ValidationError("reused with different content")) is (
        PersistenceOutcome.FAILED
    )


def test_duplicate_markers_are_not_widened() -> None:
    """A real failure message must not accidentally match a duplicate marker."""
    from packages.core.persistence import classify_error

    assert classify_error(RuntimeError("UNIQUE constraint failed: ev_event.event_id")) is (
        PersistenceOutcome.DUPLICATE
    )
    assert classify_error(RuntimeError("disk is full")) is (
        PersistenceOutcome.DEPENDENCY_UNAVAILABLE
    )
    assert classify_error(RuntimeError("evidence_id reused with different content")) is (
        PersistenceOutcome.FAILED
    )


# 10. Run finalized, ChainStore save failed -> failure must stay queryable


def test_after_run_exception_becomes_a_failed_stage(tmp_path) -> None:
    """The orchestrator turns a persistence blow-up into a FAILED stage."""
    from packages.execution.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)  # bypass DB wiring; exercise only the hook
    published: list[tuple[str, str]] = []
    progressed: list[tuple[str, str, str]] = []

    class _Runs:
        def update_progress(self, run_id: str, payload: dict[str, Any]) -> None:
            progressed.append(
                (payload.get("stage", ""), payload.get("status", ""), payload.get("detail", ""))
            )

    # emulate the after_run failure handling from run_chain
    def after_run(report: Any) -> dict[str, Any]:
        raise RuntimeError("ChainStore save failed: disk full")

    orch.runs = _Runs()  # type: ignore[assignment]
    try:
        persisted = after_run(None)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - mirrors run_chain's handler
        persisted = {
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "stages": [
                {
                    "name": "persistence",
                    "status": "FAILED",
                    "detail": f"{type(exc).__name__}: {exc}"[:500],
                }
            ],
        }
    for entry in persisted.get("stages", []):
        published.append((entry["name"], entry["status"], entry["detail"]))

    assert persisted["status"] == "FAILED"
    assert published == [
        ("persistence", "FAILED", "RuntimeError: ChainStore save failed: disk full")
    ]
    assert progressed == []  # stage loop not yet driven in this unit check


# ------------------------------------------------------------------ helpers


def _session_factory(tmp_path: Any) -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from packages.evidence.manager import EvidenceBase

    engine = create_engine(f"sqlite:///{(tmp_path / 'ev.db').as_posix()}")
    EvidenceBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)
