"""Recoverable local review decisions. Lock order: candidate, then formal case.

The durable intent records a previously validated human decision. Recovery only
finishes that decision; it never invents an approval. Single process only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from packages.core.errors import ConflictError
from packages.execution.formal_lock import case_lock
from packages.execution.review_policy import is_self_approver


def checkpoint(stage: str) -> None:
    """No-op seam for subprocess crash tests; no runtime/environment switch."""


def json_bytes(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, *, hook: Callable[[str], None] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if hook:
            hook("TEMP_DURABLE")
        os.replace(temp, path)
        if hook:
            hook("REPLACED")
    finally:
        temp.unlink(missing_ok=True)


class CandidateTransactions:
    def __init__(self, store: Any, case_dir: Path) -> None:
        self.store = store
        self.case_dir = Path(case_dir)
        self.root = store.root / "transactions"

    def _path(self, cid: str) -> Path:
        if not re.fullmatch(r"cand_[A-Za-z0-9]+", cid):
            raise ConflictError("invalid candidate transaction id")
        return self.root / f"{cid}.json"

    def _save(self, tx: dict[str, Any], stage: str) -> None:
        tx["stage"] = stage
        atomic_write(self._path(tx["candidate_id"]), json_bytes(tx))
        checkpoint(stage)

    def recover(self, cid: str) -> None:
        path = self._path(cid)
        if not path.exists():
            return
        try:
            tx = json.loads(path.read_bytes())
            if tx["candidate_id"] != cid or not re.fullmatch(r"TC-[A-Z0-9-]+", tx["case_id"]):
                raise ValueError("transaction identity mismatch")
            if tx["stage"] == "BLOCKED":
                raise ConflictError("candidate transaction BLOCKED", details={"candidate_id": cid})
            if (
                tx["stage"]
                not in {"INTENT_DURABLE", "FORMAL_CASE_DURABLE", "CANDIDATE_STATE_DURABLE", "DONE"}
                or digest(json_bytes(tx["before"])) != tx["before_sha256"]
                or digest(json_bytes(tx["after"])) != tx["after_sha256"]
                or tx["before"]["status"] != "PENDING_REVIEW"
                or tx["after"]["candidate_id"] != cid
                or tx["after"]["case_id"] != tx["case_id"]
                or tx["after"]["source_trace_hash"] != tx["source_trace_hash"]
                or tx["before"]["source_trace_hash"] != tx["source_trace_hash"]
                or tx["before"]["candidate_id"] != cid
                or tx["before"]["case_id"] != tx["case_id"]
                or tx["original_status"] != tx["before"]["status"]
                or tx["after"]["review_transaction_id"] != tx["transaction_id"]
                or tx["after"]["reviewer"] != tx["reviewer"]
                or tx["created_at"] != tx["after"]["reviewed_at"]
                or tx["approval_record"] != tx["after"].get("approval_record", "")
                or tx["review_confirmed"] != tx["after"].get("review_confirmed", [])
                or tx["uncertainty_acknowledged"]
                != tx["after"].get("uncertainty_acknowledged", False)
                or not tx["reviewer"].strip()
                or is_self_approver(tx["reviewer"])
                or tx["target_status"] != tx["after"]["status"]
                or (tx["operation"], tx["target_status"])
                not in {("APPROVE", "APPROVED"), ("REJECT", "REJECTED")}
                or (tx["formal"] is not None) != (tx["operation"] == "APPROVE")
            ):
                self._blocked(tx, "invalid durable decision")
            with case_lock(self.case_dir, tx["case_id"]):
                self._finish(tx)
        except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
            # Preserve malformed intent bytes. Never guess ownership or paths.
            raise ConflictError(
                "candidate recovery BLOCKED", details={"candidate_id": cid}
            ) from exc

    def recover_all(self) -> list[dict[str, str]]:
        results = []
        for path in sorted(self.root.glob("cand_*.json")):
            cid = path.stem
            with self.store.lock(cid):
                try:
                    self.recover(cid)
                    results.append({"candidate_id": cid, "status": "RECOVERED"})
                except ConflictError as exc:
                    results.append({"candidate_id": cid, "status": "BLOCKED", "reason": str(exc)})
        return results

    def commit(
        self, before: dict[str, Any], after: dict[str, Any], formal: dict[str, Any] | None = None
    ) -> None:
        cid = before["candidate_id"]
        if not re.fullmatch(r"TC-[A-Z0-9-]+", before["case_id"]):
            raise ConflictError("invalid formal case id in review decision")
        if self._path(cid).exists():
            raise ConflictError("candidate already has a decision transaction")
        tid = uuid4().hex
        after["review_transaction_id"] = tid
        asset = {**formal, "_candidate_transaction": tid} if formal is not None else None
        tx = {
            "transaction_id": tid,
            "candidate_id": cid,
            "case_id": before["case_id"],
            "operation": "APPROVE" if asset is not None else "REJECT",
            "original_status": before["status"],
            "target_status": after["status"],
            "source_trace_hash": before["source_trace_hash"],
            "reviewer": after["reviewer"],
            "approval_record": after.get("approval_record", ""),
            "review_confirmed": after.get("review_confirmed", []),
            "uncertainty_acknowledged": after.get("uncertainty_acknowledged", False),
            "created_at": after["reviewed_at"],
            "before": before,
            "after": after,
            "before_sha256": digest(json_bytes(before)),
            "after_sha256": digest(json_bytes(after)),
            "formal": asset,
            "formal_sha256": digest(json_bytes(asset)) if asset is not None else "",
        }
        with case_lock(self.case_dir, tx["case_id"]):
            target = self.case_dir / f"{tx['case_id']}.json"
            if asset is not None:
                for path in self.root.glob("cand_*.json"):
                    try:
                        other = json.loads(path.read_bytes())
                        if other["operation"] == "APPROVE" and other["case_id"] == tx["case_id"]:
                            raise ConflictError("formal case is reserved by another review intent")
                    except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
                        raise ConflictError(
                            "unreadable review intent; formal creation BLOCKED"
                        ) from exc
            if asset is not None and target.exists():
                self._blocked(tx, "pre-existing formal file has no matching durable intent")
            checkpoint("PREPARED")
            self._save(tx, "INTENT_DURABLE")
            self._finish(tx)

    def _blocked(self, tx: dict[str, Any], reason: str) -> None:
        tx["blocked_reason"] = reason
        self._save(tx, "BLOCKED")
        raise ConflictError("candidate transaction BLOCKED", details={"reason": reason})

    def _finish(self, tx: dict[str, Any]) -> None:
        cid = tx["candidate_id"]
        current = self.store.get(cid)
        before, after = tx["before"], tx["after"]
        if current not in (before, after):
            self._blocked(tx, "candidate differs from both intent snapshots")
        asset = tx["formal"]
        target = self.case_dir / f"{tx['case_id']}.json"
        if asset is not None:
            data = json_bytes(asset)
            if (
                digest(data) != tx["formal_sha256"]
                or asset.get("_candidate_transaction") != tx["transaction_id"]
                or asset.get("case_id") != tx["case_id"]
            ):
                self._blocked(tx, "intent formal digest mismatch")
            if target.exists():
                if target.read_bytes() != data:
                    self._blocked(tx, "formal file ownership or content mismatch")
            elif tx["stage"] in {"FORMAL_CASE_DURABLE", "CANDIDATE_STATE_DURABLE", "DONE"}:
                self._blocked(tx, "committed formal asset is missing")
            else:
                atomic_write(target, data, hook=lambda s: checkpoint(f"FORMAL_{s}"))
        if tx["stage"] == "DONE":
            if current != after:
                self._blocked(tx, "completed candidate state was modified")
            return
        self._save(tx, "FORMAL_CASE_DURABLE")
        checkpoint("BEFORE_CANDIDATE_STATE")
        self.store.save(after, hook=lambda s: checkpoint(f"CANDIDATE_{s}"))
        self._save(tx, "CANDIDATE_STATE_DURABLE")
        checkpoint("BEFORE_DONE")
        self._save(tx, "DONE")
