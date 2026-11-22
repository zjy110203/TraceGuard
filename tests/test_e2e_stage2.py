"""Stage-2 end-to-end: real Playwright (system Chrome) drives TeamFlow.

Proves the minimal closed loop:
  TestCaseVersion -> browser steps -> auto-observations (independent API + UI)
  -> Oracle evaluation -> frozen Verdict -> Run/Step report with evidence.

Three scenarios exercise the key diagnostic property:
  * healthy: project created -> PASSED
  * TF-UI-001: UI hides success but DB has the project -> PASSED verdict,
    but a step assertion fails and the divergence is recorded for diagnosis.
    (UI lying must NOT flip a correct "test meets expectation" verdict.)
  * TF-DB-001: DB fault, no project -> FAILED verdict.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator

import httpx
import pytest
import uvicorn

from packages.execution.models import (
    AssertionSpec,
    OracleBinding,
    OracleSpec,
    TestCaseVersion,
    TestIntent,
    TestStep,
)
from packages.execution.runner import LocalRunner
from packages.execution.verdict import OracleVerdict

for _model in (TestCaseVersion, TestIntent, TestStep):
    _model.__test__ = False

BASE = "http://127.0.0.1:8031"
EVAL_H = {"X-Eval-Token": "local-dev-eval-token-change-me"}


@pytest.fixture(scope="module")
def teamflow_server() -> Generator[None, None, None]:
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=8031, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        pytest.fail("TeamFlow server did not become healthy")

    # Ensure a clean schema exists (server startup seeds it; be defensive).
    yield
    server.should_exit = True
    thread.join(timeout=10)


def _case(name: str = "E2E Proj") -> TestCaseVersion:
    return TestCaseVersion(
        case_id="TC-E2E-001",
        version=1,
        title="login and create project",
        intent=TestIntent("i1", "verify a project can be created and persisted"),
        steps=(
            TestStep("s1", "navigate", "/"),
            TestStep("s2", "fill", "[data-testid=username]", value="tester"),
            TestStep("s3", "fill", "[data-testid=password]", value="test-pass-123"),
            TestStep("s4", "click", "[data-testid=login-submit]"),
            TestStep("s5", "assert_visible", "[data-testid=new-project-link]"),
            TestStep("s6", "click", "[data-testid=new-project-link]"),
            TestStep("s7", "fill", "[data-testid=project-name]", value=name),
            TestStep("s8", "click", "[data-testid=create-submit]"),
            TestStep("s9", "assert_visible", "[data-testid=success-message]"),
        ),
        assertions=(AssertionSpec("a1", "project persisted in independent state", required=True),),
        oracle_bindings=(
            OracleBinding(
                "a1",
                primary=OracleSpec(
                    "o1",
                    "independent_api",
                    "state.project_count",
                    expected=OracleVerdict.PASSED,
                    expected_value=1,
                    source="manual test spec v1",
                ),
            ),
        ),
    )


def _reset(client: httpx.Client) -> None:
    client.post("/__eval__/faults/clear", headers=EVAL_H)
    client.post("/__eval__/data/reset", headers=EVAL_H)


def test_e2e_healthy_path_passes(teamflow_server: None) -> None:
    with httpx.Client(base_url=BASE, timeout=10) as client:
        _reset(client)
    report = LocalRunner(BASE).run(_case())
    assert all(s.ok for s in report.steps)
    assert report.execution_status.value == "COMPLETED"
    assert report.test_verdict == "PASSED"
    assert report.observations["independent_state"]["project_count"] == 1
    assert len(report.evidence) >= 10
    assert any(e["event_type"] == "run.started" for e in report.events)


def test_e2e_db_fault_fails(teamflow_server: None) -> None:
    with httpx.Client(base_url=BASE, timeout=10) as client:
        _reset(client)
        client.post("/__eval__/faults/TF-DB-001/enable", headers=EVAL_H)
    report = LocalRunner(BASE).run(_case("Should Not Persist"))
    assert report.test_verdict == "FAILED"
    assert report.observations["independent_state"]["project_count"] == 0
    assert report.execution_status.value == "COMPLETED"  # dimensions independent
    with httpx.Client(base_url=BASE, timeout=10) as client:
        client.post("/__eval__/faults/clear", headers=EVAL_H)


def test_e2e_ui_fault_records_divergence_without_false_failure(
    teamflow_server: None,
) -> None:
    """UI hides success (step s9 fails) but DB has the project: verdict stays
    PASSED, while UI divergence is captured for the stage-7 diagnosis."""
    with httpx.Client(base_url=BASE, timeout=10) as client:
        _reset(client)
        client.post("/__eval__/faults/TF-UI-001/enable", headers=EVAL_H)
    report = LocalRunner(BASE).run(_case("UI Fault Proj"))
    assert report.test_verdict == "PASSED"
    assert report.observations["independent_state"]["project_count"] == 1
    s9 = next(s for s in report.steps if s.step_id == "s9")
    assert s9.ok is False  # UI lie is observed, not hidden
    with httpx.Client(base_url=BASE, timeout=10) as client:
        client.post("/__eval__/faults/clear", headers=EVAL_H)


def test_history_runs_not_overwritten(teamflow_server: None, tmp_path) -> None:
    """Two runs produce two immutable report files, bound to the same version."""
    runner = LocalRunner(BASE, report_dir=tmp_path)
    with httpx.Client(base_url=BASE, timeout=10) as client:
        _reset(client)
    r1 = runner.run(_case("First"))
    r2 = runner.run(_case("Second"))
    assert r1.run_id != r2.run_id
    assert r1.case_version == r2.case_version == 1
    assert len(list(tmp_path.glob("run_*.json"))) == 2
