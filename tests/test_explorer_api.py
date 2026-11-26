"""Explorer REST surface: policy honesty, safety gates and trace redaction.

These tests deliberately avoid launching a browser. The guarded execution loop
itself is covered by ``tests/test_llm_exploration.py``; here we pin the contract
the UI relies on:

* the LLM policy is never advertised as available;
* non-local targets are rejected before any browser starts;
* credential-shaped tool arguments are masked before a trace can be read.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.explorer_api import (
    REDACTED,
    _cleanup_scenario_data,
    _prepare_scenario_data,
    recover_expired_scenario_data,
    redact_actions,
)
from apps.api.main import build_app
from apps.teamflow import service as teamflow_service
from apps.teamflow.models import Base as TeamFlowBase
from apps.teamflow.models import Project
from packages.agents.scenarios import default_registry
from packages.core import database
from packages.core.config import Settings


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        exploration_data_dir=str(tmp_path / "explorations"),
        benchmark_data_dir=str(tmp_path / "benchmark"),
    )
    app = build_app(settings)
    with TestClient(app) as c:
        yield c


def test_policies_state_llm_is_not_available(client: TestClient) -> None:
    body = client.get("/api/v1/explorations/policies").json()
    by_id = {p["id"]: p for p in body["policies"]}

    assert by_id["scripted"]["verified"] is True
    assert by_id["scripted"]["is_llm"] is False
    # The LLM policy is declared but explicitly unverified: no fake capability.
    assert by_id["llm"]["is_llm"] is True
    assert by_id["llm"]["verified"] is False
    assert body["active_policy"] == "scripted"
    assert body["llm_blocked_reason"]


def test_scripted_agent_policy_builder_returns_the_declared_policy() -> None:
    from apps.api.explorer_api import build_policy
    from packages.agents.scripted_agent import ScriptedAgentPolicy

    settings = Settings(app_env="test")
    scenario = default_registry().require("teamflow-login")
    policy, version = build_policy(
        "scripted-agent",
        settings,
        "http://127.0.0.1:8001",
        scenario,
        run_id="run-policy-test",
        test_intent={"goal": "reach project list", "target": "/projects"},
    )
    assert isinstance(policy, ScriptedAgentPolicy)
    assert version == "scripted-agent-1.0.0-NOT-REAL-INFERENCE"


def test_llm_policy_is_refused_with_conflict(client: TestClient) -> None:
    resp = client.post("/api/v1/explorations", json={"policy": "llm"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize(
    "url", ["http://example.com/", "https://evil.test/x", "file:///etc/passwd"]
)
def test_non_local_target_is_rejected(client: TestClient, url: str) -> None:
    resp = client.post(
        "/api/v1/explorations",
        json={"policy": "scripted", "base_url": url},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_step_budget_is_validated(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/explorations",
        json={"policy": "scripted", "base_url": "http://127.0.0.1:8001", "max_steps": 999},
    )
    assert resp.status_code == 422


def test_list_is_empty_before_any_run(client: TestClient) -> None:
    assert client.get("/api/v1/explorations").json() == []


def test_unknown_exploration_is_404(client: TestClient) -> None:
    resp = client.get("/api/v1/explorations/exp_deadbeef")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_path_like_id_cannot_escape_the_store(client: TestClient) -> None:
    # A traversal-shaped id must not read outside the exploration directory.
    assert client.get("/api/v1/explorations/exp_..%2F..%2Fsecret").status_code == 404


def test_scenario_digest_tampering_is_reported_on_read(client: TestClient) -> None:
    store = client.app.state.exploration_store
    store.save(
        {
            "exploration_id": "exp_scenario_digest_tamper",
            "scenario_id": "teamflow-login",
            "scenario_version": "1.0.0",
            "scenario_digest": "0" * 64,
            "digest_algorithm": "sha256",
            "digest_algorithm_version": "canonical-json-v1",
            "action_sequence": [],
            "observations": [],
        }
    )

    response = client.get("/api/v1/explorations/exp_scenario_digest_tamper")

    assert response.status_code == 200
    assert response.json()["scenario_digest_verified"] is False


def test_unknown_scenario_digest_algorithm_is_rejected(client: TestClient) -> None:
    store = client.app.state.exploration_store
    store.save(
        {
            "exploration_id": "exp_unknown_scenario_digest",
            "scenario_id": "teamflow-login",
            "scenario_version": "1.0.0",
            "scenario_digest": "0" * 64,
            "digest_algorithm": "md5",
            "digest_algorithm_version": "legacy",
            "action_sequence": [],
            "observations": [],
        }
    )

    response = client.get("/api/v1/explorations/exp_unknown_scenario_digest")

    assert response.status_code == 422
    assert response.json()["error"]["details"]["code"] == "unsupported-digest-algorithm"


def test_run_data_seed_cleanup_isolated_to_own_run(client: TestClient) -> None:
    TeamFlowBase.metadata.create_all(client.app.state.engine)
    session_factory = database.make_session_factory(client.app.state.engine)
    with session_factory() as session:
        teamflow_service.seed_default(session, "tester", "test-pass-123")
    manager = client.app.state.data_lease_manager
    first = manager.setup("exp_run_one")
    second = manager.setup("exp_run_two")
    request = type("RequestStub", (), {"app": client.app})()
    scenario = default_registry().require("project-edit", "2.0.0")

    first_values = _prepare_scenario_data(request, scenario, "exp_run_one", first)
    second_values = _prepare_scenario_data(request, scenario, "exp_run_two", second)
    with session_factory() as session:
        assert session.query(Project).count() == 2
    _cleanup_scenario_data(request, "exp_run_one", first)
    assert manager.cleanup("exp_run_one", first)
    with session_factory() as session:
        projects = session.query(Project).all()
        assert len(projects) == 1
        assert projects[0].name == second_values["project_target"]
    _cleanup_scenario_data(request, "exp_run_two", second)
    assert manager.cleanup("exp_run_two", second)
    assert first_values["project_target_id"] != second_values["project_target_id"]


def test_run_data_cleanup_refuses_modified_project_identity(client: TestClient) -> None:
    TeamFlowBase.metadata.create_all(client.app.state.engine)
    session_factory = database.make_session_factory(client.app.state.engine)
    with session_factory() as session:
        teamflow_service.seed_default(session, "tester", "test-pass-123")
    manager = client.app.state.data_lease_manager
    token = manager.setup("exp_identity_guard")
    request = type("RequestStub", (), {"app": client.app})()
    scenario = default_registry().require("project-edit", "2.0.0")
    values = _prepare_scenario_data(request, scenario, "exp_identity_guard", token)
    with session_factory() as session:
        project = session.get(Project, values["project_target_id"])
        assert project is not None
        project.name = "unowned-project"
        session.commit()

    with pytest.raises(RuntimeError, match="refusing cleanup"):
        _cleanup_scenario_data(request, "exp_identity_guard", token)
    with session_factory() as session:
        assert session.get(Project, values["project_target_id"]) is not None


def test_startup_recovery_cleans_stale_run_data_only(client: TestClient) -> None:
    import time

    TeamFlowBase.metadata.create_all(client.app.state.engine)
    session_factory = database.make_session_factory(client.app.state.engine)
    with session_factory() as session:
        teamflow_service.seed_default(session, "tester", "test-pass-123")
    manager = client.app.state.data_lease_manager
    stale_token = manager.setup("exp_stale_owner", now=time.time() - 61)
    active_token = manager.setup("exp_active_owner")
    request = type("RequestStub", (), {"app": client.app})()
    scenario = default_registry().require("project-edit", "2.0.0")
    stale_values = _prepare_scenario_data(request, scenario, "exp_stale_owner", stale_token)
    active_values = _prepare_scenario_data(request, scenario, "exp_active_owner", active_token)

    recover_expired_scenario_data(client.app)

    with session_factory() as session:
        assert session.get(Project, stale_values["project_target_id"]) is None
        assert session.get(Project, active_values["project_target_id"]) is not None
    assert not (client.app.state.data_lease_manager.root / "exp_stale_owner").exists()
    assert (client.app.state.data_lease_manager.root / "exp_active_owner").exists()


def test_startup_recovery_retries_database_outage_without_losing_lease(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from apps.api import explorer_api

    TeamFlowBase.metadata.create_all(client.app.state.engine)
    session_factory = database.make_session_factory(client.app.state.engine)
    with session_factory() as session:
        teamflow_service.seed_default(session, "tester", "test-pass-123")
    manager = client.app.state.data_lease_manager
    token = manager.setup("exp_db_retry", now=time.time() - 61)
    request = type("RequestStub", (), {"app": client.app})()
    scenario = default_registry().require("project-edit", "2.0.0")
    values = _prepare_scenario_data(request, scenario, "exp_db_retry", token)

    original = explorer_api._cleanup_app_scenario_data
    monkeypatch.setattr(
        explorer_api,
        "_cleanup_app_scenario_data",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    recover_expired_scenario_data(client.app)
    lease = manager.lease("exp_db_retry")
    assert lease["state"] == "CLEANUP_FAILED"
    assert (manager.root / "exp_db_retry" / "lease.json").exists()
    with session_factory() as session:
        assert session.get(Project, values["project_target_id"]) is not None

    monkeypatch.setattr(explorer_api, "_cleanup_app_scenario_data", original)
    recover_expired_scenario_data(client.app)
    with session_factory() as session:
        assert session.get(Project, values["project_target_id"]) is None
    assert not (manager.root / "exp_db_retry").exists()


def test_setup_failure_retains_lease_when_database_cleanup_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.api import explorer_api

    manager = client.app.state.data_lease_manager

    def fail_setup(request, scenario, run_id, token):
        manager.add_resource(run_id, token, {"kind": "teamflow-project", "id": "p-1"})
        raise RuntimeError("setup failed")

    def fail_cleanup(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(explorer_api, "_prepare_scenario_data", fail_setup)
    monkeypatch.setattr(explorer_api, "_cleanup_scenario_data", fail_cleanup)
    with pytest.raises(RuntimeError, match="setup failed"):
        client.post("/api/v1/explorations", json={"policy": "scripted"})
    (run_id,) = manager.expired_runs()
    lease = manager.lease(run_id)
    assert lease["state"] == "CLEANUP_FAILED"
    assert lease["resources"][0]["id"] == "p-1"


def test_redact_actions_masks_credentials_only() -> None:
    actions = [
        {"tool": "fill", "args": {"selector": "[data-testid=password]", "value": "hunter2"}},
        {"tool": "fill", "args": {"selector": "[data-testid=username]", "value": "tester"}},
        {"tool": "navigate", "args": {"url": "http://127.0.0.1:8001/"}},
        {"tool": "fill", "args": {"selector": "#x", "value": "abc", "token": "t-123"}},
    ]
    clean, masked = redact_actions(actions)

    assert masked == 2
    # The password field's *value* key is sensitive -> masked.
    assert clean[0]["args"]["value"] == REDACTED
    # Non-sensitive args survive untouched, so the trace stays useful.
    assert clean[1]["args"]["value"] == "tester"
    assert clean[2]["args"]["url"] == "http://127.0.0.1:8001/"
    assert clean[3]["args"]["token"] == REDACTED
    assert clean[3]["args"]["selector"] == "#x"


def test_redact_actions_is_idempotent() -> None:
    actions = [{"tool": "fill", "args": {"password": "x"}}]
    once, n1 = redact_actions(actions)
    twice, n2 = redact_actions(once)
    assert n1 == 1
    assert twice[0]["args"]["password"] == REDACTED
    # Re-redacting an already-masked value does not double-count it.
    assert n2 == 1
