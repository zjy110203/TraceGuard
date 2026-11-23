"""R5-4.6 goal semantics: the status vocabulary and every refusal scenario.

Scenario map (from the spec):
  A. correct login      -> VERIFIED
  B. wrong password     -> NOT_VERIFIED (stays on the login page)
  C. budget exhausted   -> NOT_VERIFIED / INCONCLUSIVE, never VERIFIED
  D. no goal declared   -> NOT_EVALUATED, compile refused
  E. goal/page mismatch -> NOT_VERIFIED, compile refused
"""

from __future__ import annotations

import pytest

from packages.agents.compiler import TestCaseCompiler
from packages.agents.exploration import (
    ExplorationStatus,
    ExplorationTrace,
    GoalStatus,
    StopReason,
    evaluate_goal,
)
from packages.core.errors import ValidationError
from packages.execution.models import TestIntent

TestIntent.__test__ = False  # dataclass named Test* is not a test class


def _obs(path: str, text: str = "", testids: tuple[str, ...] = ()) -> dict:
    return {
        "url": f"http://127.0.0.1:8001{path}",
        "url_path": path,
        "title": "TeamFlow",
        "visible_testids": list(testids),
        "text_excerpt": text,
        "step_index": 1,
        "phase": "pre",
    }


LOGIN_OBS = [
    _obs("/", "TeamFlow 登录", ("username", "password", "login-submit")),
    _obs("/projects", "项目列表", ("new-project-link", "project-item")),
]

INTENT = {
    "intent_id": "i-1",
    "description": "登录并进入项目列表",
    "goal": "登录并进入项目列表",
    "target": "/projects",
    "expected_outcomes": ["项目列表"],
    "required_assertions": ["state.project_count=1"],
    "constraints": [],
}


def _trace_with(*, stop: str, observations: list[dict]) -> ExplorationTrace:
    trace = ExplorationTrace(
        exploration_id="exp_goal",
        test_intent_ref="i-1",
        application_version="v",
        browser_version="b",
        agent_version="a",
        prompt_version="p",
        tool_version="t",
        test_intent=dict(INTENT),
        observations=observations,
        stop_reason=stop,
    )
    trace.finalize()
    return trace


# --------------------------------------------------------------- A. verified


def test_scenario_a_correct_login_is_verified() -> None:
    evaluation = evaluate_goal(INTENT, LOGIN_OBS)
    assert evaluation.status == GoalStatus.VERIFIED
    names = {c.name for c in evaluation.checks}
    assert {"target_path", "expected_outcome", "required_assertion"} <= names
    # state assertions are deferred to the formal-run Oracle, never faked here
    deferred = [c for c in evaluation.checks if c.deferred]
    assert deferred and all(not c.evaluated for c in deferred)


# ----------------------------------------------------------- B. wrong result


def test_scenario_b_wrong_password_is_not_verified() -> None:
    staying = [_obs("/", "TeamFlow 登录", ("username", "login-submit", "login-error"))]
    evaluation = evaluate_goal(INTENT, staying)
    assert evaluation.status == GoalStatus.NOT_VERIFIED
    failed = [c for c in evaluation.checks if c.evaluated and not c.ok]
    assert any(c.name == "target_path" for c in failed)


# ---------------------------------------------------------- C. budget stops


def test_scenario_c_budget_exhaustion_is_not_goal_evidence() -> None:
    trace = _trace_with(
        stop=StopReason.STEP_BUDGET,
        observations=[_obs("/", "TeamFlow 登录", ("username",))],
    )
    evaluation = evaluate_goal(trace.test_intent, trace.observations)
    assert evaluation.status == GoalStatus.NOT_VERIFIED

    # a trace with no usable observation is INCONCLUSIVE, never VERIFIED
    empty = _trace_with(stop=StopReason.TIME_BUDGET, observations=[])
    assert evaluate_goal(empty.test_intent, empty.observations).status == (GoalStatus.INCONCLUSIVE)


# ------------------------------------------------------- D. no goal declared


def test_scenario_d_no_goal_is_refused() -> None:
    trace = _trace_with(
        stop=StopReason.PLAN_EXHAUSTED,
        observations=[_obs("/projects", "项目列表", ("project-item",))],
    )
    trace.test_intent = {}
    evaluation = evaluate_goal({}, trace.observations)
    assert evaluation.status == GoalStatus.NOT_EVALUATED
    with pytest.raises(ValidationError) as excinfo:
        TestCaseCompiler().compile(trace, case_id="TC-NOGOAL", title="no goal")
    assert excinfo.value.details["goal_status"] == GoalStatus.NOT_EVALUATED


# -------------------------------------------------------- E. target mismatch


def test_scenario_e_target_page_mismatch_is_refused() -> None:
    intent = {**INTENT, "target": "/settings"}
    trace = _trace_with(stop=StopReason.PLAN_EXHAUSTED, observations=list(LOGIN_OBS))
    evaluation = evaluate_goal(intent, trace.observations)
    assert evaluation.status == GoalStatus.NOT_VERIFIED
    with pytest.raises(ValidationError) as excinfo:
        TestCaseCompiler().compile(
            trace,
            case_id="TC-MISMATCH",
            title="mismatch",
            intent=TestIntent(**intent),
        )
    # The goal gate fires first (NOT_VERIFIED); its checks name the mismatch.
    details = excinfo.value.details
    assert details["goal_status"] == GoalStatus.NOT_VERIFIED
    target_check = next(c for c in details["checks"] if c["name"] == "target_path")
    assert target_check["expected"] == "/settings"
    assert target_check["observed"] == "/projects"


# ------------------------------------------------- status vocabulary pinning


def test_status_vocabularies_are_explicit() -> None:
    # execution status != stop reason != goal status; the three axes exist so
    # "the loop ended" can never be reported as "the goal was proven".
    assert ExplorationStatus.COMPLETED != GoalStatus.VERIFIED
    for reason in (
        "GOAL_VERIFIED",
        "PLAN_EXHAUSTED",
        "STEP_BUDGET",
        "TOOL_CALL_BUDGET",
        "TIME_BUDGET",
        "POLICY_STOP",
        "ERROR",
    ):
        assert reason in StopReason.__dict__.values()
    assert "GOAL_REACHED" not in StopReason.__dict__.values()


def test_url_targets_are_normalized() -> None:
    evaluation = evaluate_goal({**INTENT, "target": "http://127.0.0.1:8001/projects"}, LOGIN_OBS)
    assert evaluation.status == GoalStatus.VERIFIED
