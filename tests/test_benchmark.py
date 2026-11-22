"""Stage-11 acceptance: UI-FaultBench three tracks, GT isolation, metrics.

Verifies:
  * Ground Truth isolated in a separate manager; subject predict() receives
    ONLY scenario id, never GT; Trusted/Agent partitions separate + versioned;
  * diagnosis metrics with explicit statistical contracts (unit/numerator/
    denominator/exclusions/missing/multi-root/variance);
  * INCONCLUSIVE never PASSED; Selective Accuracy reported with Coverage;
  * false pass rate; self-healing metrics; ablation axes;
  * LLM baselines without credentials recorded as NOT_RUN, never fabricated;
  * results traceable to dataset version + metadata.
"""

from __future__ import annotations

import pytest

from benchmark.engine import (
    AblationRunner,
    DatasetManager,
    DatasetPartition,
    ExperimentManager,
    GroundTruthManager,
    GroundTruthRecord,
    metric_def,
)
from packages.core.errors import ConflictError, ValidationError


def _setup():
    gt = GroundTruthManager()
    gt.add(GroundTruthRecord("s1", "FAILED", "persistence", "PERSISTENCE", gt.version))
    gt.add(GroundTruthRecord("s2", "FAILED", "api_500", "API", gt.version))
    gt.add(GroundTruthRecord("s3", "PASSED", "none", "NONE", gt.version))
    ds = DatasetManager(gt)
    trusted = ds.create("d-trusted", DatasetPartition.TRUSTED, ("s1", "s2", "s3"))
    generated = ds.create("d-gen", DatasetPartition.AGENT_GENERATED, ("s1",))
    return gt, ds, trusted, generated, ExperimentManager(ds, gt)


def test_ground_truth_isolation() -> None:
    gt, ds, trusted, generated, exp = _setup()
    # GT manager never handed to subject; predict receives only scenario id.
    seen: list[str] = []

    def predict(sid: str):
        seen.append(sid)
        return {"primary_cause": "unknown"}

    exp.run_diagnosis("e1", "d-trusted", "rule", predict)
    assert all(isinstance(s, str) for s in seen)  # no GT object leaked into subject
    assert generated.dataset_id != trusted.dataset_id
    assert trusted.partition == "TRUSTED_TEST_SET"
    assert generated.partition == "AGENT_GENERATED_TEST_SET"
    assert trusted.version.startswith("gt-v1:")
    with pytest.raises(ConflictError):
        ds.create("d-trusted", DatasetPartition.TRUSTED, ("s1",))
    with pytest.raises(ValidationError):
        ds.create("bad", "NOT_A_PARTITION", ("s1",))


def test_metric_contracts_complete() -> None:
    for name in (
        "macro_f1",
        "primary_cause_accuracy",
        "false_pass_rate",
        "coverage",
        "selective_accuracy",
        "probe_count",
        "assertion_preservation",
        "replay_stability",
    ):
        m = metric_def(name)
        for field in (
            "unit",
            "numerator",
            "denominator",
            "exclusions",
            "missing_value",
            "multi_root",
            "variance",
        ):
            assert getattr(m, field), f"{name}.{field} empty"
    with pytest.raises(ValidationError):
        metric_def("made_up_metric")


def test_diagnosis_metrics_and_inconclusive_never_passed() -> None:
    _, _, _, _, exp = _setup()

    def predict(sid: str):
        mapping = {
            "s1": ("persistence", "FAILED"),
            "s2": ("INCONCLUSIVE", "FAILED"),
            "s3": ("none", "PASSED"),
        }
        cause, verdict = mapping[sid]
        return {"primary_cause": cause, "test_verdict": verdict, "probe_count": 2}

    result = exp.run_diagnosis("e-diag", "d-trusted", "rule", predict)
    assert result.scenario_count == 3
    assert result.metrics["coverage"] == pytest.approx(2 / 3, abs=1e-3)
    # selective accuracy correct/(answered=2) = 2/2; s2 inconclusive not correct
    assert result.metrics["selective_accuracy"] == pytest.approx(1.0, abs=1e-3)
    assert result.metrics["primary_cause_accuracy"] == pytest.approx(2 / 3, abs=1e-3)
    # false pass: truth FAILED (s1,s2), none predicted PASSED -> 0
    assert result.metrics["false_pass_rate"] == 0.0
    assert result.metrics["probe_count"] == pytest.approx(2.0, abs=1e-3)
    assert "INCONCLUSIVE" in result.note


def test_false_pass_rate_detects_lie() -> None:
    _, _, _, _, exp = _setup()

    def liar(sid: str):
        # every failing scenario falsely reported PASSED
        return {"primary_cause": "persistence", "test_verdict": "PASSED"}

    result = exp.run_diagnosis("e-lie", "d-trusted", "false-pass", liar)
    assert result.metrics["false_pass_rate"] == 1.0


def test_self_healing_metrics() -> None:
    _, _, _, _, exp = _setup()

    def repair(sid: str):
        return {"assertion_preserved": sid != "s2", "sandbox_passed": True, "promoted": sid != "s2"}

    result = exp.run_self_healing("e-heal", "d-trusted", "oracle-protected", repair)
    assert result.metrics["assertion_preservation"] == pytest.approx(2 / 3, abs=1e-3)
    assert result.metrics["acceptance"] == pytest.approx(2 / 3, abs=1e-3)
    assert result.metadata["dataset_version"].startswith("gt-v1:")


def test_llm_baseline_without_credentials_is_not_run() -> None:
    _, _, _, _, exp = _setup()
    result = exp.not_run("e-llm", "diagnosis", "single-llm", "no LLM credential configured")
    assert result.status == "NOT_RUN"
    assert result.metrics == {}
    assert "no LLM credential" in result.note


def test_ablations_named_and_traceable() -> None:
    _, _, _, _, exp = _setup()
    exp.run_diagnosis("e-base", "d-trusted", "full", lambda s: {"primary_cause": "persistence"})
    ablation = AblationRunner(exp)
    result = ablation.run(
        "e-base", "evidence_graph", "d-trusted", lambda s: {"primary_cause": "INCONCLUSIVE"}
    )
    assert result.metadata["removed"] == "evidence_graph"
    assert result.metadata["ablation_of"] == "e-base"
    assert result.method == "ablation:evidence_graph"
    with pytest.raises(ValidationError):
        ablation.run("e-base", "not_an_axis", "d-trusted", lambda s: {})


def test_experiment_metadata_complete() -> None:
    _, _, _, _, exp = _setup()
    result = exp.run_diagnosis(
        "e-meta",
        "d-trusted",
        "rule",
        lambda s: {"primary_cause": "none"},
        metadata={
            "model_version": "deterministic",
            "prompt_version": "none",
            "tool_version": "1.0.0",
            "browser_version": "chrome",
            "dataset_version": "gt-v1",
        },
    )
    for key in (
        "dataset_id",
        "dataset_version",
        "partition",
        "at",
        "model_version",
        "prompt_version",
    ):
        assert key in result.metadata
    assert exp.get("e-meta").experiment_id == "e-meta"


def test_unresolved_gt_blocks_accuracy_but_keeps_verdict_metrics() -> None:
    """R4 GT gate: UNRESOLVED primary_cause must yield None (BLOCKED), never 0,
    while verdict-based metrics keep their reliable denominators."""
    gt = GroundTruthManager()
    gt.add(GroundTruthRecord("s1", "FAILED", "UNRESOLVED", "UNRESOLVED", gt.version))
    gt.add(GroundTruthRecord("s2", "PASSED", "UNRESOLVED", "UNRESOLVED", gt.version))
    ds = DatasetManager(gt)
    ds.create("d-unresolved", DatasetPartition.TRUSTED, ("s1", "s2"))
    exp = ExperimentManager(ds, gt)

    result = exp.run_diagnosis(
        "e-unresolved",
        "d-unresolved",
        "rule",
        lambda s: {"test_verdict": "FAILED", "primary_cause": "PERSISTENCE"},
    )

    assert result.metrics["primary_cause_accuracy"] is None
    assert result.metrics["selective_accuracy"] is None
    assert result.metrics["macro_f1"] is None
    # Verdict-based metrics remain measurable against authoritative verdicts.
    assert result.metrics["false_pass_rate"] == 0.0  # no truth=FAILED run was judged PASSED
    assert result.metrics["coverage"] == 1.0
    assert "UNRESOLVED" in result.note
