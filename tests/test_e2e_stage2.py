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

import socket
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

BASE = "http://127.0.0.1:8031"  # rebound to a free port by the fixture below
EVAL_H = {"X-Eval-Token": "local-dev-eval-token-change-me"}


def _free_port() -> int:
    """Ask the OS for an unused loopback port (avoids hardcoded-port clashes)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def teamflow_server(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Start TeamFlow on a free port against an isolated temp DB.

    The app is built explicitly here. It deliberately does NOT reuse the
    module-level ``apps.teamflow.main.app`` that conftest's import creates,
    because that instance is bound to the production ``data/traceguard.db``.
    Building our own keeps this E2E off real data, and the server is stopped in
    ``finally`` so a failed health check can never leave the port wedged.
    """
    global BASE

    from apps.teamflow.main import build_app
    from packages.core.config import Settings

    db_path = tmp_path_factory.mktemp("e2e-stage2") / "teamflow.db"
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{db_path.as_posix()}",
        teamflow_test_user="tester",
        teamflow_test_password="test-pass-123",
        eval_control_token="local-dev-eval-token-change-me",
    )
    app = build_app(settings)
    # A free port is only free until we bind it, and the full suite starts other
    # servers on random ports concurrently. Three attempts turn that race from a
    # spurious fixture error into a retry; the real failure detail is kept so a
    # genuine startup problem is still visible.
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    healthy = False
    last_error = "no HTTP response"
    for _attempt in range(3):
        port = _free_port()
        BASE = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/health", timeout=1, trust_env=False).status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
        if healthy:
            break
        server.should_exit = True
        thread.join(timeout=10)

    if not healthy:
        pytest.fail(f"TeamFlow server did not become healthy after 3 ports ({last_error})")
    try:
        yield
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
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
    """Clear faults and wipe data, asserting both really happened.

    A silently ignored 401/500 here used to leave the previous test's projects
    in place, so a later ``project_count == 0`` assertion failed for a reason
    the test could not report. The status codes are part of the fixture now.
    """
    cleared = client.post("/__eval__/faults/clear", headers=EVAL_H)
    assert cleared.status_code == 200, f"faults/clear -> {cleared.status_code}"
    reset = client.post("/__eval__/data/reset", headers=EVAL_H)
    assert reset.status_code == 200, f"data/reset -> {reset.status_code}"
    assert "projects_deleted" in reset.json(), reset.text[:200]


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
