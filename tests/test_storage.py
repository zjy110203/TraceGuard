"""Blob storage: content addressing, atomicity, bounds, and evidence linkage."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from packages.evidence.manager import EvidenceBase, EvidenceManager
from packages.evidence.storage import LocalBlobStore, S3BlobStore, StorageError


@pytest.fixture()
def store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def test_put_then_get_roundtrip(store: LocalBlobStore) -> None:
    payload = b"trace zip bytes \x00\x01"
    ref = store.put(payload, content_type="application/zip")
    assert ref.size == len(payload)
    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert store.get(ref.key) == payload
    assert store.exists(ref.key) is True
    assert store.verify(ref.key) is True


def test_identical_bytes_deduplicate_to_one_blob(store: LocalBlobStore) -> None:
    first = store.put(b"same content")
    second = store.put(b"same content")
    assert first.key == second.key
    files = [p for p in store.root.rglob("*") if p.is_file()]
    assert len(files) == 1


def test_size_cap_is_enforced_before_writing(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs", max_bytes=8)
    with pytest.raises(StorageError, match="exceeds"):
        store.put(b"way too many bytes")
    assert [p for p in store.root.rglob("*") if p.is_file()] == []


def test_keys_are_validated_so_no_path_can_escape(store: LocalBlobStore) -> None:
    for bad in ["../../etc/passwd", "not-a-hash", "A" * 64, ""]:
        with pytest.raises(StorageError):
            store.get(bad)


def test_missing_key_returns_none(store: LocalBlobStore) -> None:
    assert store.get("0" * 64) is None
    assert store.stat("0" * 64) is None


def test_verify_detects_corruption_on_disk(store: LocalBlobStore, tmp_path: Path) -> None:
    ref = store.put(b"original")
    path = store.root / ref.key[:2] / ref.key[2:4] / ref.key
    path.write_bytes(b"tampered")  # simulate bit rot / manual edit
    assert store.verify(ref.key) is False


def test_writes_leave_no_temporary_files(store: LocalBlobStore) -> None:
    for i in range(5):
        store.put(f"payload-{i}".encode())
    leftovers = [p for p in store.root.rglob("*.tmp-*")]
    assert leftovers == []


def test_s3_adapter_refuses_to_pretend() -> None:
    with pytest.raises(StorageError):
        S3BlobStore("some-bucket")


def _manager(tmp_path: Path) -> tuple[EvidenceManager, Any]:
    engine = create_engine(f"sqlite:///{(tmp_path / 'ev.db').as_posix()}")
    EvidenceBase.metadata.create_all(engine)
    return EvidenceManager(sessionmaker(bind=engine, expire_on_commit=False)), engine


def test_evidence_row_links_to_the_stored_blob(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    blobs = LocalBlobStore(tmp_path / "blobs")
    screenshot = b"\x89PNG\r\n\x1a\nfake-image-bytes"

    record = manager.add_raw(
        evidence_id="evd_blob_1",
        evidence_type="screenshot",
        source="browser",
        data=screenshot,
        run_id="run_1",
        step_id="s1",
        summary="login page",
        blob_store=blobs,
        content_type="image/png",
    )

    assert record["attachment_ref"] == hashlib.sha256(screenshot).hexdigest()
    assert record["payload"]["blob"]["content_type"] == "image/png"
    assert record["integrity_check"] == f"sha256:{record['attachment_ref']}"
    # the bytes are retrievable from the store the reference points at
    assert blobs.get(record["attachment_ref"]) == screenshot
    # and the row's own hash matches the blob hash, so the two cannot diverge
    assert manager.verify("evd_blob_1", screenshot).valid is True


def test_evidence_without_blob_store_keeps_empty_attachment_ref(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path)
    record = manager.add_raw(
        evidence_id="evd_no_blob",
        evidence_type="console",
        source="browser",
        data=b"console output",
        run_id="run_1",
    )
    assert record["attachment_ref"] == ""
    assert "blob" not in record["payload"]
