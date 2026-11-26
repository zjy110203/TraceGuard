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

import contextlib
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from apps.api.explorer_api import scenario_steps
from apps.api.main import build_app as build_platform_app
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
from packages.core.config import Settings
from packages.core.errors import ValidationError
from tests.netutil import release_reserved, reserve_port, serve_uvicorn, take_reserved

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
        assert set(scenario.paths or {}) == {"normal", "exception", "boundary", "recovery"}
        for path_id in scenario.paths or {}:
            spec = scenario.path_spec_for_path(path_id)
            assert spec is not None
            assert spec["path_id"]
            assert spec["expected_goal_status"] in {"VERIFIED", "NOT_VERIFIED"}
            assert any(item.get("required", True) for item in spec["expect"])
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


def test_registry_supports_exact_versions_without_replacing_history() -> None:
    registry = ScenarioRegistry()
    latest = default_registry().require("teamflow-login")
    original = Scenario(**{**latest.__dict__, "version": "1.0.0"})
    newer = Scenario(**{**latest.__dict__, "version": "2.0.0"})
    registry.register(original)
    registry.register(newer)

    assert registry.require("teamflow-login", "1.0.0") is original
    assert registry.require("teamflow-login", "2.0.0") is newer
    assert registry.require("teamflow-login") is newer
    assert len(registry.all()) == 2
    with pytest.raises(ValueError, match="already registered"):
        registry.register(newer)


def test_scenario_legacy_mapping_and_strict_unknown_field_validation() -> None:
    original = default_registry().require("teamflow-login")
    legacy_fields = {"version", "preconditions", "data_setup", "cleanup", "denied_actions", "paths"}
    legacy = Scenario.from_mapping(
        {key: value for key, value in original.__dict__.items() if key not in legacy_fields}
    )
    assert legacy.version == "1.0.0"
    assert legacy.validate() == []
    with pytest.raises(ValueError, match="unknown scenario fields"):
        Scenario.from_mapping({**original.__dict__, "unexpected": True})


def test_scenario_digest_is_canonical_and_covers_v2_fields() -> None:
    original = default_registry().require("teamflow-login")
    equivalent = Scenario(
        **{**original.__dict__, "intent_templates": dict(original.intent_templates)}
    )
    changed = Scenario(**{**original.__dict__, "denied_actions": ("click",)})

    assert original.digest == equivalent.digest
    assert original.digest != changed.digest
    assert original.to_dict()["digest_algorithm_version"] == "canonical-json-v1"


def test_scenario_digest_rejects_nan_and_preserves_list_order_and_null() -> None:
    original = default_registry().require("teamflow-login")
    with pytest.raises(ValueError, match="Out of range float"):
        assert Scenario(**{**original.__dict__, "data_setup": {"value": float("nan")}}).digest
    list_a = Scenario(**{**original.__dict__, "constraints": ("first", "second")})
    list_b = Scenario(**{**original.__dict__, "constraints": ("second", "first")})
    null_value = Scenario(**{**original.__dict__, "data_setup": {"key": None}})
    empty_value = Scenario(**{**original.__dict__, "data_setup": {}})
    assert list_a.digest != list_b.digest
    assert null_value.digest != empty_value.digest


def test_scenario_without_constraints_is_refused() -> None:
    scenario = default_registry().require("teamflow-login")
    bad = Scenario(**{**scenario.__dict__, "scenario_id": "no-limits", "constraints": ()})

    with pytest.raises(ValueError, match="constraints"):
        ScenarioRegistry().register(bad)


@pytest.mark.parametrize(
    "spec,problem",
    [
        (
            {
                "path_id": "p",
                "expect": [{"name": "x", "kind": "testid", "value": "x"}],
                "steps": [],
            },
            "expected_goal_status",
        ),
        ({"path_id": "p", "expected_goal_status": "VERIFIED", "steps": []}, "expect"),
        ({"path_id": "p", "expected_goal_status": "VERIFIED", "expect": [], "steps": []}, "expect"),
        (
            {
                "path_id": "p",
                "expected_goal_status": "VERIFIED",
                "expect": [{"name": "x", "kind": "testid", "value": "x", "required": False}],
                "steps": [],
            },
            "required assertion",
        ),
        (
            {
                "path_id": "p",
                "expected_goal_status": "INCONCLUSIVE",
                "expect": [{"name": "x", "kind": "testid", "value": "x"}],
                "steps": [],
            },
            "expected_goal_status",
        ),
    ],
)
def test_scenario_path_spec_requires_explicit_oracle(spec, problem) -> None:
    source = default_registry().require("teamflow-login")
    bad = Scenario(**{**source.__dict__, "paths": {"normal": spec}})
    assert any(problem in issue for issue in bad.validate())


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


def _run_scenario(
    scenario: Scenario,
    *,
    base_url: str = TF_BASE,
    steps: list[Any] | None = None,
    max_tool_calls: int | None = None,
):  # noqa: ANN202 - ExplorationTrace
    from packages.agents.scripted_agent import ScriptedAgentPolicy

    if steps is None:
        steps = scenario_steps(
            scenario,
            base_url=base_url,
            username="tester",
            password="test-pass-123",
        )
    explorer = Explorer(
        policy=ScriptedAgentPolicy(steps, goal=scenario.description),
        budget=Budget(
            max_steps=scenario.max_steps,
            max_tool_calls=max_tool_calls or scenario.max_steps * 2 + 4,
            max_duration_s=scenario.timeout_budget_s,
            allowed_domains=("127.0.0.1",),
            allowed_origins=(base_url,),
        ),
        application_version=f"teamflow-0.1.0 ({scenario.scenario_id})",
    )
    return explorer.explore(
        f"i-{scenario.scenario_id}", base_url, intent=_intent_from_scenario(scenario)
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


# --------------------------------------------------------------------------- #
# 3. project-query and page-navigation against a real, isolated SUT
# --------------------------------------------------------------------------- #
#
# These two scenarios used to be registry-only: ``scenario_steps`` produced
# steps for them, but no test ever proved the steps complete a real browser
# journey. They now run against their own TeamFlow instance with its own SQLite
# file, so "the list has projects" is data this test created, not whatever the
# developer's data directory happened to contain.


@contextlib.contextmanager
def _isolated_teamflow(tmp_path: Path) -> Iterator[str]:
    """A fresh TeamFlow on its own database (empty project list)."""
    from apps.teamflow.main import build_app as build_teamflow_app

    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{(tmp_path / 'teamflow.db').as_posix()}",
    )
    with serve_uvicorn(build_teamflow_app(settings)) as base:
        assert httpx.get(f"{base}/health", timeout=5, trust_env=False).status_code == 200
        yield base


def _create_project(base_url: str, name: str, description: str = "") -> str:
    """Create a project through the real form, then read its id off the list."""
    with httpx.Client(
        base_url=base_url, timeout=10, trust_env=False, follow_redirects=True
    ) as client:
        response = client.post("/projects/new", data={"name": name, "description": description})
        assert response.status_code == 200, response.text
        html = client.get("/projects").text
    match = re.search(r'data-project-id="([^"]+)"[^>]*>\s*<a[^>]*>' + re.escape(name), html)
    assert match, f"project {name!r} is not on the list page"
    return match.group(1)


def test_project_query_opens_a_real_project_detail(tmp_path: Path) -> None:
    """Login -> list -> click a real row -> detail page, judged by the goal."""
    scenario = default_registry().require("project-query")
    with _isolated_teamflow(tmp_path) as base:
        first_id = _create_project(base, "Alpha项目", "第一个项目")
        # A second project proves the selector picks the FIRST row deterministically:
        # two matches used to be a strict-mode failure, not a click.
        _create_project(base, "Beta项目", "第二个项目")

        trace = _run_scenario(scenario, base_url=base)

        assert trace.goal_status == VERIFIED, trace.goal_reason
        # The journey is in the trace, not just the verdict.
        assert trace.observations[0]["url_path"] in ("/", "")
        detail = trace.observations[-1]
        assert detail["url_path"] == f"/projects/{first_id}"
        assert "Alpha项目" in str(detail["title"] or "")
        assert "project-title" in set(detail["visible_testids"])
        assert "project-status" in set(detail["visible_testids"])
        assert any(
            entry.get("tool") == "click"
            and "project-item" in str(entry.get("args", {}).get("selector", ""))
            for entry in trace.action_sequence
        )

        report = evaluate_assertions(list(scenario.goal_definition), detail)
        assert report.verdict == VERIFIED, report.reason

        candidate = TestCaseCompiler().compile(
            trace, case_id="TC-SCENARIO-QUERY", title="scenario project-query"
        )
        assert candidate.goal_status == VERIFIED
        assert candidate.steps and candidate.assertions and candidate.oracle_bindings


def test_project_query_without_projects_cannot_verify(tmp_path: Path) -> None:
    """An empty list is a legitimate outcome: not verified, and no candidate."""
    scenario = default_registry().require("project-query")
    with _isolated_teamflow(tmp_path) as base:
        trace = _run_scenario(scenario, base_url=base, max_tool_calls=20)

        assert trace.goal_status in (NOT_VERIFIED, INCONCLUSIVE), trace.goal_reason
        assert trace.goal_status != VERIFIED
        # It must not have wandered onto a detail page that does not exist.
        assert all(
            not observation["url_path"]
            or not re.fullmatch(r"/projects/[^/]+", str(observation["url_path"]))
            for observation in trace.observations
        )
        # The click that had nothing to click is recorded as an error, not as a
        # silent success.
        assert any(
            entry.get("result") == "ERROR" and entry.get("action") == "click"
            for entry in trace.agent_decisions
        )
        with pytest.raises(ValidationError):
            TestCaseCompiler().compile(trace, case_id="TC-QUERY-EMPTY", title="empty list")


def test_page_navigation_reaches_the_create_form(tmp_path: Path) -> None:
    """Login -> click the real nav link -> /projects/new, then compile."""
    scenario = default_registry().require("page-navigation")
    with _isolated_teamflow(tmp_path) as base:
        _create_project(base, "导航起点项目")

        trace = _run_scenario(scenario, base_url=base)

        assert trace.goal_status == VERIFIED, trace.goal_reason
        final = trace.observations[-1]
        assert final["url_path"] == "/projects/new"
        assert "新建项目" in str(final["title"] or "")
        assert "project-name" in set(final["visible_testids"])
        assert "create-submit" in set(final["visible_testids"])
        # Navigated by a real page element, not by typing the URL.
        click = [
            entry
            for entry in trace.action_sequence
            if entry.get("tool") == "click"
            and "new-project-link" in str(entry.get("args", {}).get("selector", ""))
        ]
        assert click, trace.action_sequence

        candidate = TestCaseCompiler().compile(
            trace, case_id="TC-SCENARIO-NAV", title="scenario page-navigation"
        )
        assert candidate.goal_status == VERIFIED
        assert candidate.steps and candidate.assertions and candidate.oracle_bindings


def test_unexpected_page_stops_the_plan_instead_of_verifying(tmp_path: Path) -> None:
    """Dynamic deviation: the page moves somewhere the plan did not predict.

    The inserted decision logs out, so the page becomes ``/login`` while the next
    pre-scripted decision was written for ``/projects``. The Explorer must stop
    there — not click blindly on a page it never saw, and above all not produce a
    VERIFIED candidate out of a journey that never reached its goal.
    """
    import json

    from packages.agents.scripted_agent import AgentStep

    scenario = default_registry().require("page-navigation")

    with _isolated_teamflow(tmp_path) as base:
        _create_project(base, "偏离测试项目")
        planned = scenario_steps(
            scenario, base_url=base, username="tester", password="test-pass-123"
        )
        logout = AgentStep(
            decision=json.dumps(
                {
                    "action": "click",
                    # The nav's last link is 退出; attribute selectors carrying a
                    # "/" are refused by the selector policy, and this form says
                    # the same thing without needing one.
                    "args": {"selector": "nav a:last-child"},
                    "rationale": "leave the page on purpose, deviating from the plan",
                }
            ),
            expect_url_path="/projects",
        )
        deviated = planned[:5] + [logout] + planned[5:]

        trace = _run_scenario(scenario, base_url=base, steps=deviated, max_tool_calls=20)

        assert trace.goal_status != VERIFIED, trace.goal_reason
        assert trace.goal_status == NOT_VERIFIED, trace.goal_reason
        # Stopped by the policy, not by running out of plan.
        assert trace.stop_reason == "POLICY_STOP", trace.stop_reason
        errors = [d for d in trace.agent_decisions if d.get("result") == "ERROR"]
        assert errors and "drift" in str(errors[-1].get("error", "")).lower()
        # The pre-scripted tail (click new-project-link, snapshot) never ran.
        assert trace.action_sequence, "the login journey should have executed"
        last = trace.action_sequence[-1]
        assert last.get("tool") == "click"
        assert last.get("args", {}).get("selector") == "nav a:last-child"
        assert not any(
            "new-project-link" in str(entry.get("args", {}).get("selector", ""))
            for entry in trace.action_sequence
        )
        with pytest.raises(ValidationError):
            TestCaseCompiler().compile(trace, case_id="TC-DRIFT", title="deviated")


def test_project_edit_paths_run_through_api_and_cleanup_owned_data(tmp_path: Path) -> None:
    scenario = default_registry().require("project-edit", "2.0.0")
    database_path = (tmp_path / "teamflow.db").as_posix()
    with _isolated_teamflow(tmp_path) as base:
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{database_path}",
            exploration_data_dir=str(tmp_path / "explorations"),
            candidate_data_dir=str(tmp_path / "candidates"),
            case_dir=str(tmp_path / "cases"),
            benchmark_data_dir=str(tmp_path / "benchmark"),
            chain_data_dir=str(tmp_path / "chains"),
            blob_dir=str(tmp_path / "blobs"),
        )
        with TestClient(build_platform_app(settings)) as client:
            results: dict[str, dict[str, Any]] = {}
            for path_kind in ("normal", "exception", "boundary", "recovery"):
                response = client.post(
                    "/api/v1/explorations",
                    json={
                        "scenario": scenario.scenario_id,
                        "scenario_version": scenario.version,
                        "path": path_kind,
                        "goal": "Edit the seeded project and verify the saved result",
                        "target": "/projects/*",
                        "base_url": base,
                        "policy": "scripted",
                        "max_steps": 20,
                    },
                )
                assert response.status_code == 201, response.text
                record = response.json()
                results[path_kind] = record
                assert record["scenario_version"] == "2.0.0"
                assert record["scenario_digest"] == scenario.digest
                assert record["data_lifecycle"]["state"] == "CLEANED"
                from apps.teamflow.models import Project
                from packages.core import database

                session_factory = database.make_session_factory(client.app.state.engine)
                with session_factory() as session:
                    assert not any(
                        project.name.startswith("TG_RUN_")
                        for project in session.query(Project).all()
                    )

            normal = results["normal"]
            assert normal["goal_status"] == VERIFIED, normal["goal_reason"]
            assert normal["goal_assertions"]["verdict"] == VERIFIED
            candidate = client.post(
                f"/api/v1/explorations/{normal['exploration_id']}/compile",
                json={"case_id": "TC-PROJECT-EDIT", "title": "Project edit normal path"},
            )
            assert candidate.status_code == 201, candidate.text
            assert candidate.json()["status"] == "PENDING_REVIEW"

            exception = results["exception"]
            assert any(
                "form-error" in observation.get("visible_testids", [])
                for observation in exception.get("observations", [])
            )
            recovery = results["recovery"]
            assert recovery["goal_assertions"]["verdict"] == VERIFIED


def test_form_validation_paths_repeat_with_run_scoped_seed_cleanup(tmp_path: Path) -> None:
    scenario = default_registry().require("form-validation")
    database_path = (tmp_path / "teamflow.db").as_posix()
    with _isolated_teamflow(tmp_path) as base:
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{database_path}",
            exploration_data_dir=str(tmp_path / "explorations"),
            candidate_data_dir=str(tmp_path / "candidates"),
            case_dir=str(tmp_path / "cases"),
            benchmark_data_dir=str(tmp_path / "benchmark"),
            chain_data_dir=str(tmp_path / "chains"),
            blob_dir=str(tmp_path / "blobs"),
        )
        with TestClient(build_platform_app(settings)) as client:
            from apps.teamflow.models import Project
            from packages.core import database

            session_factory = database.make_session_factory(client.app.state.engine)
            for path_kind in ("normal", "exception", "boundary", "recovery", "normal"):
                response = client.post(
                    "/api/v1/explorations",
                    json={
                        "scenario": scenario.scenario_id,
                        "scenario_version": scenario.version,
                        "path": path_kind,
                        "goal": "Validate project form input and show the correct result",
                        "target": "/projects/new",
                        "expected_outcomes": ["project name already exists"],
                        "base_url": base,
                        "policy": "scripted",
                        "max_steps": 20,
                    },
                )
                assert response.status_code == 201, response.text
                record = response.json()
                assert record["scenario_path"] == path_kind
                assert record["data_lifecycle"]["state"] == "CLEANED"
                visible = {
                    testid
                    for observation in record.get("observations", [])
                    for testid in observation.get("visible_testids", [])
                }
                if path_kind in {"normal", "exception", "recovery"}:
                    assert "form-error" in visible
                if path_kind in {"boundary", "recovery"}:
                    assert "success-message" in visible
                with session_factory() as session:
                    assert not any(
                        project.name.startswith("TG_RUN_")
                        for project in session.query(Project).all()
                    )


def test_four_path_matrix_for_existing_scenarios_through_real_browser_api(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "teamflow.db").as_posix()
    with _isolated_teamflow(tmp_path) as base:
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{database_path}",
            exploration_data_dir=str(tmp_path / "explorations"),
            candidate_data_dir=str(tmp_path / "candidates"),
            case_dir=str(tmp_path / "cases"),
            benchmark_data_dir=str(tmp_path / "benchmark"),
            chain_data_dir=str(tmp_path / "chains"),
            blob_dir=str(tmp_path / "blobs"),
        )
        with TestClient(build_platform_app(settings)) as client:
            registry = default_registry()
            for scenario_id in (
                "teamflow-login",
                "project-create",
                "project-query",
                "page-navigation",
            ):
                scenario = registry.require(scenario_id)
                templates = scenario.intent_templates
                for path_kind in ("normal", "exception", "boundary", "recovery"):
                    response = client.post(
                        "/api/v1/explorations",
                        json={
                            "scenario": scenario.scenario_id,
                            "scenario_version": scenario.version,
                            "path": path_kind,
                            "goal": templates.get("goal", scenario.description),
                            "target": templates.get("target", ""),
                            "expected_outcomes": templates.get("expected_outcomes", []),
                            "required_assertions": templates.get("required_assertions", []),
                            "base_url": base,
                            "policy": "scripted",
                            "max_steps": scenario.max_steps,
                        },
                    )
                    assert response.status_code == 201, response.text
                    record = response.json()
                    assert record["scenario_path"] == path_kind
                    assert record["scenario_version"] == scenario.version
                    assert record["action_sequence"]
                    if path_kind in {"normal", "recovery"}:
                        assert record["goal_status"] == VERIFIED, record["goal_reason"]
                    if path_kind == "exception":
                        assert record["goal_status"] != VERIFIED
                    reread = client.get(f"/api/v1/explorations/{record['exploration_id']}")
                    assert reread.status_code == 200
                    assert reread.json()["scenario_digest_verified"] is True


def test_api_test_case_verdict_distinguishes_expected_negative_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apps.api.explorer_api as explorer_api

    scenario = default_registry().require("teamflow-login", "2.0.0")
    with _isolated_teamflow(tmp_path) as base:
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{(tmp_path / 'teamflow.db').as_posix()}",
            exploration_data_dir=str(tmp_path / "explorations"),
            candidate_data_dir=str(tmp_path / "candidates"),
            case_dir=str(tmp_path / "cases"),
            benchmark_data_dir=str(tmp_path / "benchmark"),
            chain_data_dir=str(tmp_path / "chains"),
            blob_dir=str(tmp_path / "blobs"),
        )
        with TestClient(build_platform_app(settings)) as client:
            request = {
                "scenario": scenario.scenario_id,
                "scenario_version": scenario.version,
                "goal": "Log in and reach the project list",
                "target": "/projects",
                "expected_outcomes": ["项目列表"],
                "base_url": f"{base}/?access_token=secret123#password=abc",
                "policy": "scripted",
                "max_steps": 8,
            }
            normal = client.post("/api/v1/explorations", json={**request, "path": "normal"}).json()
            assert normal["goal_status"] == "VERIFIED"
            assert normal["test_case_verdict"] == "PASSED"
            leaked_fields = {
                key: str(value)
                for key, value in normal.items()
                if "secret123" in str(value) or "password=abc" in str(value)
            }
            assert not leaked_fields, leaked_fields
            reread = client.get(f"/api/v1/explorations/{normal['exploration_id']}").json()
            assert reread["test_case_verdict"] == "PASSED"
            assert reread["trace_integrity_verified"] is True

            negative = client.post(
                "/api/v1/explorations", json={**request, "path": "exception"}
            ).json()
            assert negative["goal_status"] == "NOT_VERIFIED"
            assert negative["path_expect"]["verdict"] == "VERIFIED"
            assert negative["test_case_verdict"] == "PASSED"

            # A valid credential on the negative path is a real regression.
            path_specs = {key: dict(value) for key, value in scenario.paths.items()}
            exception_spec = dict(path_specs["exception"])
            exception_spec["steps"] = [
                {
                    **step,
                    "args": {
                        **dict(step.get("args") or {}),
                        **(
                            {"value": "{password}"}
                            if step.get("tool") == "fill"
                            and step.get("args", {}).get("selector") == "[data-testid=password]"
                            else {}
                        ),
                    },
                }
                for step in exception_spec["steps"]
            ]
            path_specs["exception"] = exception_spec
            wrong_success = replace(scenario, paths=path_specs)
            original_get = explorer_api.SCENARIOS.get
            monkeypatch.setattr(
                explorer_api.SCENARIOS,
                "get",
                lambda sid, version=None: (
                    wrong_success if sid == scenario.scenario_id else original_get(sid, version)
                ),
            )
            regression = client.post(
                "/api/v1/explorations", json={**request, "path": "exception"}
            ).json()
            assert regression["goal_status"] == "VERIFIED"
            assert regression["test_case_verdict"] == "FAILED"


def test_mock_agent_runs_real_api_browser_authorization_and_post_action(
    tmp_path: Path,
) -> None:
    with _isolated_teamflow(tmp_path) as base:
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{(tmp_path / 'teamflow.db').as_posix()}",
            exploration_data_dir=str(tmp_path / "explorations"),
            candidate_data_dir=str(tmp_path / "candidates"),
            case_dir=str(tmp_path / "cases"),
            benchmark_data_dir=str(tmp_path / "benchmark"),
            chain_data_dir=str(tmp_path / "chains"),
            blob_dir=str(tmp_path / "blobs"),
        )
        with TestClient(build_platform_app(settings)) as client:
            catalogue = client.get("/api/v1/explorations/policies").json()
            mock_policy = next(p for p in catalogue["policies"] if p["id"] == "mock-agent")
            assert mock_policy["verified"] is True
            assert mock_policy["is_real_model"] is False
            response = client.post(
                "/api/v1/explorations",
                json={
                    "scenario": "teamflow-login",
                    "scenario_version": "2.0.0",
                    "path": "normal",
                    "goal": "Log in and reach the project list",
                    "target": "/projects",
                    "expected_outcomes": ["项目列表"],
                    "base_url": base,
                    "policy": "mock-agent",
                    "max_steps": 8,
                },
            )
            assert response.status_code == 201, response.text
            trace = response.json()
            assert trace["status"] == "COMPLETED"
            assert trace["goal_status"] == "VERIFIED"
            assert trace["test_case_verdict"] == "PASSED"
            assert trace["is_real_model"] is False
            assert trace["authorization_events"]
            assert all(item["consumed"] for item in trace["authorization_events"])
            assert len(trace["authorization_events"]) == len(trace["action_sequence"])
            assert trace["post_action_records"]
            assert any(item["page_changed"] for item in trace["post_action_records"])
            assert trace["decision_records"]
            assert all(item["decision_source"] == "mock" for item in trace["decision_records"])
            assert len({item["observation_ref"] for item in trace["decision_records"]}) > 1
            serialized = str(trace)
            assert settings.teamflow_test_password not in serialized
