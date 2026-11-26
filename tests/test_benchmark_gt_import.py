"""Approval preparation uses synthetic records only; real GT stays unresolved."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from benchmark.engine import GroundTruthRecord
from benchmark.gt_import import import_approved_gt
from benchmark.store import BenchmarkStore, _sha256
from packages.core.errors import ConflictError, ValidationError


def fixture(tmp_path: Path) -> tuple[BenchmarkStore, dict[str, Any]]:
    store = BenchmarkStore(tmp_path)
    store.save_gt_version(
        [GroundTruthRecord("synthetic-case", "FAILED", "UNRESOLVED", "UNRESOLVED", "gt-v1")],
        "gt-v1",
    )
    base = store.load_gt_versions()[0]
    return store, {
        "decision": "APPROVED",
        "reviewer": "test-human",
        "approval_record": "SYNTHETIC-ONLY",
        "approved_at": "2026-11-26T12:00:00Z",
        "base_version": "gt-v1",
        "base_sha256": _sha256(base),
        "records": [
            {
                "scenario_id": "synthetic-case",
                "primary_cause": "API",
                "fault_class": "API",
                "authority_ref": "synthetic decision",
            }
        ],
    }


def test_gt_import_appends_and_preserves_base(tmp_path: Path) -> None:
    store, decision = fixture(tmp_path)
    before = (store._gt_dir / "gt-gt-v1.json").read_bytes()
    target = import_approved_gt(store, decision)
    assert (store._gt_dir / "gt-gt-v1.json").read_bytes() == before
    payload = json.loads(target.read_bytes())["payload"]
    assert payload["records"][0]["expected_verdict"] == "FAILED"
    assert payload["approval"] == decision
    assert payload["activation"].startswith("OFFLINE_ONLY")
    with pytest.raises(ConflictError):
        import_approved_gt(store, decision)


@pytest.mark.parametrize(
    "damage",
    [
        "reviewer",
        "approval",
        "digest",
        "unknown",
        "duplicate",
        "missing",
        "label",
        "authority",
        "verdict",
        "version",
    ],
)
def test_gt_import_strict_rejection(tmp_path: Path, damage: str) -> None:
    store, original = fixture(tmp_path)
    decision = copy.deepcopy(original)
    version = "gt-v2"
    if damage == "reviewer":
        decision["reviewer"] = "mock-agent"
    if damage == "approval":
        decision["decision"] = "PENDING"
    if damage == "digest":
        decision["base_sha256"] = "0" * 64
    if damage == "unknown":
        decision["records"][0]["scenario_id"] = "unknown"
    if damage == "duplicate":
        decision["records"] *= 2
    if damage == "missing":
        decision["records"] = []
    if damage == "label":
        decision["records"][0]["primary_cause"] = "UNRESOLVED"
    if damage == "authority":
        decision["records"][0]["authority_ref"] = ""
    if damage == "verdict":
        decision["records"][0]["expected_verdict"] = "PASSED"
    if damage == "version":
        version = "../escape"
    with pytest.raises(ValidationError):
        import_approved_gt(store, decision, version=version)
    assert len(store.load_gt_versions()) == 1
