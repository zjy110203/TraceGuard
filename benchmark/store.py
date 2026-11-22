"""File-backed persistence for the benchmark track (R4-2).

Why files, not SQLite: benchmark artefacts are versioned, append-only
documents that must be exportable/restorable by hand; one JSON file per
versioned object keeps provenance auditable without a migration.

Discipline:
* GT files, dataset manifests and experiment results are append-only —
  overwriting an existing version/id raises ConflictError;
* every file carries a sha256 of its payload so the on-disk state can be
  verified against the manifest later;
* the store never hands GroundTruthRecord objects to subjects — subjects
  receive scenario ids (str) only, enforced by the engine's call signature.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.engine import Dataset, ExperimentResult, GroundTruthRecord
from packages.core.errors import ConflictError, ValidationError


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


class BenchmarkStore:
    """Append-only JSON persistence under ``benchmark/data/``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._gt_dir = self.root / "gt"
        self._ds_dir = self.root / "datasets"
        self._ex_dir = self.root / "experiments"
        for d in (self._gt_dir, self._ds_dir, self._ex_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ ground truth

    def save_gt_version(self, records: list[GroundTruthRecord], version: str) -> Path:
        path = self._gt_dir / f"gt-{version}.json"
        if path.exists():
            raise ConflictError(f"GT version already persisted: {version}")
        payload = {
            "kind": "ground_truth",
            "version": version,
            "created_at": _now(),
            "records": [asdict(r) for r in records],
        }
        envelope = {"payload": payload, "sha256": _sha256(payload)}
        path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_gt_versions(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self._gt_dir.glob("gt-*.json")):
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("sha256") != _sha256(envelope["payload"]):
                raise ValidationError(f"GT file integrity mismatch: {path.name}")
            out.append(envelope["payload"])
        return out

    # ---------------------------------------------------------------- datasets

    def save_dataset(self, ds: Dataset, manifest: dict[str, Any] | None = None) -> Path:
        path = self._ds_dir / f"{ds.dataset_id}-{ds.version.replace(':', '_')}.json"
        if path.exists():
            raise ConflictError(f"dataset already persisted: {ds.dataset_id}@{ds.version}")
        payload = {
            "kind": "dataset",
            "dataset": ds.to_dict(),
            "manifest": manifest or {},
            "created_at": _now(),
        }
        envelope = {"payload": payload, "sha256": _sha256(payload)}
        path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_datasets(self) -> list[Dataset]:
        out: list[Dataset] = []
        for path in sorted(self._ds_dir.glob("*.json")):
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("sha256") != _sha256(envelope["payload"]):
                raise ValidationError(f"dataset file integrity mismatch: {path.name}")
            d = envelope["payload"]["dataset"]
            out.append(
                Dataset(
                    dataset_id=d["dataset_id"],
                    partition=d["partition"],
                    scenario_ids=tuple(d["scenario_ids"]),
                    version=d["version"],
                    metadata=d.get("metadata", {}),
                )
            )
        return out

    # -------------------------------------------------------------- experiments

    def save_experiment(self, result: ExperimentResult) -> Path:
        path = self._ex_dir / f"{result.experiment_id}.json"
        if path.exists():
            raise ConflictError(
                f"experiment result is immutable and already persisted: {result.experiment_id}"
            )
        payload = {"kind": "experiment", "result": result.to_dict(), "created_at": _now()}
        envelope = {"payload": payload, "sha256": _sha256(payload)}
        path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_experiments(self) -> list[ExperimentResult]:
        out: list[ExperimentResult] = []
        for path in sorted(self._ex_dir.glob("*.json")):
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("sha256") != _sha256(envelope["payload"]):
                raise ValidationError(f"experiment file integrity mismatch: {path.name}")
            r = envelope["payload"]["result"]
            out.append(
                ExperimentResult(
                    experiment_id=r["experiment_id"],
                    track=r["track"],
                    method=r["method"],
                    dataset_id=r["dataset_id"],
                    scenario_count=r["scenario_count"],
                    metrics=r["metrics"],
                    per_scenario=r.get("per_scenario", []),
                    metadata=r.get("metadata", {}),
                    status=r.get("status", "COMPLETED"),
                    note=r.get("note", ""),
                )
            )
        return out

    def restore(self, gt: Any, datasets: Any, experiments: Any) -> None:
        """Reload persisted state into live managers (repeatable bootstrap)."""
        seen_gt: dict[str, GroundTruthRecord] = {}
        for payload in self.load_gt_versions():
            for r in payload["records"]:
                rec = GroundTruthRecord(
                    scenario_id=r["scenario_id"],
                    expected_verdict=r["expected_verdict"],
                    primary_cause=r["primary_cause"],
                    fault_class=r["fault_class"],
                    dataset_version=r["dataset_version"],
                )
                if rec.scenario_id not in seen_gt:
                    seen_gt[rec.scenario_id] = rec
        for rec in seen_gt.values():
            try:
                gt.add(rec)
            except ConflictError:
                pass  # idempotent restore

        for ds in self.load_datasets():
            try:
                datasets.create(ds.dataset_id, ds.partition, ds.scenario_ids, metadata=ds.metadata)
            except (ConflictError, ValidationError):
                pass  # already present or GT not yet available: keep in-memory copy

        for res in self.load_experiments():
            experiments._results.setdefault(res.experiment_id, res)
