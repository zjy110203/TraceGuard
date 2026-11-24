"""R5-5.2: scenario-driven Agentic Testing.

Two layers, deliberately separate:

1. **Definition layer** (no browser): the registry contract and the structured
   goal assertions. These pin the semantics the whole stage depends on — above
   all that an unobservable assertion is INCONCLUSIVE, never FAILED, and that a
   goal with only optional assertions can never be VERIFIED.
2. **Execution layer** (real Chrome): the *same* ``Explorer`` runs two different
   business scenarios loaded from the registry. Nothing in the Explorer core
   knows about logins or projects; the difference is entirely scenario data.

The second layer is what makes "generic agentic testing" a claim instead of a
slogan, so it deliberately drives a real TeamFlow over HTTP with Playwright.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn

from apps.api.explorer_api import scenario_steps
from packages.agents.compiler import TestCaseCompiler
from packages.agents.exploration import Budget
from packages.agents.explorer import Explorer
from packages.agents.goal_assertions import (
    FAILED,
    INCONCLUSIVE,
    NOT_VERIFIED,
    PASSED,
    VERIFIED,
    AssertionDefinitionError,
    _ObservationView,
    evaluate_assertion,
    evaluate_assertions,
    validate_assertion,
)
from packages.agents.scenarios import Scenario, ScenarioRegistry, default_registry
from packages.core.errors import ValidationError
from tests.netutil import release_reserved, reserve_port, take_reserved

TF_PORT = reserve_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"


# --------------------------------------------------------------------------- #
# 1. Definition layer
# --------------------------------------------------------------------------- #


def test_default_registry_is_valid_and_lists_every_required_scenario() -> None:
    registry = default_registry()

    assert registry.validate_all() == {}
    ids = {s.scenario_id for s in registry.all()}
    assert {
        "teamflow-login",
        "project-create",
        "project-query",
        "page-navigation",
        "project-edit",
        "form-validation",
    } <= ids
    # An unimplemented scenario stays visible, with the reason it is not runnable.
    for scenario in registry.all():
        if not scenario.implemented:
            assert scenario.blocked_reason, scenario.scenario_id


def test_registry_rejects_duplicates_and_unknown_actions() -> None:
    registry = ScenarioRegistry()
    scenario = default_registry().require("teamflow-login")
    registry.register(scenario)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(scenario)

    bad = Scenario(
        **{
            **scenario.__dict__,
            "scenario_id": "bad-actions",
            "allowed_actions": ("navigate", "snapshot", "shell"),
            "steps": (),
        }
    )
    with pytest.raises(ValueError, match="unknown tools"):
        registry.register(bad)


def test_scenario_without_constraints_is_refused() -> None:
    scenario = default_registry().require("teamflow-login")
    bad = Scenario(**{**scenario.__dict__, "scenario_id": "no-limits", "constraints": ()})

    with pytest.raises(ValueError, match="constraints"):
        ScenarioRegistry().register(bad)


def test_unsupported_assertion_kind_is_a_definition_error() -> None:
    problems = validate_assertion({"name": "x", "kind": "looks_nice", "value": "y"})

    assert problems and "unsupported" in problems[0]
    with pytest.raises(AssertionDefinitionError):
        evaluate_assertion({"name": "x", "kind": "looks_nice"}, _ObservationView({}))


_OBSERVATION: dict[str, Any] = {
    "url": "http://127.0.0.1:8001/projects",
    "url_path": "/projects",
    "title": "项目列表 · TeamFlow",
    "text_excerpt": "项目列表 项目创建成功",
    "visible_testids": ["project-list", "project-item", "new-project-link", "project-name"],
    "visible_elements": [
        {"tag": "ul", "testid": "project-list", "name": "Alpha", "visible": True},
        {"tag": "a", "testid": "new-project-link", "name": "新建项目", "visible": True},
        {"tag": "input", "testid": "project-name", "name": "name", "visible": True},
        {
            "tag": "button",
            "testid": "create-submit",
            "name": "创建",
            "visible": True,
            "disabled": True,
        },
        # Deliberately pre-R5-5.2 shape: no ``visible`` key, as traces captured
        # before visibility was recorded look like this.
        {"tag": "span", "testid": "legacy-badge", "name": "旧"},
    ],
}


@pytest.mark.parametrize(
    ("spec", "verdict"),
    [
        ({"name": "url", "kind": "url_exact", "value": "/projects"}, PASSED),
        ({"name": "url", "kind": "url_exact", "value": "/login"}, FAILED),
        ({"name": "url", "kind": "url_regex", "value": r"^/proj"}, PASSED),
        ({"name": "url", "kind": "url_regex", "value": r"^/admin"}, FAILED),
        ({"name": "tid", "kind": "testid", "value": "project-list"}, PASSED),
        ({"name": "tid", "kind": "testid", "value": "ghost"}, FAILED),
        ({"name": "el", "kind": "element_exists", "testid": "project-name"}, PASSED),
        ({"name": "el", "kind": "element_exists", "testid": "ghost"}, FAILED),
        ({"name": "vis", "kind": "element_visible", "testid": "new-project-link"}, PASSED),
        (
            {
                "name": "txt",
                "kind": "element_text",
                "testid": "new-project-link",
                "contains": "新建",
            },
            PASSED,
        ),
        (
            {
                "name": "txt",
                "kind": "element_text",
                "testid": "new-project-link",
                "contains": "删除",
            },
            FAILED,
        ),
        (
            {"name": "fs", "kind": "form_state", "testid": "project-name", "state": "present"},
            PASSED,
        ),
        ({"name": "fs", "kind": "form_state", "testid": "ghost", "state": "absent"}, PASSED),
        (
            {"name": "fs", "kind": "form_state", "testid": "create-submit", "state": "disabled"},
            PASSED,
        ),
        ({"name": "ps", "kind": "page_state", "field": "title", "value": "项目列表"}, PASSED),
        (
            {
                "name": "combo",
                "kind": "all_of",
                "assertions": [
                    {"name": "a", "kind": "url_exact", "value": "/projects"},
                    {"name": "b", "kind": "testid", "value": "project-list"},
                ],
            },
            PASSED,
        ),
        (
            {
                "name": "any",
                "kind": "any_of",
                "assertions": [
                    {"name": "a", "kind": "url_exact", "value": "/nope"},
                    {"name": "b", "kind": "testid", "value": "project-list"},
                ],
            },
            PASSED,
        ),
        (
            {
                "name": "neg",
                "kind": "not",
                "assertions": [{"name": "a", "kind": "url_exact", "value": "/login"}],
            },
            PASSED,
        ),
    ],
)
def test_assertion_semantics(spec: dict[str, Any], verdict: str) -> None:
    result = evaluate_assertion(spec, _ObservationView(_OBSERVATION))

    assert result.verdict == verdict, result.to_dict()
    assert result.evidence, "every assertion must say where it read the fact"


def test_unobservable_assertion_is_inconclusive_not_failed() -> None:
    """The core rule: 'we did not look' is not 'it is false'."""
    blind = _ObservationView({"url": "http://127.0.0.1:8001/projects", "url_path": "/projects"})

    report = evaluate_assertions(
        [{"name": "el", "kind": "element_exists", "testid": "project-list"}],
        dict(blind.raw),
    )

    assert report.verdict == INCONCLUSIVE
    assert report.results[0].verdict == INCONCLUSIVE
    assert "cannot be decided" in report.results[0].detail or report.results[0].detail


def test_pre_visibility_observations_are_inconclusive_not_failed() -> None:
    """Traces captured before visibility existed must not be called failures."""
    legacy = _ObservationView(
        {
            "url_path": "/projects",
            "visible_testids": ["project-list"],
            "visible_elements": [{"tag": "ul", "testid": "project-list", "name": ""}],
        }
    )

    result = evaluate_assertion(
        {"name": "vis", "kind": "element_visible", "testid": "project-list"}, legacy
    )

    assert result.verdict == INCONCLUSIVE


def test_optional_failures_never_block_a_verified_goal() -> None:
    report = evaluate_assertions(
        [
            {"name": "required", "kind": "url_exact", "value": "/projects", "required": True},
            {"name": "nice", "kind": "testid", "value": "ghost", "required": False},
        ],
        _OBSERVATION,
    )

    assert report.verdict == VERIFIED
    assert report.to_dict()["optional_failed"] == ["nice"]


def test_goal_with_only_optional_assertions_cannot_be_verified() -> None:
    report = evaluate_assertions(
        [{"name": "nice", "kind": "url_exact", "value": "/projects", "required": False}],
        _OBSERVATION,
    )

    assert report.verdict == INCONCLUSIVE
    assert "prove nothing" in report.reason


def test_required_failure_wins_over_inconclusive() -> None:
    report = evaluate_assertions(
        [
            {"name": "wrong", "kind": "url_exact", "value": "/login", "required": True},
            # project-list has no ``visible`` flag in this observation, so this
            # one is genuinely INCONCLUSIVE (a different verdict from FAILED).
            {
                "name": "blind",
                "kind": "element_visible",
                "testid": "legacy-badge",
                "required": True,
            },
        ],
        _OBSERVATION,
    )

    assert report.verdict == NOT_VERIFIED
    assert report.to_dict()["required_failed"] == ["wrong"]
    assert report.to_dict()["required_inconclusive"] == ["blind"]


# --------------------------------------------------------------------------- #
# 2. Execution layer: the same Explorer, two different scenarios, real Chrome
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def teamflow_server() -> Iterator[None]:
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=TF_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": take_reserved(TF_PORT)}, daemon=True
    )
    thread.start()
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:  # pragma: no cover
            pytest.fail("TeamFlow did not become healthy")
        yield
    finally:
        server.should_exit = True
        thread.join(15)
        release_reserved(TF_PORT)


def _intent_from_scenario(scenario: Scenario) -> dict[str, Any]:
    templates = scenario.intent_templates
    return {
        "intent_id": f"i-{scenario.scenario_id}",
        "description": str(templates.get("goal") or scenario.description),
        "scenario": scenario.scenario_id,
        "goal": str(templates.get("goal") or ""),
        "target": str(templates.get("target") or ""),
        "expected_outcomes": list(templates.get("expected_outcomes") or []),
        "required_assertions": list(templates.get("required_assertions") or []),
        "constraints": list(scenario.constraints),
    }


def _run_scenario(scenario: Scenario):  # noqa: ANN202 - ExplorationTrace
    from packages.agents.scripted_agent import ScriptedAgentPolicy

    steps = scenario_steps(
        scenario,
        base_url=TF_BASE,
        username="tester",
        password="test-pass-123",
    )
    explorer = Explorer(
        policy=ScriptedAgentPolicy(steps, goal=scenario.description),
        budget=Budget(
            max_steps=scenario.max_steps,
            max_tool_calls=scenario.max_steps * 2 + 4,
            max_duration_s=scenario.timeout_budget_s,
            allowed_domains=("127.0.0.1",),
            allowed_origins=(TF_BASE,),
        ),
        application_version=f"teamflow-0.1.0 ({scenario.scenario_id})",
    )
    return explorer.explore(
        f"i-{scenario.scenario_id}", TF_BASE, intent=_intent_from_scenario(scenario)
    )


def test_two_scenarios_run_through_the_same_explorer(teamflow_server: None) -> None:
    """Genericity proof: the core loop is untouched, only scenario data differs."""
    registry = default_registry()
    login = registry.require("teamflow-login")
    create = registry.require("project-create")

    login_trace = _run_scenario(login)
    create_trace = _run_scenario(create)

    assert login_trace.goal_status == VERIFIED, login_trace.goal_reason
    assert create_trace.goal_status == VERIFIED, create_trace.goal_reason
    # Different scenarios really took different paths.
    assert len(create_trace.action_sequence) > len(login_trace.action_sequence)
    assert create_trace.test_intent["scenario"] == "project-create"

    # Scenario assertions agree with the goal verdict, independently computed.
    login_report = evaluate_assertions(
        list(login.goal_definition), dict(login_trace.observations[-1])
    )
    create_report = evaluate_assertions(
        list(create.goal_definition), dict(create_trace.observations[-1])
    )
    assert login_report.verdict == VERIFIED, login_report.reason
    assert create_report.verdict == VERIFIED, create_report.reason


def test_scenario_traces_compile_into_candidates(teamflow_server: None) -> None:
    """TestIntent -> Explore -> Goal verified -> Candidate (compiler admission)."""
    registry = default_registry()
    compiler = TestCaseCompiler()

    for scenario_id, case_id in (
        ("teamflow-login", "TC-SCENARIO-LOGIN"),
        ("project-create", "TC-SCENARIO-CREATE"),
    ):
        trace = _run_scenario(registry.require(scenario_id))
        candidate = compiler.compile(trace, case_id=case_id, title=f"scenario {scenario_id}")

        assert candidate.goal_status == VERIFIED
        assert candidate.source_trace_hash == trace.source_trace_hash
        assert candidate.steps and candidate.assertions and candidate.oracle_bindings
        assert candidate.review_checklist


def test_wrong_password_scenario_is_not_verified(teamflow_server: None) -> None:
    """Failure scenario: the click works, the business goal does not happen."""
    login = default_registry().require("teamflow-login")
    steps = scenario_steps(login, base_url=TF_BASE, username="tester", password="definitely-wrong")
    from packages.agents.scripted_agent import ScriptedAgentPolicy

    trace = Explorer(
        policy=ScriptedAgentPolicy(steps, goal=login.description),
        budget=Budget(
            max_steps=login.max_steps,
            max_tool_calls=20,
            max_duration_s=login.timeout_budget_s,
            allowed_domains=("127.0.0.1",),
            allowed_origins=(TF_BASE,),
        ),
    ).explore("i-login-wrong", TF_BASE, intent=_intent_from_scenario(login))

    assert trace.goal_status == "NOT_VERIFIED", trace.goal_reason
    with pytest.raises(ValidationError):
        TestCaseCompiler().compile(trace, case_id="TC-WRONG", title="wrong password")
