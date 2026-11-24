"""R5-4.6 §3.6: the scripted-agent decision loop, end to end, in a real browser.

Chain under test (nothing mocked except the model, which is ScriptedProvider
and labelled NOT_REAL_INFERENCE everywhere):

    Observation -> LLMPolicy -> ScriptedProvider -> ToolGateway
    -> PlaywrightAdapter -> Result -> next Observation -> next decision

Covered: normal completion, per-decision observation consumption, illegal tool
rejected, decision/observation drift, budget exhaustion, error recovery, and
the agent-decision audit trail.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from packages.agents.exploration import Budget, StopReason
from packages.agents.explorer import Explorer
from packages.agents.scripted_agent import (
    AgentStep,
    ScenarioDriftError,
    ScriptedAgentPolicy,
    teamflow_login_agent_steps,
)
from tests.netutil import free_port

TF_PORT = free_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"
EVAL = {"X-Eval-Token": "local-dev-eval-token-change-me"}


@pytest.fixture(scope="module")
def teamflow_server() -> Iterator[None]:
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=TF_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        try:
            if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:  # pragma: no cover
        pytest.fail("TeamFlow did not become healthy")
    yield
    server.should_exit = True
    thread.join(timeout=10)


def _policy(goal: str = "log in and reach the project list") -> ScriptedAgentPolicy:
    # Credentials come from the deployment's test-data configuration — the
    # same source the product path uses — never hardcoded in the decision
    # script, and redacted before anything enters the trace.
    from packages.core.config import Settings

    settings = Settings()
    return ScriptedAgentPolicy(
        teamflow_login_agent_steps(
            base_url=f"{TF_BASE}/",
            username=str(settings.teamflow_test_user),
            password=str(settings.teamflow_test_password),
        ),
        goal=goal,
    )


def _budget(**kw: object) -> Budget:
    return Budget(
        max_steps=int(kw.get("max_steps", 10)),
        max_tool_calls=int(kw.get("max_tool_calls", 24)),
        max_duration_s=60,
        max_action_s=10,
        allowed_domains=("127.0.0.1",),
        allowed_origins=(f"http://127.0.0.1:{TF_PORT}",),
    )


def _decision(action: str, rationale: str, **args: str) -> str:
    return json.dumps({"action": action, "args": args, "rationale": rationale})


# ------------------------------------------------------------ normal completion


def test_decision_loop_completes_login_and_records_every_decision(
    teamflow_server: None,
) -> None:
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        c.post("/__eval__/data/reset", headers=EVAL)

    policy = _policy()
    explorer = Explorer(policy=policy, budget=_budget())
    intent = {
        "intent_id": "i-sa-login",
        "goal": "log in and reach the project list",
        "target": "/projects",
        "expected_outcomes": [],
        "required_assertions": [],
    }
    trace = explorer.explore("i-sa-login", TF_BASE, intent=intent)

    assert trace.goal_status == "VERIFIED", trace.goal_reason
    assert trace.stop_reason == StopReason.GOAL_VERIFIED
    # every scripted decision was consumed, validated by the schema and traced
    assert len(policy.call_traces()) == 5
    assert all(t["schema_ok"] for t in policy.call_traces())
    assert all(t["provider"] == "scripted" for t in policy.call_traces())
    # the agent-decision audit trail: rationale + versions + result per step
    executed = [d for d in trace.agent_decisions if d["result"] == "OK"]
    assert len(executed) == 5
    assert all(d["rationale"] for d in executed)
    assert all(d["policy_version"] == "scripted-agent-1.0.0-NOT-REAL-INFERENCE" for d in executed)
    # the version marker is carried into the trace itself
    assert "NOT-REAL-INFERENCE" in trace.agent_version
    # the final observation is the post-login page, consumed by the last decision
    assert trace.observations[-1]["url_path"] == "/projects"


# -------------------------------------------------- observation consumption


def test_each_decision_consumes_the_fresh_observation(teamflow_server: None) -> None:
    """A decision whose expectation names the NEXT page proves the loop re-observes."""
    steps = [
        AgentStep(decision=_decision("navigate", "open login", url=f"{TF_BASE}/")),
        AgentStep(
            decision=_decision("snapshot", "observe login page"),
            expect_url_path="/",
            expect_testid_visible="username",
        ),
    ]
    policy = ScriptedAgentPolicy(steps, goal="observe only")
    trace = Explorer(policy=policy, budget=_budget(max_steps=4, max_tool_calls=8)).explore(
        "i-sa-obs", TF_BASE
    )
    # the second decision carried expect_url_path="/" and passed only because
    # the browser had actually navigated there by the time it was asked.
    assert trace.observations[0]["url_path"] == "/"
    # no intent was declared, so the goal evaluator honestly says so
    assert trace.goal_status == "NOT_EVALUATED"
    assert trace.stop_reason == StopReason.PLAN_EXHAUSTED


# --------------------------------------------------------------- illegal tool


def test_illegal_tool_decision_is_rejected_and_audited(teamflow_server: None) -> None:
    steps = [
        AgentStep(decision=_decision("navigate", "open login", url=f"{TF_BASE}/")),
        AgentStep(decision=_decision("shell", "try to escape", command="rm -rf /")),
    ]
    policy = ScriptedAgentPolicy(steps, goal="pwn")
    trace = Explorer(policy=policy, budget=_budget()).explore("i-sa-shell", TF_BASE)

    # LLMPolicy's schema + whitelist refuse the decision before the gateway
    # must; the audit trail records which decision failed and why.
    error_step = [d for d in trace.agent_decisions if d["result"] == "ERROR"]
    assert error_step and "shell" in error_step[0]["error"]
    assert trace.stop_reason == StopReason.POLICY_STOP
    assert "shell" not in json.dumps(trace.action_sequence)


# ------------------------------------------------------------ scenario drift


def test_decision_drift_stops_the_loop_loudly(teamflow_server: None) -> None:
    """A decision that expects the wrong page must not be applied."""
    steps = [
        AgentStep(
            decision=_decision("snapshot", "wrongly expect the project list"),
            expect_url_path="/projects",  # but we start on about:blank/login
        ),
    ]
    policy = ScriptedAgentPolicy(steps, goal="drift")
    trace = Explorer(policy=policy, budget=_budget()).explore("i-sa-drift", TF_BASE)

    error_step = [d for d in trace.agent_decisions if d["result"] == "ERROR"]
    assert error_step and "scenario drift" in error_step[0]["error"]
    assert trace.stop_reason == StopReason.POLICY_STOP


def test_drift_error_type_is_precise() -> None:
    err = ScenarioDriftError("url_path=/projects", "url_path=/")
    assert "expected" in str(err) and "observed" in str(err)


# ----------------------------------------------------------- budget limits


def test_step_budget_exhausts_before_the_plan_ends(teamflow_server: None) -> None:
    policy = _policy()
    trace = Explorer(policy=policy, budget=_budget(max_steps=1, max_tool_calls=2)).explore(
        "i-sa-budget", TF_BASE
    )
    # The budget stopped the loop — NOT the plan, and NOT a verified goal.
    assert trace.stop_reason == StopReason.STEP_BUDGET
    assert trace.goal_status != "VERIFIED"
    assert len(policy.call_traces()) < 5


# ------------------------------------------------------------ error recovery


def test_failing_action_is_recovered_by_the_next_decision(teamflow_server: None) -> None:
    steps = [
        AgentStep(decision=_decision("navigate", "open login", url=f"{TF_BASE}/")),
        AgentStep(
            decision=_decision(
                "click", "click a nonexistent element", selector="[data-testid=does-not-exist]"
            ),
            expect_url_path="/",
        ),
        AgentStep(
            decision=_decision("snapshot", "recover by observing the same page"),
            expect_url_path="/",
        ),
    ]
    policy = ScriptedAgentPolicy(steps, goal="error recovery")
    trace = Explorer(policy=policy, budget=_budget()).explore("i-sa-recover", TF_BASE)

    failed = [a for a in trace.action_sequence if a.get("error")]
    assert failed and failed[0]["tool"] == "click"
    # the loop kept going: the recovery decision ran afterwards on a fresh obs
    ok_after = [d for d in trace.agent_decisions if d["result"] == "OK"]
    assert ok_after and ok_after[-1]["action"] == "snapshot"
    assert trace.observations[-1]["url_path"] == "/"
