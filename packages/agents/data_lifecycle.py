"""Crash-recoverable, per-run test-data leases used by Scenario packages."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class DataLeaseManager:
    """Owns only exact run directories; it never clears a shared data root."""

    def __init__(self, root: Path, *, heartbeat_seconds: int = 20, timeout_seconds: int = 60):
        if not 1 <= heartbeat_seconds <= 20:
            raise ValueError("heartbeat_seconds must be between 1 and 20")
        if not heartbeat_seconds < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be greater than heartbeat and at most 60")
        self.root = Path(root)
        self.heartbeat_seconds = heartbeat_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = threading.RLock()
        self._cleaning: set[str] = set()
        self._heartbeat_threads: dict[str, tuple[threading.Event, threading.Thread]] = {}

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("invalid run_id")

    def _run_dir(self, run_id: str) -> Path:
        self._validate_run_id(run_id)
        return self.root / run_id

    def _write_lease(self, run_id: str, lease: dict[str, Any]) -> None:
        folder = self._run_dir(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".lease-{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps(lease, sort_keys=True), encoding="utf-8")
        temporary.replace(folder / "lease.json")

    def setup(
        self, run_id: str, seed: dict[str, Any] | None = None, *, now: float | None = None
    ) -> str:
        with self._lock:
            folder = self._run_dir(run_id)
            if folder.exists():
                raise ValueError(f"run lease already exists: {run_id}")
            token = uuid.uuid4().hex
            timestamp = time.time() if now is None else now
            lease = {
                "run_id": run_id,
                "token": token,
                "created_at": timestamp,
                "heartbeat_at": timestamp,
                "state": "ACTIVE",
                "cleanup_error": "",
            }
            self._write_lease(run_id, lease)
            if seed:
                (folder / "seed.json").write_text(
                    json.dumps(seed, ensure_ascii=False, sort_keys=True), encoding="utf-8"
                )
            return token

    def heartbeat(self, run_id: str, token: str, *, now: float | None = None) -> None:
        with self._lock:
            path = self._run_dir(run_id) / "lease.json"
            lease = json.loads(path.read_text(encoding="utf-8"))
            if lease.get("token") != token or lease.get("state") != "ACTIVE":
                raise ValueError("lease is not active or token does not match")
            lease["heartbeat_at"] = time.time() if now is None else now
            self._write_lease(run_id, lease)

    def resources(self, run_id: str, token: str) -> list[dict[str, Any]]:
        with self._lock:
            lease = json.loads((self._run_dir(run_id) / "lease.json").read_text("utf-8"))
            if lease.get("token") != token:
                raise ValueError("lease token mismatch")
            return list(lease.get("resources") or [])

    def lease(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return json.loads((self._run_dir(run_id) / "lease.json").read_text("utf-8"))

    def add_resource(self, run_id: str, token: str, resource: dict[str, Any]) -> None:
        with self._lock:
            lease_path = self._run_dir(run_id) / "lease.json"
            lease = json.loads(lease_path.read_text("utf-8"))
            if lease.get("token") != token or lease.get("state") != "ACTIVE":
                raise ValueError("lease is not active or token does not match")
            resources = list(lease.get("resources") or [])
            resources.append(dict(resource))
            lease["resources"] = resources
            self._write_lease(run_id, lease)

    def mark_cleanup_failed(self, run_id: str, token: str, error: str) -> None:
        with self._lock:
            lease_path = self._run_dir(run_id) / "lease.json"
            lease = json.loads(lease_path.read_text("utf-8"))
            if lease.get("token") != token:
                raise ValueError("lease token mismatch")
            lease["state"] = "CLEANUP_FAILED"
            lease["cleanup_error"] = error[:1000]
            self._write_lease(run_id, lease)

    def start_heartbeat(self, run_id: str, token: str) -> None:
        with self._lock:
            if run_id in self._heartbeat_threads:
                raise ValueError("heartbeat already running")
            stop = threading.Event()

            def beat() -> None:
                while not stop.wait(self.heartbeat_seconds):
                    try:
                        self.heartbeat(run_id, token)
                    except (OSError, ValueError, json.JSONDecodeError):
                        return

            thread = threading.Thread(target=beat, name=f"tg-lease-{run_id}", daemon=True)
            self._heartbeat_threads[run_id] = (stop, thread)
            thread.start()

    def stop_heartbeat(self, run_id: str) -> None:
        with self._lock:
            heartbeat = self._heartbeat_threads.pop(run_id, None)
        if heartbeat:
            stop, thread = heartbeat
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 1)

    def cleanup(self, run_id: str, token: str, *, now: float | None = None) -> bool:
        with self._lock:
            folder = self._run_dir(run_id)
            lease_path = folder / "lease.json"
            if not lease_path.exists():
                return True
            if run_id in self._cleaning:
                return False
            self._cleaning.add(run_id)
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                if lease.get("token") != token:
                    raise ValueError("lease token mismatch; refusing cleanup")
                # The directory is owned exclusively by this run. No glob or prefix
                # matching against the parent is used.
                for child in folder.iterdir():
                    if child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        import shutil

                        shutil.rmtree(child)
                folder.rmdir()
                return True
            except OSError as exc:
                try:
                    lease = json.loads(lease_path.read_text(encoding="utf-8"))
                    lease["state"] = "CLEANUP_FAILED"
                    lease["cleanup_error"] = str(exc)
                    self._write_lease(run_id, lease)
                except (OSError, json.JSONDecodeError):
                    pass
                return False
            finally:
                self._cleaning.discard(run_id)

    def expired_runs(self, *, now: float | None = None) -> tuple[str, ...]:
        timestamp = time.time() if now is None else now
        if not self.root.is_dir():
            return ()
        expired: list[str] = []
        for folder in self.root.iterdir():
            lease_path = folder / "lease.json"
            try:
                lease = json.loads(lease_path.read_text(encoding="utf-8"))
                if (
                    lease.get("state") == "ACTIVE"
                    and timestamp - float(lease["heartbeat_at"]) > self.timeout_seconds
                ):
                    expired.append(str(lease["run_id"]))
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return tuple(sorted(expired))

    def recover_orphans(self, *, now: float | None = None) -> tuple[str, ...]:
        timestamp = time.time() if now is None else now
        cleaned: list[str] = []
        for run_id in self.expired_runs(now=timestamp):
            try:
                lease = json.loads((self._run_dir(run_id) / "lease.json").read_text("utf-8"))
                if self.cleanup(run_id, str(lease["token"]), now=timestamp):
                    cleaned.append(run_id)
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return tuple(cleaned)
