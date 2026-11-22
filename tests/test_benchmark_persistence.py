"""R4-2: benchmark persistence + GT service-layer isolation.

Verifies:
  * append-only persistence with integrity hashes (refuse overwrites);
  * idempotent restore into live managers;
  * GroundTruthManager is NOT reachable through app.state — the isolation is
    enforced at the service layer, not by hiding UI fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from benchmark.engine import (
    DatasetManager,
    ExperimentManager,
    GroundTruthManager,
    GroundTruthRecord,
)
from benchmark.store import BenchmarkStore
from packages.core.errors import ConflictError


def _sample_records(version: str) -> list[GroundTruthRecord]:
    return [
        GroundTruthRecord("s1", "FAILED", "locator_drift", "LOCATOR", version),
        GroundTruthRecord("s2", "FAILED", "api_500", "API", version),
    ]


def test_store_gt_roundtrip_and_integrity(tmp_path: Path) -> None:
    store = BenchmarkStore(tmp_path / "data")
    records = _sample_records("gt-v1")
    store.save_gt_version(records, "gt-v1")

    payloads = store.load_gt_versions()
    assert len(payloads) == 1
    assert payloads[0]["version"] == "gt-v1"
    assert [r["scenario_id"] for r in payloads[0]["records"]] == ["s1", "s2"]


def test_store_refuses_gt_overwrite(tmp_path: Path) -> None:
    store = BenchmarkStore(tmp_path / "data")
    store.save_gt_version(_sample_records("gt-v1"), "gt-v1")
    with pytest.raises(ConflictError):
        store.save_gt_version(_sample_records("gt-v1"), "gt-v1")


def test_store_dataset_and_experiment_append_only(tmp_path: Path) -> None:
    from benchmark.engine import ExperimentResult

    store = BenchmarkStore(tmp_path / "data")
    gt = GroundTruthManager()
    for r in _sample_records("gt-v1"):
        gt.add(r)
    ds_manager = DatasetManager(gt)
    ds = ds_manager.create("d1", "TRUSTED_TEST_SET", ("s1", "s2"))

    store.save_dataset(ds)
    with pytest.raises(ConflictError):
        store.save_dataset(ds)

    loaded = store.load_datasets()
    assert len(loaded) == 1
    assert loaded[0].dataset_id == "d1"
    assert loaded[0].scenario_ids == ("s1", "s2")

    result = ExperimentResult(
        "exp-1", "diagnosis", "rule", "d1", 2, {"macro_f1": 0.5}, [], {"at": "t"}
    )
    store.save_experiment(result)
    with pytest.raises(ConflictError):
        store.save_experiment(result)
    loaded_exp = store.load_experiments()
    assert loaded_exp[0].experiment_id == "exp-1"
    assert loaded_exp[0].metrics["macro_f1"] == 0.5


def test_tampered_file_fails_integrity(tmp_path: Path) -> None:
    import json

    store = BenchmarkStore(tmp_path / "data")
    store.save_gt_version(_sample_records("gt-v1"), "gt-v1")
    target = next((tmp_path / "data" / "gt").glob("*.json"))
    envelope = json.loads(target.read_text(encoding="utf-8"))
    envelope["payload"]["records"][0]["primary_cause"] = "tampered"
    target.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    from packages.core.errors import ValidationError

    with pytest.raises(ValidationError):
        store.load_gt_versions()


def test_restore_is_idempotent(tmp_path: Path) -> None:
    store = BenchmarkStore(tmp_path / "data")
    store.save_gt_version(_sample_records("gt-v1"), "gt-v1")

    gt = GroundTruthManager()
    ds_manager = DatasetManager(gt)
    exp_manager = ExperimentManager(ds_manager, gt)

    store.restore(gt, ds_manager, exp_manager)
    store.restore(gt, ds_manager, exp_manager)  # second restore must not blow up

    assert len(gt.all()) == 2


def test_gt_not_reachable_via_app_state(tmp_path: Path) -> None:
    """Service-layer GT isolation: register_extensions must not expose GT."""
    from apps.api.extensions import register_extensions

    app = FastAPI()
    register_extensions(
        app, case_dir=Path("benchmark/cases"), benchmark_data_dir=tmp_path / "bdata"
    )

    assert not hasattr(app.state, "gt_manager"), (
        "GroundTruthManager must not be reachable from app.state"
    )
    # The exposed handles are read-only facades: no GT attribute on them either.
    assert not hasattr(app.state.dataset_manager, "gt")
    assert not hasattr(app.state.experiment_manager, "gt")
    # No route leaks GT-derived data on a fresh install:
    client = TestClient(app)
    assert client.get("/api/v1/benchmark/datasets").json() == []
    assert client.get("/api/v1/benchmark/experiments").json() == []
