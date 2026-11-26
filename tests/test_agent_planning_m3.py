from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from packages.agents.context_builder import ContextBuilder
from packages.agents.exploration import Budget, Observation, ToolGateway, verify_trace_record
from packages.agents.explorer import Explorer
from packages.agents.planner import ActionProposal, MockProvider, PlannerPolicy
from packages.agents.policy_validator import PolicyValidator
from packages.agents.post_action import AgentStopReason, evaluate_post_action


def _context(url_path: str = "/projects"):
    return ContextBuilder().build(
        test_intent={"goal": "open a project"},
        observation={
            "url_path": url_path,
            "state_summary": url_path,
            "observation_ref": "obs-1",
            "text_excerpt": "x" * 5000,
        },
        scenario={
            "scenario_id": "project-open",
            "version": "2.0.0",
            "constraints": ["never delete records"],
            "allowed_actions": ["navigate", "snapshot"],
            "denied_actions": ["delete"],
        },
        history=[],
        budget={"remaining": 3},
        goal_status="NOT_VERIFIED",
    )


def test_context_is_deterministic_bounded_and_preserves_safety_constraints() -> None:
    builder = ContextBuilder(max_text_chars=200)
    first = builder.build(
        test_intent={"goal": "open"},
        observation={"text_excerpt": "x" * 9000},
        scenario={
            "constraints": ["never delete"],
            "allowed_actions": ["snapshot"],
            "denied_actions": ["delete"],
        },
        history=[],
        budget={"remaining": 2},
        goal_status="INCONCLUSIVE",
    )
    second = builder.build(
        test_intent={"goal": "open"},
        observation={"text_excerpt": "x" * 9000},
        scenario={
            "constraints": ["never delete"],
            "allowed_actions": ["snapshot"],
            "denied_actions": ["delete"],
        },
        history=[],
        budget={"remaining": 2},
        goal_status="INCONCLUSIVE",
    )
    assert first == second
    assert len(first.observation["text_excerpt"]) == 200
    assert first.scenario["constraints"] == ["never delete"]
    assert first.denied_actions == ("delete",)


def test_mock_provider_is_observation_responsive_and_never_real() -> None:
    provider = MockProvider(
        {
            "/projects": {
                "action_type": "click",
                "arguments": {"selector": "[data-testid=project-link]"},
            },
            "/projects/42": {"action_type": "snapshot"},
        }
    )
    context = _context()
    assert provider.is_real_model is False
    assert provider.propose(context) == provider.propose(context)
    assert provider.propose(_context("/projects/42")).action_type == "snapshot"


def test_planner_policy_validates_before_returning_executable_tool_call() -> None:
    observation = Observation(url="http://localhost/", page_text="", url_path="/")
    planner = MockProvider(
        {
            observation.state_summary: {
                "action_type": "navigate",
                "arguments": {"url": "http://localhost/projects"},
            }
        }
    )
    policy = PlannerPolicy(
        planner=planner,
        scenario={"allowed_actions": ["navigate"], "denied_actions": []},
        test_intent={"goal": "go to projects"},
        budget=Budget(),
        run_id="run-1",
    )
    policy.bind_gateway(ToolGateway(Budget()))
    call = policy.next_action(observation)
    assert call is not None
    assert call.tool == "navigate"
    assert call.args == {"url": "http://localhost/projects"}
    assert policy.last_proposal is not None
    assert policy.last_proposal.decision_source == "mock"


def test_policy_validator_checks_run_action_schema_denial_budget_and_replay() -> None:
    validator = PolicyValidator()
    gateway = ToolGateway(Budget(max_steps=3, max_tool_calls=3))
    proposal = ActionProposal(
        "p1", "click", {"selector": "[data-testid=go]"}, "done", "test", "obs", "mock"
    )
    kwargs = dict(
        run_id="run-1",
        bound_run_id="run-1",
        allowed_actions={"click"},
        denied_actions=set(),
        gateway=gateway,
        remaining_budget=1,
    )
    decision = validator.validate(proposal, **kwargs)
    assert decision.allowed
    validator.record_execution(decision)
    assert "duplicate action proposal" in validator.validate(proposal, **kwargs).reasons
    assert (
        "run permission mismatch"
        in validator.validate(proposal, **{**kwargs, "bound_run_id": "run-2"}).reasons
    )
    assert (
        "action is explicitly denied"
        in validator.validate(proposal, **{**kwargs, "denied_actions": {"click"}}).reasons
    )
    bad_args = replace(proposal, arguments={"selector": "[data-testid=go]", "extra": True})
    assert (
        "action parameters do not match the strict schema"
        in validator.validate(bad_args, **kwargs).reasons
    )
    assert (
        "execution budget is exhausted"
        in validator.validate(proposal, **{**kwargs, "remaining_budget": 0}).reasons
    )
    unknown = replace(proposal, action_type="unknown")
    assert not validator.validate(unknown, **kwargs).allowed


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"goal_status": "VERIFIED"}, AgentStopReason.GOAL_VERIFIED),
        ({"plan_remaining": 0}, AgentStopReason.PLAN_EXHAUSTED),
        ({"budget_remaining": 0}, AgentStopReason.BUDGET_EXHAUSTED),
        ({"execution_error": "timeout"}, AgentStopReason.EXECUTION_FAILED),
        ({"policy_allowed": False}, AgentStopReason.POLICY_STOP),
        ({"goal_status": "INCONCLUSIVE"}, AgentStopReason.INCONCLUSIVE),
    ],
)
def test_post_action_stop_policy_matrix(
    options: dict[str, object], expected: AgentStopReason
) -> None:
    result = evaluate_post_action(
        actual_result={"detail": "saved"},
        expected_outcome="saved",
        before_observation={"observation_ref": "a"},
        after_observation={"observation_ref": "b"},
        **options,
    )
    assert result.stop_reason is expected


def test_mock_planner_runs_through_real_browser_gateway_and_trace_hash_v3() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/done":
                body = b"<title>Done</title><h1 data-testid='complete'>Complete</h1>"
            else:
                body = b"<title>Start</title><a data-testid='go' href='/done'>Continue</a>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    budget = Budget(
        max_steps=4,
        max_tool_calls=8,
        allowed_domains=("127.0.0.1",),
        allowed_origins=(base_url,),
    )
    blank_summary = "(untitled) @ blank | 0 testids | 0 elements"
    planner = MockProvider(
        {
            blank_summary: {"action_type": "navigate", "arguments": {"url": base_url}},
            "/": {
                "action_type": "click",
                "arguments": {"selector": "[data-testid=go]"},
                "expected_outcome": "done",
            },
        }
    )
    policy = PlannerPolicy(
        planner=planner,
        scenario={
            "scenario_id": "browser-m3",
            "version": "1.0.0",
            "allowed_actions": ["navigate", "click"],
            "denied_actions": [],
        },
        test_intent={"target": "/done", "goal": "reach done"},
        budget=budget,
        run_id="run-browser-m3",
    )
    try:
        explorer = Explorer(policy=policy, budget=budget, trace_hash_version=3)
        trace = explorer.explore(
            "intent-m3", base_url, intent={"target": "/done", "goal": "reach done"}
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    record = trace.to_dict()
    assert trace.goal_status == "VERIFIED"
    assert trace.stop_reason == "GOAL_VERIFIED"
    assert len(trace.decision_records) == 2
    assert all(row["decision_source"] == "mock" for row in trace.decision_records)
    assert verify_trace_record(record)
    tampered = dict(record)
    tampered["decision_records"] = [dict(row) for row in record["decision_records"]]
    tampered["decision_records"][0]["result"] = {"status": "tampered"}
    assert not verify_trace_record(tampered)
