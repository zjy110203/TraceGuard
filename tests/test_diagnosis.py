"""Stage-7 acceptance: deterministic multi-dimensional diagnosis.

Verifies the v1.1 §阶段7 rules without an LLM:
  * eight dimensions independent; Domain != Mechanism enum;
  * CONFIRMED only via named deterministic rules + authoritative evidence;
  * multiple hypotheses, two-sided evidence, no auto-root-cause;
  * evidence insufficiency -> INCONCLUSIVE -> NEEDS_MORE_EVIDENCE;
  * Flaky is a stability characteristic requiring repeated divergence;
  * Disposition routed separately;
  * multiple confirmed root causes preserved;
  * runs built from REAL run reports produced by LocalRunner.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from packages.agents.diagnosis import (
    DISPOSITIONS,
    FAILURE_DOMAINS,
    FAILURE_MECHANISMS,
    DiagnosisService,
    EvidenceRef,
    Hypothesis,
)
from packages.core.errors import ValidationError
from packages.execution.models import load_case
from packages.execution.runner import LocalRunner
from tests.netutil import release_reserved, reserve_port, take_reserved

TF_PORT = reserve_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"
EVAL_H = {"X-Eval-Token": "local-dev-eval-token-change-me"}


@pytest.mark.parametrize(
    "count, evidence_run, expected",
    [(0, "r", "SUPPORTED"), (1, "r", "CANDIDATE"), (0, "other", "CANDIDATE")],
)
def test_observed_missing_locator_support_is_not_root_cause(count, evidence_run, expected):
    diagnosis = DiagnosisService().diagnose(
        "d",
        "r",
        {
            "test_verdict": "FAILED",
            "failed_step_ids": ["s"],
            "evidence": [
                {
                    "evidence_id": "e",
                    "evidence_type": "locator_failure",
                    "run_id": evidence_run,
                    "step_id": "s",
                    "source_trust": "observed",
                    "payload": {"match_count": count, "error_type": "TimeoutError"},
                }
            ],
        },
    )
    assert diagnosis.hypotheses[0].status == expected
    assert diagnosis.root_causes == ()


@pytest.fixture(scope="module")
def teamflow_server() -> None:
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=TF_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, kwargs={"sockets": take_reserved(TF_PORT)}, daemon=True)
    t.start()
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("TeamFlow not healthy")
        yield
    finally:
        server.should_exit = True
        t.join(timeout=15)
        assert not t.is_alive(), "server thread did not stop"
        release_reserved(TF_PORT)


def _fault_log(client: httpx.Client) -> list[dict[str, Any]]:
    data = client.get("/__eval__/events", headers=EVAL_H).json()
    return data.get("events", data) if isinstance(data, dict) else list(data)


def _bundle_from_real_run(fault_id: str | None = None) -> dict[str, Any]:
    case = load_case(Path("benchmark/cases/TC-CREATE-001.json"))
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        c.post("/__eval__/faults/clear", headers=EVAL_H)
        c.post("/__eval__/data/reset", headers=EVAL_H)
        if fault_id:
            c.post(f"/__eval__/faults/{fault_id}/enable", headers=EVAL_H)
    report = LocalRunner(TF_BASE).run(case)
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        fault_events = _fault_log(c)
        c.post("/__eval__/faults/clear", headers=EVAL_H)
    return {
        "test_verdict": report.test_verdict,
        "execution_status": report.execution_status.value,
        "failed_step_ids": [s.step_id for s in report.steps if not s.ok],
        "assertions": report.assertion_results,
        "evidence": report.evidence,
        "events": report.events,
        "observations": report.observations,
        "fault_events": fault_events,
    }


def test_enums_and_dimensions_independent() -> None:
    assert "PRODUCT" in FAILURE_DOMAINS and "PERSISTENCE" in FAILURE_MECHANISMS
    # Two distinct enums; only UNKNOWN is shared as an explicit fallback value.
    assert FAILURE_DOMAINS & FAILURE_MECHANISMS == {"UNKNOWN"}
    assert "API" in FAILURE_MECHANISMS and "API" not in FAILURE_DOMAINS
    assert "PRODUCT_DEFECT" in DISPOSITIONS
    h = Hypothesis("h1", "x", "PRODUCT", "UI_RENDERING")
    assert h.failure_domain == "PRODUCT" and h.failure_mechanism == "UI_RENDERING"
    with pytest.raises(ValidationError):
        Hypothesis("bad", "x", "NOT_A_DOMAIN", "API")


def test_evidence_ref_role_validation() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef("x", "y", "invalid_role")


def test_healthy_run_is_confirmed_no_disposition_action() -> None:
    d = DiagnosisService().diagnose("dx-ok", "run-ok", {"test_verdict": "PASSED"})
    assert d.diagnosis_status == "CONFIRMED"
    assert d.root_causes == ()
    assert d.flaky_characteristic == "NOT_DETERMINED"


def test_db_fault_confirmed_persistence_product_defect(teamflow_server: None) -> None:
    bundle = _bundle_from_real_run("TF-DB-001")
    assert bundle["test_verdict"] == "FAILED"
    d = DiagnosisService().diagnose("dx-db", "run-db", bundle)
    assert d.diagnosis_status == "CONFIRMED"
    assert d.failure_domain == "PRODUCT"
    assert d.failure_mechanism == "PERSISTENCE"
    assert d.root_causes and "persistence" in d.root_causes[0]
    assert d.disposition == "PRODUCT_DEFECT"
    h = next(h for h in d.hypotheses if h.hypothesis_id == "h-persistence")
    assert h.status == "CONFIRMED" and h.supporting


def test_api_fault_confirmed_api_mechanism(teamflow_server: None) -> None:
    bundle = _bundle_from_real_run("TF-API-001")
    d = DiagnosisService().diagnose("dx-api", "run-api", bundle)
    assert d.failure_mechanism == "API"
    assert d.diagnosis_status == "CONFIRMED"


def test_ui_fault_has_two_sided_evidence_and_not_confirmed(teamflow_server: None) -> None:
    bundle = _bundle_from_real_run("TF-UI-001")
    # UI fault with intact independent state -> verdict PASSED; diagnosis still
    # records the divergence hypothesis with support + contradiction.
    d = DiagnosisService().diagnose("dx-ui", "run-ui", bundle)
    h = next(h for h in d.hypotheses if h.hypothesis_id == "h-ui")
    assert h.supporting and h.contradicting
    assert h.status == "SUPPORTED"  # contradiction prevents CONFIRMED


def test_insufficient_evidence_is_inconclusive() -> None:
    bundle = {
        "test_verdict": "FAILED",
        "failed_step_ids": ["s9"],
        "assertions": [{"assertion_id": "a1", "verdict": "FAILED"}],
        "evidence": [],
        "fault_events": [],
        "observations": {},
    }
    d = DiagnosisService().diagnose("dx-unk", "run-unk", bundle)
    assert d.diagnosis_status == "INCONCLUSIVE"
    assert d.disposition == "NEEDS_MORE_EVIDENCE"
    h = d.hypotheses[0]
    assert h.status != "CONFIRMED"
    assert d.root_causes == ()  # unconfirmed hypothesis never becomes root cause


def test_flaky_requires_repeated_divergence_not_single_timeout() -> None:
    service = DiagnosisService()
    base = {"test_verdict": "FAILED", "fault_events": [{"fault_id": "TF-DB-001", "id": 1}]}
    one = service.diagnose("dx-f1", "r1", {**base, "repeated_verdicts": ["FAILED"]})
    assert one.flaky_characteristic == "NOT_DETERMINED"
    diverge = service.diagnose(
        "dx-f2", "r2", {**base, "repeated_verdicts": ["PASSED", "FAILED", "PASSED"]}
    )
    assert diverge.flaky_characteristic == "REPEATED_DIVERGENCE_OBSERVED"
    assert diverge.disposition == "TEST_EXECUTION_ISSUE"
    assert "flaky" not in " ".join(diverge.root_causes).lower()  # flaky never a root cause


def test_multiple_confirmed_root_causes_preserved() -> None:
    bundle = {
        "test_verdict": "FAILED",
        "fault_events": [
            {"fault_id": "TF-DB-001", "id": 1, "outcome": "TX_ROLLBACK"},
            {"fault_id": "TF-API-001", "id": 2, "outcome": "API_500"},
        ],
    }
    d = DiagnosisService().diagnose("dx-multi", "run-multi", bundle)
    assert d.diagnosis_status == "CONFIRMED"
    assert len(d.root_causes) >= 2  # not compressed into one
    assert any("additional confirmed hypothesis" in f for f in d.contributing_factors)


def test_bookkeeping_events_are_not_divergence() -> None:
    """CLEARED / business events must not demote a healthy run.

    Regression: the eval EventLog also stores CLEARED and business audit rows, so
    treating any event as divergence pushed healthy runs into the PROBABLE path.
    """
    from packages.agents.diagnosis import is_fault_signal

    assert is_fault_signal({"fault_id": "TF-UI-001", "outcome": "ARMED"}) is True
    assert is_fault_signal({"fault_id": "TF-DB-001", "outcome": "TX_ROLLBACK"}) is True
    assert is_fault_signal({"fault_id": "TF-API-001", "outcome": "API_500"}) is True
    assert is_fault_signal({"fault_id": "TF-DB-001"}) is True  # outcome may be absent
    assert is_fault_signal({"fault_id": "TF-UI-001", "outcome": "CLEARED"}) is False
    assert is_fault_signal({"fault_id": "TF", "outcome": "PROJECT_CREATED"}) is False

    healthy = DiagnosisService().diagnose(
        "dx-noise",
        "run-noise",
        {
            "test_verdict": "PASSED",
            "fault_events": [
                {"fault_id": "TF", "outcome": "PROJECT_CREATED"},
                {"fault_id": "TF-UI-001", "outcome": "CLEARED"},
            ],
        },
    )
    assert healthy.root_causes == ()
    assert healthy.hypotheses[0].hypothesis_id == "h-healthy"
