"""A real exploration loop driven by the LLM-shaped policy.

The model here is ``ScriptedProvider`` — it returns canned JSON decisions. What is
NOT faked: the browser, the tool gateway, the budget, the trace, and the compiler
that consumes the trace. This proves the decision *loop* is real and that the loop's
output is usable by the compiler; it does not prove model quality.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from packages.agents.compiler import TestCaseCompiler
from packages.agents.exploration import Budget, StopReason
from packages.agents.explorer import Explorer
from packages.agents.llm import ModelConfig, ScriptedProvider, TracedClient
from packages.agents.llm_policy import LLMPolicy
from packages.execution.models import TestIntent
from tests.netutil import free_port

# A dataclass named Test* is not a pytest test class.
TestIntent.__test__ = False

TF_PORT = free_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"


@pytest.fixture(scope="module")
def teamflow_server() -> Iterator[None]:
    """Dedicated port so this module never races another module's server."""
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


EVAL = {"X-Eval-Token": "local-dev-eval-token-change-me"}

TOOLS = frozenset({"navigate", "fill", "click", "assert_text", "assert_visible", "finish"})


def _decision(action: str, rationale: str, **args: str) -> str:
    import json

    return json.dumps({"action": action, "args": args, "rationale": rationale})


def _policy() -> tuple[LLMPolicy, TracedClient]:
    script = [
        _decision("navigate", "open the login page", url=f"{TF_BASE}/"),
        _decision("fill", "enter the username", selector="[data-testid=username]", value="tester"),
        _decision(
            "fill", "enter the password", selector="[data-testid=password]", value="test-pass-123"
        ),
        _decision("click", "submit the form", selector="[data-testid=login-submit]"),
        _decision("finish", "logged in; goal reached"),
    ]
    client = TracedClient(
        provider=ScriptedProvider(script),
        config=ModelConfig(provider="test", model="scripted-not-a-model", max_retries=1),
    )
    policy = LLMPolicy(client=client, goal="log in as tester", allowed_tools=TOOLS)
    return policy, client


def test_llm_shaped_policy_drives_a_real_exploration(teamflow_server: None) -> None:
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        c.post("/__eval__/data/reset", headers=EVAL)

    policy, client = _policy()
    explorer = Explorer(
        policy=policy,
        budget=Budget(max_steps=12, max_tool_calls=24, max_duration_s=60),
    )
    trace = explorer.explore("INT-LOGIN-LLM", TF_BASE)

    assert trace.exploration_id.startswith("exp_")
    assert trace.test_intent_ref == "INT-LOGIN-LLM"
    # the policy answered five times: four tool calls plus the finish
    assert len(client.traces) == 5
    assert all(t.schema_ok for t in client.traces)
    assert all(t.provider == "scripted" for t in client.traces)
    executed = [step for step in trace.action_sequence if not step.get("rejected")]
    assert len(executed) >= 4
    assert trace.stop_reason == StopReason.PLAN_EXHAUSTED
    # trace records which policy produced it, and it is marked non-real inference
    assert "NOT-REAL-INFERENCE" in trace.agent_version


def test_trace_from_llm_loop_compiles_into_a_candidate(teamflow_server: None) -> None:
    """The compiler must not care whether a scripted or LLM policy produced the trace."""
    policy, _ = _policy()
    explorer = Explorer(policy=policy, budget=Budget(max_steps=12, max_tool_calls=24))
    intent = {
        "intent_id": "INT-LOGIN-COMPILE",
        "description": "log in as tester",
        "source_ref": "INT-LOGIN-COMPILE",
        "goal": "log in as tester and reach the project list",
        "expected_outcomes": [],
        "target": "/projects",
        "required_assertions": [],
        "constraints": [],
    }
    trace = explorer.explore("INT-LOGIN-COMPILE", TF_BASE, intent=intent)
    assert trace.goal_status == "VERIFIED", trace.goal_reason

    candidate = TestCaseCompiler().compile(
        trace,
        case_id="TC-LLM-001",
        title="login via reviewer-approved exploration",
        intent=TestIntent(**intent),
    )
    assert candidate.case_id == "TC-LLM-001"
    assert candidate.source_exploration_id == trace.exploration_id
    assert candidate.steps, "compiled candidate must have steps"
    assert candidate.uncertainty_notes is not None
    # a candidate is not a formal version until a human reviews it
    assert candidate.reviewer == ""
