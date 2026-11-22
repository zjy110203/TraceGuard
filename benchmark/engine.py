"""Stage-11 UI-FaultBench (C12) — dataset/GT/experiment/evaluation/ablation.

Three evaluation tracks (v1.1 §阶段11):
  A Test Generation   B Diagnosis   C Self-Healing
with baseline methods A-E and metrics defined with explicit statistical
contracts (unit, numerator, denominator, exclusions, missing values,
multi-root handling, variance). INCONCLUSIVE is NEVER counted as PASSED.
Selective Accuracy is always reported with Coverage.

Ground Truth isolation: GT lives in a separate GroundTruthManager that is NOT
passed to evaluation subjects (Explorer/Compiler/Agent). Only the Evaluation
Engine reads GT to score results. Trusted Test Set and Agent Generated Set
are separate partitions with a dataset version that cannot silently change.

No LLM credentials: single-LLM baselines are marked NOT_RUN (not fabricated).
Deterministic baselines (manual case library, rule diagnosis, rule repair
guards) run with real data from earlier stages.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from packages.core.errors import ConflictError, ValidationError


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DatasetPartition:
    TRUSTED = "TRUSTED_TEST_SET"
    AGENT_GENERATED = "AGENT_GENERATED_TEST_SET"


@dataclass
class MetricDefinition:
    """Explicit statistical contract for a metric (no fabricated numbers)."""

    name: str
    unit: str
    numerator: str
    denominator: str
    exclusions: str
    missing_value: str
    multi_root: str
    variance: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


METRICS: dict[str, MetricDefinition] = {
    # Diagnosis
    "macro_f1": MetricDefinition(
        "macro_f1",
        "per fault class, averaged",
        "2*TP/(2*TP+FP+FN) per class",
        "number of fault classes with ground truth",
        "classes with zero support reported separately",
        "missing prediction = FN",
        "multi-root counted per class independently",
        "bootstrap CI deferred to experiment config (not fabricated)",
    ),
    "primary_cause_accuracy": MetricDefinition(
        "primary_cause_accuracy",
        "per scenario",
        "scenarios with correct primary cause",
        "scenarios with ground truth primary cause",
        "INCONCLUSIVE predictions excluded from numerator",
        "INCONCLUSIVE kept in denominator",
        "only the primary root cause compared; extra roots reported separately",
        "mean +/- std over scenarios",
    ),
    "false_pass_rate": MetricDefinition(
        "false_pass_rate",
        "per scenario",
        "truth=FAILED but verdict=PASSED",
        "all scenarios with truth=FAILED",
        "none",
        "missing verdict excluded and reported",
        "n/a",
        "binomial interval deferred",
    ),
    "coverage": MetricDefinition(
        "coverage",
        "per scenario",
        "scenarios with a non-INCONCLUSIVE answer",
        "all evaluated scenarios",
        "none",
        "missing answer = not covered",
        "n/a",
        "mean +/- std",
    ),
    "selective_accuracy": MetricDefinition(
        "selective_accuracy",
        "per answered scenario",
        "answered scenarios that are correct",
        "answered scenarios (coverage reported alongside)",
        "INCONCLUSIVE answers not counted as correct",
        "missing = unanswered",
        "n/a",
        "always reported WITH coverage",
    ),
    "probe_count": MetricDefinition(
        "probe_count",
        "per scenario",
        "sum of probes used",
        "number of scenarios",
        "rejected probes reported separately",
        "missing = 0 with note",
        "n/a",
        "mean +/- std",
    ),
    # Self-healing / generation
    "assertion_preservation": MetricDefinition(
        "assertion_preservation",
        "per repair",
        "repairs preserving all required assertions",
        "all evaluated repairs",
        "rejected proposals reported separately",
        "missing check = failure",
        "n/a",
        "binomial interval deferred",
    ),
    "replay_stability": MetricDefinition(
        "replay_stability",
        "per repeated replay",
        "replays with identical verdict",
        "all replays for the case",
        "environment-fault replays reported separately",
        "missing replay = not stable",
        "n/a",
        "std across replays",
    ),
}


@dataclass
class GroundTruthRecord:
    scenario_id: str
    expected_verdict: str
    primary_cause: str
    fault_class: str
    dataset_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GroundTruthManager:
    """Separate GT store. Never passed to Agent/Explorer/Compiler/subjects."""

    def __init__(self) -> None:
        self._records: dict[str, GroundTruthRecord] = {}
        self._version = "gt-v1"

    @property
    def version(self) -> str:
        return self._version

    def add(self, record: GroundTruthRecord) -> None:
        if record.scenario_id in self._records:
            raise ConflictError(f"GT exists: {record.scenario_id}")
        if record.expected_verdict not in ("PASSED", "FAILED"):
            raise ValidationError("GT verdict must be PASSED/FAILED")
        self._records[record.scenario_id] = record

    def get(self, scenario_id: str) -> GroundTruthRecord:
        rec = self._records.get(scenario_id)
        if rec is None:
            raise ValidationError(f"no ground truth for {scenario_id}")
        return rec

    def all(self) -> dict[str, GroundTruthRecord]:
        return dict(self._records)  # copy; evaluation engine only


@dataclass
class Dataset:
    dataset_id: str
    partition: str
    scenario_ids: tuple[str, ...]
    version: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scenario_ids"] = list(self.scenario_ids)
        return d


class DatasetManager:
    """Trusted/Agent-generated partitions with immutable versions."""

    def __init__(self, gt: GroundTruthManager) -> None:
        self.gt = gt
        self._datasets: dict[str, Dataset] = {}

    def create(
        self,
        dataset_id: str,
        partition: str,
        scenario_ids: tuple[str, ...],
        metadata: dict[str, Any] | None = None,
    ) -> Dataset:
        if partition not in (DatasetPartition.TRUSTED, DatasetPartition.AGENT_GENERATED):
            raise ValidationError(f"invalid partition: {partition}")
        if dataset_id in self._datasets:
            raise ConflictError(f"dataset exists: {dataset_id}")
        for sid in scenario_ids:
            self.gt.get(sid)  # ensure GT exists at creation; version frozen in dataset
        ds = Dataset(
            dataset_id,
            partition,
            scenario_ids,
            version=f"{self.gt.version}:{hashlib.sha256(('|'.join(scenario_ids)).encode()).hexdigest()[:8]}",
            metadata=dict(metadata or {}),
        )
        self._datasets[dataset_id] = ds
        return ds

    def get(self, dataset_id: str) -> Dataset:
        if dataset_id not in self._datasets:
            raise ValidationError(f"unknown dataset: {dataset_id}")
        return self._datasets[dataset_id]

    def all(self) -> list[Dataset]:
        return list(self._datasets.values())


@dataclass
class ExperimentResult:
    experiment_id: str
    track: str
    method: str
    dataset_id: str
    scenario_count: int
    metrics: dict[str, float | int | None]
    per_scenario: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "COMPLETED"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExperimentManager:
    """Runs baselines and records results with full experiment metadata."""

    def __init__(self, datasets: DatasetManager, gt: GroundTruthManager) -> None:
        self.datasets = datasets
        self.gt = gt
        self._results: dict[str, ExperimentResult] = {}

    def run_diagnosis(
        self,
        experiment_id: str,
        dataset_id: str,
        method: str,
        predict: Callable[[str], dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> ExperimentResult:
        """B/E diagnosis evaluation. INCONCLUSIVE never counted as PASSED."""
        ds = self.datasets.get(dataset_id)
        per: list[dict[str, Any]] = []
        tp: dict[str, int] = defaultdict(int)
        fp: dict[str, int] = defaultdict(int)
        fn: dict[str, int] = defaultdict(int)
        correct_primary = 0
        answered = 0
        false_pass = 0
        probes = 0
        truth_failed = 0
        for sid in ds.scenario_ids:
            truth = self.gt.get(sid)
            pred = predict(sid)  # subject receives ONLY scenario input, never GT
            verdict = str(pred.get("test_verdict", ""))
            cause = str(pred.get("primary_cause", "INCONCLUSIVE"))
            is_answered = cause not in ("INCONCLUSIVE", "", "UNKNOWN")
            is_correct = is_answered and cause == truth.primary_cause
            if truth.expected_verdict == "FAILED":
                truth_failed += 1
                if verdict == "PASSED":
                    false_pass += 1
            fc = truth.fault_class
            if is_correct:
                tp[fc] += 1
                correct_primary += 1
            elif is_answered:
                fp[fc] += 1
            else:
                fn[fc] += 1
            if is_answered:
                answered += 1
            probes += int(pred.get("probe_count", 0))
            per.append(
                {
                    "scenario_id": sid,
                    "truth_cause": truth.primary_cause,
                    "predicted_cause": cause,
                    "correct": is_correct,
                    "answered": is_answered,
                    "verdict": verdict,
                }
            )
        n = len(ds.scenario_ids)
        classes = list({self.gt.get(s).fault_class for s in ds.scenario_ids})
        f1s = []
        for cls in classes:
            denom = 2 * tp[cls] + fp[cls] + fn[cls]
            f1s.append((2 * tp[cls] / denom) if denom else 0.0)
        macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0

        # R4 GT gate: primary-cause metrics are only meaningful against an
        # approved mapping. When GT carries UNRESOLVED causes (mapping pending
        # human approval), the accuracy numbers must be None/BLOCKED — never 0,
        # which would fabricate a measured result.
        gt_resolved = all(
            self.gt.get(s).primary_cause not in ("", "UNRESOLVED") for s in ds.scenario_ids
        )
        note = "INCONCLUSIVE excluded from selective numerator, kept in coverage denominator"
        blocked: dict[str, float | int | None] = {
            "primary_cause_accuracy": round(correct_primary / n, 4) if n else 0,
            "selective_accuracy": round(correct_primary / answered, 4) if answered else 0.0,
            "macro_f1": round(macro_f1, 4),
        }
        if not gt_resolved:
            blocked = {k: None for k in blocked}
            note = (
                "PRIMARY_CAUSE_MAPPING_UNRESOLVED: accuracy metrics BLOCKED pending "
                "approved GT mapping (docs/benchmark/R4-GT-Mapping-Proposal.md); "
                "verdict-based metrics remain valid"
            )
        result = ExperimentResult(
            experiment_id,
            "diagnosis",
            method,
            dataset_id,
            n,
            {
                "macro_f1": blocked["macro_f1"],
                "primary_cause_accuracy": blocked["primary_cause_accuracy"],
                "false_pass_rate": round(false_pass / truth_failed, 4) if truth_failed else 0.0,
                "coverage": round(answered / n, 4) if n else 0,
                "selective_accuracy": blocked["selective_accuracy"],
                "probe_count": round(probes / n, 4) if n else 0,
            },
            per,
            self._meta(ds, metadata),
            note=note,
        )
        self._results[experiment_id] = result
        return result

    def run_self_healing(
        self,
        experiment_id: str,
        dataset_id: str,
        method: str,
        repair: Callable[[str], dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> ExperimentResult:
        ds = self.datasets.get(dataset_id)
        per: list[dict[str, Any]] = []
        preserved = 0
        validated = 0
        promoted = 0
        for sid in ds.scenario_ids:
            outcome = repair(sid)
            ok = bool(outcome.get("assertion_preserved"))
            preserved += int(ok)
            validated += int(bool(outcome.get("sandbox_passed")))
            promoted += int(bool(outcome.get("promoted")))
            per.append({"scenario_id": sid, **outcome})
        n = len(ds.scenario_ids)
        result = ExperimentResult(
            experiment_id,
            "self_healing",
            method,
            dataset_id,
            n,
            {
                "assertion_preservation": round(preserved / n, 4) if n else 0,
                "repair_validation_success": round(validated / n, 4) if n else 0,
                "acceptance": round(promoted / n, 4) if n else 0,
            },
            per,
            self._meta(ds, metadata),
        )
        self._results[experiment_id] = result
        return result

    def not_run(self, experiment_id: str, track: str, method: str, reason: str) -> ExperimentResult:
        """Honest placeholder for LLM baselines without credentials — NOT_RUN."""
        result = ExperimentResult(
            experiment_id, track, method, "", 0, {}, [], {}, status="NOT_RUN", note=reason
        )
        self._results[experiment_id] = result
        return result

    def _meta(self, ds: Dataset, extra: dict[str, Any] | None) -> dict[str, Any]:
        meta = {
            "dataset_id": ds.dataset_id,
            "dataset_version": ds.version,
            "partition": ds.partition,
            "at": _now(),
        }
        meta.update(extra or {})
        return meta

    def get(self, experiment_id: str) -> ExperimentResult:
        return self._results[experiment_id]

    def all(self) -> list[ExperimentResult]:
        return list(self._results.values())


class DatasetManagerView:
    """Read-only facade for HTTP surface: exposes get/all WITHOUT the GT handle.

    The real DatasetManager needs GroundTruthManager at creation time; the API
    surface must never carry that reference, so register_extensions mounts this
    view instead. Trusted scripts (benchmark runner) use the real manager.
    """

    def __init__(self, manager: DatasetManager) -> None:
        self._manager = manager

    def get(self, dataset_id: str) -> Dataset:
        return self._manager.get(dataset_id)

    def all(self) -> list[Dataset]:
        return self._manager.all()


class ExperimentManagerView:
    """Read-only facade: experiment results without the GT handle."""

    def __init__(self, manager: ExperimentManager) -> None:
        self._manager = manager

    def get(self, experiment_id: str) -> ExperimentResult:
        return self._manager.get(experiment_id)

    def all(self) -> list[ExperimentResult]:
        return self._manager.all()


class AblationRunner:
    """Ablations named in v1.1 §阶段11 (removal of a capability)."""

    AXES = (
        "evidence_graph",
        "active_diagnosis",
        "oracle_protection",
        "probe_budget",
        "assertion_preservation",
    )

    def __init__(self, experiments: ExperimentManager) -> None:
        self.experiments = experiments

    def run(
        self,
        base_experiment_id: str,
        axis: str,
        dataset_id: str,
        predict: Callable[[str], dict[str, Any]],
    ) -> ExperimentResult:
        if axis not in self.AXES:
            raise ValidationError(f"unknown ablation axis: {axis}")
        base = self.experiments.get(base_experiment_id)
        return self.experiments.run_diagnosis(
            f"{base_experiment_id}-abl-{axis}",
            dataset_id,
            f"ablation:{axis}",
            predict,
            metadata={
                "ablation_of": base_experiment_id,
                "removed": axis,
                "unit": base.metrics.get("macro_f1"),
            },
        )


def metric_def(name: str) -> MetricDefinition:
    if name not in METRICS:
        raise ValidationError(f"unknown metric: {name}")
    return METRICS[name]
