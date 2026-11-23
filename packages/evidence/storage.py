"""Blob storage abstraction for evidence attachments.

Evidence rows already carry ``attachment_ref``; until now there was no store that
reference could point at. This module supplies one.

Design choices that matter for evidence integrity:
* **Content addressed.** The key is derived from the SHA-256, so identical bytes
  deduplicate and a key can never escape the root (no user-supplied paths, hence
  no traversal surface at all).
* **Atomic writes.** Data goes to a temp file and is renamed into place, so a
  crash never leaves a half-written blob that hashes to something unexpected.
* **Bounded.** A per-blob cap rejects oversized payloads before they hit disk.
* **Honest adapters.** ``LocalBlobStore`` is real and tested. ``S3BlobStore``
  cannot be exercised here (no boto3, no bucket) and refuses to construct rather
  than pretend.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_MAX_BYTES = 32 * 1024 * 1024
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class StorageError(RuntimeError):
    """Raised for unavailable backends or violated storage constraints."""


@dataclass(frozen=True)
class BlobRef:
    key: str
    size: int
    sha256: str
    content_type: str
    created_at: str

    @property
    def integrity_check(self) -> str:
        return f"sha256:{self.sha256}"


class BlobStore(Protocol):
    def put(self, data: bytes, *, content_type: str = "application/octet-stream") -> BlobRef: ...

    def get(self, key: str) -> bytes | None: ...

    def stat(self, key: str) -> BlobRef | None: ...

    def exists(self, key: str) -> bool: ...

    def verify(self, key: str) -> bool:
        """Re-hash the stored bytes; the content-addressed integrity check."""
        ...


class LocalBlobStore:
    """Content-addressed store on the local filesystem, sharded two levels deep."""

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._meta: dict[str, BlobRef] = {}

    # ---------------------------------------------------------------- write

    def put(self, data: bytes, *, content_type: str = "application/octet-stream") -> BlobRef:
        if len(data) > self.max_bytes:
            raise StorageError(f"blob of {len(data)} bytes exceeds the {self.max_bytes} byte cap")
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)  # atomic on the same filesystem
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        ref = BlobRef(
            key=digest,
            size=len(data),
            sha256=digest,
            content_type=content_type,
            created_at=_now(),
        )
        self._meta[digest] = ref
        return ref

    # ----------------------------------------------------------------- read

    def get(self, key: str) -> bytes | None:
        path = self._path(self._validate_key(key))
        if not path.exists():
            return None
        return path.read_bytes()

    def stat(self, key: str) -> BlobRef | None:
        key = self._validate_key(key)
        path = self._path(key)
        if not path.exists():
            return None
        known = self._meta.get(key)
        return known or BlobRef(
            key=key,
            size=path.stat().st_size,
            sha256=key,
            content_type="application/octet-stream",
            created_at=_now(),
        )

    def exists(self, key: str) -> bool:
        return self._path(self._validate_key(key)).exists()

    # --------------------------------------------------------------- verify

    def verify(self, key: str) -> bool:
        """Re-hash stored bytes; catches on-disk corruption, not just presence."""
        data = self.get(key)
        return data is not None and hashlib.sha256(data).hexdigest() == key

    # -------------------------------------------------------------- helpers

    def _validate_key(self, key: str) -> str:
        if not SHA256_HEX.match(key):
            raise StorageError(f"invalid blob key (expected 64 hex chars): {key!r}")
        return key

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / key[2:4] / key


class S3BlobStore:
    """Adapter sketch for boto3. NOT verified in this environment."""

    def __init__(self, bucket: str, *, allow_unverified: bool = False) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise StorageError(
                "boto3 is not installed; S3BlobStore is unavailable. "
                "LocalBlobStore is the supported implementation here."
            ) from exc
        if not allow_unverified:
            raise StorageError(
                "S3BlobStore has never been verified against a live bucket; "
                "pass allow_unverified=True only for experiments."
            )
        self.bucket = bucket

    def put(
        self, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> BlobRef:  # pragma: no cover
        raise StorageError("S3BlobStore.put is not implemented (unverified)")

    def get(self, key: str) -> bytes | None:  # pragma: no cover
        raise StorageError("S3BlobStore.get is not implemented (unverified)")

    def stat(self, key: str) -> BlobRef | None:  # pragma: no cover
        return None

    def exists(self, key: str) -> bool:  # pragma: no cover
        return False

    def verify(self, key: str) -> bool:  # pragma: no cover
        raise StorageError("S3BlobStore.verify is not implemented (unverified)")


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
