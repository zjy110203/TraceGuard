"""Explicit, offline approved GT import. Does not activate labels in live evaluation."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmark.store import BenchmarkStore, _sha256
from packages.core.errors import ConflictError, ValidationError
from packages.execution.candidate_transaction import atomic_write, json_bytes
from packages.execution.formal_lock import case_lock
from packages.execution.review_policy import is_self_approver

LABELS = {"API", "NETWORK", "PERSISTENCE", "UI_RENDERING", "TIMING", "NONE"}


def import_approved_gt(
    store: BenchmarkStore, decision: dict[str, Any], *, version: str = "gt-v2"
) -> Path:
    """Validate a full human decision against an immutable base before any write.

    Reviewer remains a local audit field, never an authenticated identity.
    The five proposed mappings are deliberately absent from executable code.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", version):
        raise ValidationError("invalid GT version")
    reviewer = decision.get("reviewer")
    if (
        decision.get("decision") != "APPROVED"
        or not isinstance(reviewer, str)
        or not reviewer.strip()
        or is_self_approver(reviewer)
        or not str(decision.get("approval_record") or "").strip()
        or not str(decision.get("approved_at") or "").strip()
    ):
        raise ValidationError("explicit named human GT approval is required")
    base_version = decision.get("base_version")
    try:
        approved_at = datetime.fromisoformat(decision["approved_at"])
        if approved_at.tzinfo is None:
            raise ValueError("timezone required")
    except (ValueError, TypeError) as exc:
        raise ValidationError("approval timestamp must be ISO-8601 with timezone") from exc
    if base_version == version:
        raise ValidationError("GT import must append a new version")
    bases = [p for p in store.load_gt_versions() if p["version"] == base_version]
    if len(bases) != 1 or _sha256(bases[0]) != decision.get("base_sha256"):
        raise ValidationError("GT approval base version or digest mismatch")
    base = bases[0]
    rows = decision.get("records")
    if not isinstance(rows, list) or not rows:
        raise ValidationError("complete GT decision rows required")
    originals = {r["scenario_id"]: r for r in base["records"]}
    if len(originals) != len(base["records"]):
        raise ValidationError("duplicate base scenario")
    imported: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "scenario_id",
            "primary_cause",
            "fault_class",
            "authority_ref",
        }:
            raise ValidationError("invalid GT decision row fields")
        sid = row["scenario_id"]
        if not isinstance(sid, str) or sid not in originals or sid in seen:
            raise ValidationError("unknown or duplicate GT scenario")
        if (
            not isinstance(row["primary_cause"], str)
            or not isinstance(row["fault_class"], str)
            or row["primary_cause"] not in LABELS
            or row["fault_class"] not in LABELS
        ):
            raise ValidationError("unapproved label vocabulary")
        if not isinstance(row["authority_ref"], str) or not row["authority_ref"].strip():
            raise ValidationError("each GT row requires an authoritative approval reference")
        seen.add(sid)
        imported.append(
            {
                **originals[sid],
                "primary_cause": row["primary_cause"],
                "fault_class": row["fault_class"],
                "dataset_version": version,
            }
        )
    if seen != set(originals):
        raise ValidationError("GT approval must cover exactly the base scenarios")
    payload = {
        "kind": "ground_truth",
        "version": version,
        "records": imported,
        "created_at": decision["approved_at"],
        "approval": decision,
        "activation": "OFFLINE_ONLY_REQUIRES_EXPLICIT_EVALUATION_SELECTION",
    }
    target = store._gt_dir / f"gt-{version}.json"
    with case_lock(store._gt_dir, version):
        if target.exists():
            raise ConflictError("GT version already exists; append-only")
        atomic_write(target, json_bytes({"payload": payload, "sha256": _sha256(payload)}))
    return target


def load_decision(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_bytes())
    if not isinstance(result, dict):
        raise ValidationError("GT approval must be an object")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decision", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--version", default="gt-v2")
    args = parser.parse_args()
    print(
        import_approved_gt(
            BenchmarkStore(args.root), load_decision(args.decision), version=args.version
        )
    )
