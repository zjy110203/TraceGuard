"""R5-4.1 acceptance: does the Oracle verify the *business* result?

Five scenarios pin the difference between "the click worked" and "the user is
logged in". They run a real browser against a real, isolated TeamFlow instance;
no LLM is involved (compiled cases are deterministic rules, and the exploration
policy is the scripted one).

  A. correct credentials        -> the goal state is reached  -> PASSED
  B. wrong password             -> the button still clicked   -> never PASSED
  C. unbound sensitive data     -> BLOCKED / NOT_EVALUATED, no placeholder replay
  D. action ok, business state wrong -> FAILED (a click is not an Oracle)
  E. multi-step flow            -> the final goal is verified, not the first click
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import replace

import httpx
import pytest
import uvicorn

from apps.teamflow.main import build_app as build_teamflow_app
from packages.agents.compiler import TestCaseCompiler
from packages.agents.exploration import Budget, ScriptedPolicy, ToolCall
from packages.agents.explorer import Explorer
from packages.core.config import Settings
from packages.execution.dataref import MappingTestDataProvider, NullTestDataProvider
from packages.execution.models import (
    AssertionSpec,
    OracleBinding,
    OracleSpec,
    TestCaseVersion,
    TestDataRef,
    TestIntent,
    TestStep,
)
from packages.execution.runner import LocalRunner
from tests.netutil import free_port

for _m in (TestCaseVersion, TestStep, TestDataRef, TestIntent):
    _m.__test__ = False

TF_PORT = free_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"
EVAL_TOKEN = "test-eval-token"
PASSWORD = "test-pass-123"


@pytest.fixture(scope="module")
def teamflow(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real TeamFlow on its own port, backed by a throwaway SQLite file."""
    db = tmp_path_factory.mktemp("tf") / "teamflow.db"
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{db.as_posix()}",
        eval_control_token=EVAL_TOKEN,
        teamflow_test_user="tester",
        teamflow_test_password=PASSWORD,
    )
    app = build_teamflow_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=TF_PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        try:
            if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:  # pragma: no cover - only when the port is unusable
        pytest.fail("TeamFlow did not become healthy")
    yield TF_BASE
    server.should_exit = True
    thread.join(timeout=10)


def _reset(base: str) -> None:
    headers = {"X-Eval-Token": EVAL_TOKEN}
    with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
        client.post("/__eval__/faults/clear", headers=headers)
        client.post("/__eval__/data/reset", headers=headers)


def _ui(source_ref: str, *, oracle_id: str) -> OracleSpec:
    return OracleSpec(
        oracle_id=oracle_id,
        source_type="ui",
        source_ref=source_ref,
        expected_value=True,
        source="tests/test_oracle_semantics.py (human-authored)",
    )


def _state(key: str, expected: object, *, oracle_id: str) -> OracleSpec:
    return OracleSpec(
        oracle_id=oracle_id,
        source_type="independent_api",
        source_ref=f"state.{key}",
        expected_value=expected,
        source="tests/test_oracle_semantics.py (human-declared expectation)",
    )


def _login_case() -> TestCaseVersion:
    """Login case whose Oracles describe the *goal*, not the click."""
    case = TestCaseVersion(
        case_id="TC-SEM-LOGIN",
        version=1,
        title="valid login reaches the project list",
        intent=TestIntent(
            intent_id="i-sem-login",
            description="a tester logs in and lands on the project list",
            goal="登录 TeamFlow 并进入项目列表",
            expected_outcomes=("项目列表",),
            target="/projects",
        ),
        steps=(
            TestStep("s1", "navigate", "/"),
            TestStep("s2", "fill", "[data-testid=username]", value="tester"),
            TestStep("s3", "fill", "[data-testid=password]", value_ref="teamflow.password"),
            TestStep("s4", "click", "[data-testid=login-submit]"),
            # An empty <ul> has a zero-size box, so it is not "visible" until a
            # project exists; the nav link is the stable destination marker.
            TestStep("s5", "assert_visible", "[data-testid=new-project-link]"),
            TestStep("s6", "assert_not_visible", "[data-testid=login-submit]"),
        ),
        assertions=(
            AssertionSpec("a1", "页面进入项目列表", required=True),
            AssertionSpec("a2", "登录表单已消失", required=True),
            AssertionSpec("a3", "目标路径为 /projects", required=True),
        ),
        oracle_bindings=(
            OracleBinding("a1", primary=_ui("ui.s5.passed", oracle_id="o-a1")),
            OracleBinding("a2", primary=_ui("ui.s6.passed", oracle_id="o-a2")),
            OracleBinding(
                "a3",
                primary=OracleSpec(
                    oracle_id="o-a3",
                    source_type="ui",
                    source_ref="ui.url_path",
                    expected_value="/projects",
                    source="the goal page is a route, not a click",
                ),
            ),
        ),
        data_refs=(TestDataRef("teamflow.password", "[data-testid=password]", value=""),),
        approved_by="human:sem-1",
    )
    case.validate()
    return case


# --------------------------------------------------------------------- A / B


def test_a_correct_login_passes_on_business_state(teamflow: str) -> None:
    _reset(teamflow)
    runner = LocalRunner(
        teamflow,
        data_provider=MappingTestDataProvider({"teamflow.password": PASSWORD}),
    )
    report = runner.run(_login_case())

    assert report.execution_status.value == "COMPLETED"
    assert report.test_verdict == "PASSED", report.assertion_results
    assert report.blocked is False
    # The verdict came from observed page state, not from "the click returned".
    assert report.observations["ui"]["url_path"] == "/projects"
    assert report.observations["ui"]["s5.passed"] is True


def test_b_wrong_password_is_never_passed(teamflow: str) -> None:
    """The button is clickable; the goal is not reached. That is a FAILED test."""
    _reset(teamflow)
    runner = LocalRunner(
        teamflow,
        data_provider=MappingTestDataProvider({"teamflow.password": "definitely-wrong"}),
    )
    report = runner.run(_login_case())

    assert report.test_verdict != "PASSED"
    assert report.test_verdict == "FAILED", report.assertion_results
    # Proof the false positive is dead: the click itself succeeded.
    assert report.observations["ui"]["s4.passed"] is True
    # The failed POST returns the login page again: the goal was not reached.
    assert report.observations["ui"]["url_path"] == "/login"


# ------------------------------------------------------------------------- C


def test_c_unbound_sensitive_data_blocks_instead_of_replaying(teamflow: str) -> None:
    _reset(teamflow)
    runner = LocalRunner(teamflow, data_provider=NullTestDataProvider())
    report = runner.run(_login_case())

    assert report.blocked is True
    assert "teamflow.password" in report.blocked_reason
    assert report.test_verdict == "NOT_EVALUATED"
    # Nothing was executed: the browser never typed the placeholder.
    assert report.steps == []
    assert report.evidence == []


def test_c2_placeholder_is_never_accepted_as_a_value(teamflow: str) -> None:
    """A case that inlines the redaction marker is rejected structurally."""
    case = _login_case()
    broken = TestCaseVersion(
        **{
            **case.__dict__,
            "steps": tuple(
                TestStep("s3", "fill", "[data-testid=password]", value="«redacted»")
                if s.step_id == "s3"
                else s
                for s in case.steps
            ),
            "data_refs": (),
        }
    )
    with pytest.raises(ValueError, match="placeholder"):
        broken.validate()


# ------------------------------------------------------------------------- D


def test_d_successful_action_but_wrong_business_state_fails(teamflow: str) -> None:
    """Every step succeeds; the business fact is false. Verdict must be FAILED."""
    _reset(teamflow)
    case = TestCaseVersion(
        case_id="TC-SEM-STATE",
        version=1,
        title="login click succeeds but the business state assertion is false",
        intent=TestIntent(
            intent_id="i-sem-state",
            description="project count is what the test claims",
            goal="登录后业务状态符合预期",
        ),
        steps=(
            TestStep("s1", "navigate", "/"),
            TestStep("s2", "fill", "[data-testid=username]", value="tester"),
            TestStep("s3", "fill", "[data-testid=password]", value_ref="teamflow.password"),
            TestStep("s4", "click", "[data-testid=login-submit]"),
        ),
        assertions=(AssertionSpec("a1", "业务状态 project_count 等于 5", required=True),),
        oracle_bindings=(
            OracleBinding("a1", primary=_state("project_count", 5, oracle_id="o-a1")),
        ),
        data_refs=(TestDataRef("teamflow.password", "[data-testid=password]", value=""),),
        approved_by="human:sem-2",
    )
    case.validate()
    runner = LocalRunner(
        teamflow, data_provider=MappingTestDataProvider({"teamflow.password": PASSWORD})
    )
    report = runner.run(case)

    assert report.execution_status.value == "COMPLETED"  # the click really worked
    assert report.observations["ui"]["s4.passed"] is True
    assert report.test_verdict == "FAILED"
    assert report.assertion_results[0]["verdict"] == "FAILED"


# ------------------------------------------------------------------------- E


def _create_project_case(name: str) -> TestCaseVersion:
    """Multi-click flow: the Oracle must point at the end, not at login."""
    case = TestCaseVersion(
        case_id="TC-SEM-CREATE",
        version=1,
        title="login then create a project, verified by business state",
        intent=TestIntent(
            intent_id="i-sem-create",
            description="a logged-in tester can create a project that persists",
            goal="创建项目并确认已持久化",
            target="/projects/",
            required_assertions=("state.project_count=1",),
        ),
        steps=(
            TestStep("s1", "navigate", "/"),
            TestStep("s2", "fill", "[data-testid=username]", value="tester"),
            TestStep("s3", "fill", "[data-testid=password]", value_ref="teamflow.password"),
            TestStep("s4", "click", "[data-testid=login-submit]"),
            TestStep("s5", "click", "[data-testid=new-project-link]"),
            TestStep("s6", "fill", "[data-testid=project-name]", value=name),
            TestStep("s7", "click", "[data-testid=create-submit]"),
            TestStep("s8", "assert_text", "body", value="项目创建成功"),
        ),
        assertions=(
            AssertionSpec("a1", "项目已持久化（业务状态）", required=True),
            AssertionSpec("a2", "页面显示创建成功", required=True),
        ),
        oracle_bindings=(
            OracleBinding("a1", primary=_state("project_count", 1, oracle_id="o-a1")),
            OracleBinding("a2", primary=_ui("ui.s8.passed", oracle_id="o-a2")),
        ),
        data_refs=(TestDataRef("teamflow.password", "[data-testid=password]", value=""),),
        approved_by="human:sem-3",
    )
    case.validate()
    return case


def test_e_multi_step_flow_verifies_the_final_goal(teamflow: str) -> None:
    _reset(teamflow)
    runner = LocalRunner(
        teamflow, data_provider=MappingTestDataProvider({"teamflow.password": PASSWORD})
    )
    report = runner.run(_create_project_case("sem-project"))

    assert report.test_verdict == "PASSED", report.assertion_results
    # The passing Oracles reference the last steps, never the login click.
    refs = [b.primary.source_ref for b in _create_project_case("x").oracle_bindings]
    assert "ui.s4.passed" not in refs
    assert report.observations["ui"]["s8.passed"] is True


def test_e2_multi_step_flow_fails_when_only_the_ui_claims_success(teamflow: str) -> None:
    """Same flow, but the business state does not match: UI alone must not pass."""
    _reset(teamflow)
    case = _create_project_case("sem-project-2")
    # A second project is claimed while only one can exist after a reset.
    overstated = TestCaseVersion(
        **{
            **case.__dict__,
            "oracle_bindings": (
                OracleBinding("a1", primary=_state("project_count", 2, oracle_id="o-a1")),
                OracleBinding("a2", primary=_ui("ui.s8.passed", oracle_id="o-a2")),
            ),
        }
    )
    overstated.validate()
    runner = LocalRunner(
        teamflow, data_provider=MappingTestDataProvider({"teamflow.password": PASSWORD})
    )
    report = runner.run(overstated)

    assert report.observations["ui"]["s8.passed"] is True  # UI said success
    assert report.test_verdict == "FAILED"  # business state said otherwise


# ------------------------------------------------- F: the compiled path is runnable


def test_f_explore_compile_promote_run_is_a_real_business_test(teamflow: str) -> None:
    """The whole R5-4.1 chain, end to end, with a real browser.

    Exploration -> structured trace -> compiler (goal-level assertions) ->
    human approval -> execution. The compiled case must be both runnable and
    honest: no plaintext password in the trace or the case, no click Oracle.
    """
    _reset(teamflow)
    script = (
        ToolCall("navigate", {"url": f"{teamflow}/"}, "open login"),
        ToolCall("fill", {"selector": "[data-testid=username]", "value": "tester"}, "username"),
        ToolCall(
            "fill",
            {"selector": "[data-testid=password]", "value": PASSWORD},
            "password",
        ),
        ToolCall("click", {"selector": "[data-testid=login-submit]"}, "submit"),
    )
    trace = Explorer(
        policy=ScriptedPolicy(script),
        budget=Budget(max_steps=10, max_tool_calls=20, max_duration_s=45),
    ).explore("i-sem-e2e", teamflow)

    # The structured observation sequence captured both page states.
    paths = [o["url_path"] for o in trace.observations]
    assert "/" in paths and "/projects" in paths
    # No credential anywhere in the trace.
    serialized = str(trace.to_dict())
    assert PASSWORD not in serialized
    assert "«redacted»" in serialized

    intent = TestIntent(
        intent_id="i-sem-e2e",
        description="登录后进入项目列表",
        goal="登录 TeamFlow 并进入项目列表",
        target="/projects",
        required_assertions=(),
    )
    candidate = TestCaseCompiler().compile(
        trace, case_id="TC-SEM-COMPILED", title="compiled login", intent=intent
    )
    # The sensitive step is a reference, not a literal.
    fill_steps = [s for s in candidate.steps if s.action == "fill"]
    assert any(s.value_ref == "teamflow.password" and s.value == "" for s in fill_steps)
    assert all(PASSWORD not in s.value for s in candidate.steps)
    # Every assertion names where it came from, and none is a click outcome.
    assert all(b.primary.source for b in candidate.oracle_bindings)
    assert not any(b.primary.source_ref == "ui.s4.passed" for b in candidate.oracle_bindings)

    approved = replace(
        candidate,
        oracle_bindings=tuple(
            replace(
                b,
                primary=replace(b.primary, approved=True, approved_by="qa"),
                supporting=tuple(replace(s, approved=True, approved_by="qa") for s in b.supporting),
            )
            for b in candidate.oracle_bindings
        ),
    )
    formal = TestCaseCompiler().promote(approved, reviewer="qa", approval_record="REV-SEM")
    report = LocalRunner(
        teamflow, data_provider=MappingTestDataProvider({"teamflow.password": PASSWORD})
    ).run(formal)

    assert report.test_verdict == "PASSED", report.assertion_results
    assert report.observations["ui"]["url_path"] == "/projects"
