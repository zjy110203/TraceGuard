"""R4-2 benchmark runner: real experiments, persisted, honestly labelled.

Executes the three UI-FaultBench tracks against a REAL isolated TeamFlow
instance + real Chrome, with DeepSeek as the real LLM (when configured):

  Track A (test generation)  : approved manual cases replayed twice ->
                               replay_stability / locator_validity /
                               assertion_correctness. Scripted Explorer +
                               Compiler chain marked NOT_RUN (bounded budget).
  Track B (diagnosis)        : 6 seeded scenarios, 4 methods:
                               A raw-report verdict, C rule diagnosis without
                               eval-plane evidence, D rule diagnosis WITH
                               eval-plane evidence (evidence graph input),
                               B single-call real LLM (DeepSeek).
                               primary_cause metrics are BLOCKED (None) until
                               the GT mapping is approved — never fabricated.
  Track C (self-healing)     : one real RepairSandbox validation (real Chrome,
                               protected repair); manual-baseline and
                               unprotected-LLM methods marked NOT_RUN.

Safety:
* isolated SQLite DB under benchmark/data/ (never the business DB);
* TeamFlow on a dedicated local port, data reset between runs;
* LLM budget bounded: 6 calls, max_output_tokens 320, max_retries 1;
* API key only via .env/env; never printed or persisted;
* GT primary_cause stays UNRESOLVED pending human approval.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolated eval-plane DB BEFORE any settings/engine is created.
BENCH_DB = ROOT / "benchmark" / "data" / "bench_teamflow.db"
os.environ["TRACEGUARD_DATABASE_URL"] = f"sqlite:///{BENCH_DB.as_posix()}"
os.environ["TEAMFLOW_DISABLE_SEED"] = "0"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from apps.teamflow import service  # noqa: E402
from apps.teamflow.eval_models import Base as EvalBase  # noqa: E402
from apps.teamflow.models import Base as BusinessBase  # noqa: E402
from benchmark.engine import (  # noqa: E402
    AblationRunner,
    DatasetManager,
    DatasetPartition,
    ExperimentManager,
    GroundTruthManager,
    GroundTruthRecord,
)
from benchmark.store import BenchmarkStore  # noqa: E402
from packages.core import database  # noqa: E402
from packages.core.config import ensure_env_file_vars, get_settings  # noqa: E402
from packages.evidence.graph import make_in_memory_graph  # noqa: E402
from packages.execution.models import load_case  # noqa: E402
from packages.execution.pipeline import FullChain  # noqa: E402

TF_PORT = 8041
TF_BASE = f"http://127.0.0.1:{TF_PORT}"
EVAL_TOKEN = get_settings().eval_control_token
EVAL_H = {"X-Eval-Token": EVAL_TOKEN}
CASES = ROOT / "benchmark" / "cases"
RUN_AT = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

RUNTIME_META = {
    "model_version": "deepseek-chat (method B only)",
    "prompt_version": "r4-diag-v1",
    "tool_version": "traceguard-r4",
    "agent_version": "deterministic-rules+llm",
    "compiler_version": "stage2-contract",
    "teamflow_version": "0.1.0-m1",
    "testcase_version": "benchmark/cases @r4",
    "oracle_version": "contract-draft",
    "browser_version": "chrome-stable (local)",
    "test_data_seed": "seed-1",
    "runtime_environment": f"windows-local/{RUN_AT}",
}


# --------------------------------------------------------------------- plumbing


def start_teamflow() -> None:
    engine = database.create_db_engine(get_settings().database_url)
    BusinessBase.metadata.create_all(engine)
    EvalBase.metadata.create_all(engine)
    factory = database.make_session_factory(engine)
    # Seed the six authoritative scenarios (idempotent on a fresh DB).
    from scripts.seed_scenarios import META, SCENARIOS

    with factory() as session:
        for sid, fid, desc, gt in SCENARIOS:
            meta = dict(META)
            meta["fault_id"] = fid or "NONE"
            try:
                service.seed_scenario(session, sid, fid, desc, meta, gt)
                session.commit()
            except Exception:
                session.rollback()  # already seeded: authoritative values unchanged
    engine.dispose()

    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=TF_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(60):
        try:
            if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise RuntimeError("isolated TeamFlow did not become healthy")


def eval_post(path: str) -> None:
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        c.post(path, headers=EVAL_H)


def fault_events() -> list[dict[str, Any]]:
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        data = c.get("/__eval__/events", headers=EVAL_H).json()
    return data if isinstance(data, list) else list(data.get("events", []))


def make_chain() -> FullChain:
    graph, _ = make_in_memory_graph()
    return FullChain(base_url=TF_BASE, graph=graph, fault_events_provider=fault_events)


# ------------------------------------------------------------------- track B runs


SCENARIO_CASE = {
    "TF-BASE-001": "TC-CREATE-001.json",
    "TF-API-001-S1": "TC-CREATE-001.json",
    "TF-NET-001-S1": "TC-CREATE-001.json",
    "TF-DB-001-S1": "TC-CREATE-001.json",
    "TF-UI-001-S1": "TC-CREATE-002.json",
    "TF-TIME-001-S1": "TC-LIST-001.json",
}


def run_all_scenarios() -> dict[str, dict[str, Any]]:
    """Run every seeded scenario once with real Chrome; return per-scenario
    bundles carrying the raw report and the rule diagnosis (D: with eval-plane
    evidence; C: without)."""
    from packages.agents.diagnosis import DiagnosisService
    from scripts.seed_scenarios import SCENARIOS

    out: dict[str, dict[str, Any]] = {}
    for sid, fid, _desc, _gt in SCENARIOS:
        eval_post("/__eval__/faults/clear")
        eval_post("/__eval__/data/reset")
        if fid:
            # fid is the fault_id string (e.g. "TF-API-001") in SCENARIOS.
            eval_post(f"/__eval__/faults/{fid}/enable")

        window_start = datetime.now(UTC).isoformat()
        case = load_case(CASES / SCENARIO_CASE[sid])
        chain = make_chain()
        report = chain.run(f"bench-{sid}", case, fault_events=fault_events())
        window_end = datetime.now(UTC).isoformat()
        if not report.completed:
            out[sid] = {"completed": False, "error": "chain aborted"}
            continue
        execution = report.execution
        events = [
            e
            for e in fault_events()
            if window_start <= str(e.get("occurred_at") or "") <= window_end
        ]
        bundle_d = {
            "test_verdict": execution.get("test_verdict", ""),
            "execution_status": execution.get("execution_status", ""),
            "failed_step_ids": execution.get("failed_steps", []),
            "assertions": [],
            "evidence": [],
            "events": [],
            "fault_events": events,
        }
        bundle_c = dict(bundle_d, fault_events=[])
        diag_d = DiagnosisService().diagnose(f"dg_D_{sid}", sid, bundle_d).to_dict()
        diag_c = DiagnosisService().diagnose(f"dg_C_{sid}", sid, bundle_c).to_dict()
        out[sid] = {
            "completed": True,
            "verdict": execution.get("test_verdict", ""),
            "steps_ok": execution.get("steps_ok"),
            "steps_total": execution.get("steps_total"),
            "failed_steps": execution.get("failed_steps", []),
            "diag_d": diag_d,
            "diag_c": diag_c,
        }
        mech = diag_d.get("failure_mechanism")
        print(f"  scenario {sid}: verdict={out[sid]['verdict']} mech_D={mech}")
    return out


def llm_predict_texts(scenarios: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """Method B: single structured DeepSeek call per scenario. Returns
    per-scenario predictions, or None when no provider is configured."""
    from packages.agents.llm import Message, TracedClient, provider_from_env

    ensure_env_file_vars()
    wired = provider_from_env()
    if wired is None:
        return None
    provider, config = wired
    config = dataclasses.replace(config, max_output_tokens=320, max_retries=1)
    client = TracedClient(provider=provider, config=config)

    schema = {
        "type": "object",
        "properties": {
            "test_verdict": {"type": "string", "enum": ["PASSED", "FAILED", "INCONCLUSIVE"]},
            "primary_cause": {
                "type": "string",
                "enum": ["API", "NETWORK", "PERSISTENCE", "UI_RENDERING", "TIMING", "NONE"],
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        # confidence is OPTIONAL: strict-required made the model's occasional
        # omission a hard failure; the classification itself is what matters.
        "required": ["test_verdict", "primary_cause"],
        "additionalProperties": False,
    }
    predictions: dict[str, dict[str, Any]] = {}
    total_tokens = 0
    for sid, info in scenarios.items():
        if not info.get("completed"):
            predictions[sid] = {
                "test_verdict": "INCONCLUSIVE",
                "primary_cause": "INCONCLUSIVE",
                "probe_count": 0,
            }
            continue
        symptoms = f"test_verdict={info['verdict']}; failed_steps={info['failed_steps']}"
        prompt = (
            "You are a web test result classifier. Given only the observation "
            "below, decide whether the test run PASSED or FAILED, and the most "
            "likely failure mechanism (API / NETWORK / PERSISTENCE / "
            "UI_RENDERING / TIMING, or NONE if the run is healthy). "
            "confidence is optional. "
            'Respond with JSON exactly like: {"test_verdict": "FAILED", '
            '"primary_cause": "API", "confidence": "high"}. '
            f"Observation: {symptoms}."
        )
        try:
            parsed = client.complete_structured([Message(role="user", content=prompt)], schema)
            predictions[sid] = {
                "test_verdict": parsed["test_verdict"],
                "primary_cause": parsed["primary_cause"],
                "probe_count": 0,
                "confidence": parsed["confidence"],
            }
            last = client.traces[-1]
            total_tokens += last.prompt_tokens + last.completion_tokens
            print(f"  LLM {sid}: {parsed} (tokens={last.prompt_tokens}+{last.completion_tokens})")
        except Exception as exc:  # noqa: BLE001 - bounded fallback, honest NOT_RUN
            print(f"  LLM {sid}: FAILED ({type(exc).__name__}) -> INCONCLUSIVE")
            predictions[sid] = {
                "test_verdict": "INCONCLUSIVE",
                "primary_cause": "INCONCLUSIVE",
                "probe_count": 0,
            }
    RUNTIME_META["llm_total_tokens_method_b"] = total_tokens
    RUNTIME_META["llm_calls_method_b"] = len(scenarios)
    return predictions


# ----------------------------------------------------------------------- tracks


def track_a(store: BenchmarkStore, exp: ExperimentManager) -> None:
    """Approved manual cases, replayed twice on a clean app: stability metrics.
    Uses LocalRunner directly for the full step/assertion report."""
    from packages.execution.runner import LocalRunner

    verdicts: dict[str, list[str]] = {}
    locator_ok: list[float] = []
    assertion_ok: list[float] = []
    for case_name in ("TC-CREATE-002.json", "TC-LIST-001.json"):
        case = load_case(CASES / case_name)
        vs: list[str] = []
        for i in range(2):
            eval_post("/__eval__/faults/clear")
            eval_post("/__eval__/data/reset")
            runner = LocalRunner(TF_BASE, eval_token=EVAL_TOKEN)
            rr = runner.run(case)
            vs.append(rr.test_verdict)
            steps_total = len(rr.steps)
            steps_ok = sum(1 for s in rr.steps if s.ok)
            if steps_total:
                locator_ok.append(steps_ok / steps_total)
            if rr.assertion_results:
                assertion_ok.append(
                    sum(1 for a in rr.assertion_results if a.get("verdict") == "PASSED")
                    / len(rr.assertion_results)
                )
            print(
                f"  trackA {case_name} run{i}: {rr.test_verdict} ({steps_ok}/{steps_total} steps)"
            )
        verdicts[case_name] = vs

    stable = sum(1 for v in verdicts.values() if len(v) == 2 and v[0] == v[1])
    n_cases = len(verdicts)
    metrics = {
        "replay_stability": round(stable / n_cases, 4) if n_cases else None,
        "locator_validity": round(sum(locator_ok) / len(locator_ok), 4) if locator_ok else None,
        "assertion_correctness": (
            round(sum(assertion_ok) / len(assertion_ok), 4) if assertion_ok else None
        ),
    }
    from benchmark.engine import ExperimentResult

    exp._results[f"genA-manual-{RUN_AT}"] = ExperimentResult(
        f"genA-manual-{RUN_AT}",
        "test_generation",
        "approved_manual_cases_replay",
        "d-trusted-r4",
        n_cases,
        metrics,
        [{"case": k, "verdicts": v} for k, v in verdicts.items()],
        {**RUNTIME_META, "dataset_version": "gt-unresolved", "at": _now()},
        note="scripted_explorer_chain=NOT_RUN (bounded R4 budget); manual cases real-replayed",
    )
    print(f"  track A metrics: {metrics}")


def track_c(real_sandbox_outcome: dict[str, Any] | None, exp: ExperimentManager) -> None:
    """Self-healing: real protected sandbox validation + honest NOT_RUNs."""
    if real_sandbox_outcome is not None:
        exp.run_self_healing(
            f"heal-protected-{RUN_AT}",
            "d-trusted-r4",
            "protected_repair_r3_sandbox",
            lambda s: real_sandbox_outcome,
            metadata={**RUNTIME_META, "operator": "agent", "promotion": "none (human gate)"},
        )
    else:
        exp.not_run(
            f"heal-protected-{RUN_AT}",
            "self_healing",
            "protected_repair_r3_sandbox",
            "sandbox run failed; see logs",
        )
    exp.not_run(
        f"heal-manual-{RUN_AT}",
        "self_healing",
        "manual_baseline",
        "NOT_RUN: no human operator available in an autonomous run; not fabricated",
    )
    exp.not_run(
        f"heal-unprotected-llm-{RUN_AT}",
        "self_healing",
        "unprotected_llm_repair",
        "NOT_RUN: unprotected LLM repair is only allowed in isolation and stayed "
        "outside the approved R4 budget",
    )


def real_sandbox_validation() -> dict[str, Any] | None:
    """One real RepairSandbox run (real Chrome, isolated TeamFlow subprocess)."""
    from packages.execution.sandbox import RepairSandbox

    base = load_case(CASES / "TC-CREATE-001.json")
    drifted = dataclasses.replace(
        base,
        version=1,
        steps=tuple(
            dataclasses.replace(s, target="[data-testid=legacy-new-project-link]")
            if s.step_id == "s6"
            else s
            for s in base.steps
        ),
    )
    candidate = dataclasses.replace(base, version=2)
    captured: list[dict[str, Any]] = []
    sandbox = RepairSandbox(
        proposal_id=f"rp_bench_{RUN_AT}",
        base_case=drifted,
        candidate=candidate,
        on_evidence=captured.append,
    )
    result = sandbox.run()
    print(
        f"  sandbox: verdict={result.verdict} delivery_err={result.evidence_delivery_error!r} "
        f"cleanup_err={result.cleanup_error!r}"
    )
    return {
        "assertion_preserved": result.verdict in ("VALIDATED", "NO_BEHAVIORAL_CHANGE"),
        "sandbox_passed": result.verdict == "VALIDATED",
        "promoted": False,  # human gate — never auto-promoted
        "sandbox_verdict": result.verdict,
        "reason": result.reason,
        "evidence_sha256": result.evidence_sha256,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ------------------------------------------------------------------------- main


def main() -> int:
    print("== R4 UI-FaultBench runner ==")
    BENCH_DB.parent.mkdir(parents=True, exist_ok=True)
    if BENCH_DB.exists():
        BENCH_DB.unlink()

    start_teamflow()

    # --- GT + dataset (primary_cause stays UNRESOLVED pending approval) ---
    from scripts.seed_scenarios import SCENARIOS

    gt = GroundTruthManager()
    records = [
        GroundTruthRecord(
            scenario_id=sid,
            expected_verdict=str(gt_cfg.get("expected_verdict", "")),
            primary_cause="UNRESOLVED",
            fault_class="UNRESOLVED",
            dataset_version="gt-v4-r4-unresolved",
        )
        for sid, _f, _d, gt_cfg in SCENARIOS
    ]
    for r in records:
        gt.add(r)
    gt_version = f"gt-v4-r4-unresolved-{RUN_AT}"
    # Run-scoped GT version so dataset versions (and therefore persisted file
    # names) are unique per run — append-only store refuses duplicates.
    gt._version = gt_version  # noqa: SLF001 - trusted runner context

    datasets = DatasetManager(gt)
    ds = datasets.create(
        "d-trusted-r4",
        DatasetPartition.TRUSTED,
        tuple(r.scenario_id for r in records),
        metadata={"source": "scripts/seed_scenarios.py SCENARIOS (authoritative expected_verdict)"},
    )
    experiments = ExperimentManager(datasets, gt)

    store = BenchmarkStore(ROOT / "benchmark" / "data")
    gt_path = store.save_gt_version(records, gt_version)
    ds_path = store.save_dataset(
        ds,
        manifest={
            "scenario_ids": list(ds.scenario_ids),
            "partition": ds.partition,
            "source": "eval_ground_truth seed (see R4-GT-Mapping-Proposal.md)",
            "primary_cause_status": "UNRESOLVED pending approval",
        },
    )
    print(f"GT persisted: {gt_path.name}; dataset persisted: {ds_path.name}")

    # --- Track B: real scenario runs ---
    print("Track B: running 6 scenarios with real Chrome...")
    scenarios = run_all_scenarios()

    def pred_A(sid: str) -> dict[str, Any]:
        info = scenarios.get(sid, {})
        if not info.get("completed"):
            return {"test_verdict": "INCONCLUSIVE", "primary_cause": "INCONCLUSIVE"}
        return {
            "test_verdict": info["verdict"],
            "primary_cause": "INCONCLUSIVE",  # raw report carries no cause
        }

    def pred_C(sid: str) -> dict[str, Any]:
        info = scenarios.get(sid, {})
        if not info.get("completed"):
            return {"test_verdict": "INCONCLUSIVE", "primary_cause": "INCONCLUSIVE"}
        d = info["diag_c"]
        return {
            "test_verdict": info["verdict"],
            "primary_cause": d.get("failure_mechanism", "INCONCLUSIVE"),
            "probe_count": 0,
        }

    def pred_D(sid: str) -> dict[str, Any]:
        info = scenarios.get(sid, {})
        if not info.get("completed"):
            return {"test_verdict": "INCONCLUSIVE", "primary_cause": "INCONCLUSIVE"}
        d = info["diag_d"]
        return {
            "test_verdict": info["verdict"],
            "primary_cause": d.get("failure_mechanism", "INCONCLUSIVE"),
            "probe_count": 0,
        }

    experiments.run_diagnosis(
        f"diagA-rawreport-{RUN_AT}",
        ds.dataset_id,
        "raw_report_verdict",
        pred_A,
        metadata={**RUNTIME_META, "dataset_version": gt_version},
    )
    experiments.run_diagnosis(
        f"diagC-rules-{RUN_AT}",
        ds.dataset_id,
        "rule_diagnosis_no_eval_evidence",
        pred_C,
        metadata={**RUNTIME_META, "dataset_version": gt_version},
    )
    experiments.run_diagnosis(
        f"diagD-graph-{RUN_AT}",
        ds.dataset_id,
        "rule_diagnosis_with_eval_evidence",
        pred_D,
        metadata={**RUNTIME_META, "dataset_version": gt_version},
    )

    print("Track B method B: real DeepSeek calls...")
    llm_preds = llm_predict_texts(scenarios)
    if llm_preds is None:
        experiments.not_run(
            f"diagB-llm-{RUN_AT}",
            "diagnosis",
            "single_llm_deepseek",
            "NOT_RUN: no LLM provider configured in this environment",
        )
    else:
        experiments.run_diagnosis(
            f"diagB-llm-{RUN_AT}",
            ds.dataset_id,
            "single_llm_deepseek",
            lambda s: llm_preds.get(
                s, {"test_verdict": "INCONCLUSIVE", "primary_cause": "INCONCLUSIVE"}
            ),
            metadata={**RUNTIME_META, "dataset_version": gt_version},
        )

    # --- Ablations (isolated: prediction-function variants, no formal chain) ---
    ablation = AblationRunner(experiments)
    base_id = f"diagD-graph-{RUN_AT}"
    ablation.run(base_id, "evidence_graph", ds.dataset_id, pred_C)  # D minus eval evidence == C
    ablation.run(base_id, "active_diagnosis", ds.dataset_id, pred_C)

    # --- Track A ---
    print("Track A: manual-case replays...")
    track_a(store, experiments)

    # --- Track C: one real sandbox validation ---
    print("Track C: real RepairSandbox validation (real Chrome)...")
    outcome = real_sandbox_validation()
    track_c(outcome, experiments)

    # --- persist every experiment ---
    for res in experiments.all():
        try:
            path = store.save_experiment(res)
            print(f"persisted: {path.name} [{res.status}]")
        except Exception as exc:  # noqa: BLE001
            print(f"persist failed for {res.experiment_id}: {exc}")

    print("\n== summary ==")
    for res in experiments.all():
        acc = res.metrics.get("primary_cause_accuracy", "n/a")
        print(
            f"{res.experiment_id:34} {res.track:16} {res.status:10} "
            f"primary_cause_acc={acc} false_pass={res.metrics.get('false_pass_rate', 'n/a')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
