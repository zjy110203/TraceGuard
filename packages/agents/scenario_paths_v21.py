"""Explicitly selected path Oracles, approved by the user in this task.

Historical 2.0.0 packages and default selection are unchanged. No formal
TestCase/Oracle is rewritten. These are deterministic browser scenarios.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packages.agents.scenarios import ScenarioRegistry


def _testid(value: str) -> dict[str, Any]:
    return {"kind": "testid", "name": f"has_{value}", "value": value, "required": True}


def _text(testid: str, contains: str) -> dict[str, Any]:
    return {
        "kind": "element_text",
        "name": f"text_{testid}",
        "testid": testid,
        "contains": contains,
        "required": True,
    }


def add_opt_in_versions(registry: ScenarioRegistry) -> None:
    for original in tuple(registry.all()):
        paths = deepcopy(original.paths or {})
        for kind, spec in paths.items():
            goals = deepcopy(list(original.goal_definition))
            intent = deepcopy(original.intent_templates)
            expect = [deepcopy(a) for a in goals if a.get("required", True)]
            expected = "VERIFIED"
            sid = original.scenario_id
            if kind in {"exception", "boundary"} and sid == "teamflow-login":
                expected = "NOT_VERIFIED"
                expect = [_testid("login-error")]
            elif kind == "exception" and sid in {"project-create", "project-edit"}:
                expected = "NOT_VERIFIED"
                message = "already exists" if sid == "project-create" else "must not be empty"
                expect = [_testid("form-error"), _text("form-error", message)]
            elif sid == "project-query" and kind == "exception":
                expected = "NOT_VERIFIED"
                # This endpoint renders a JSON error, hence no sampled testid.
                # URL + response body are independently captured by the browser.
                expect = [
                    {
                        "kind": "url_exact",
                        "name": "missing_project_route",
                        "value": "/projects/does-not-exist",
                        "required": True,
                    },
                    {
                        "kind": "page_state",
                        "name": "not_found_response",
                        "field": "text",
                        "value": '"NOT_FOUND"',
                        "required": True,
                    },
                ]
            elif sid == "page-navigation" and kind == "exception":
                expected = "NOT_VERIFIED"
                expect = [
                    _testid("project-list"),
                    {
                        "kind": "url_exact",
                        "name": "remained_on_list",
                        "value": "/projects",
                        "required": True,
                    },
                ]
            elif sid == "form-validation":
                if kind in {"normal", "exception"}:
                    message = "already exists" if kind == "normal" else "must not be empty"
                    goals = [_testid("form-error"), _text("form-error", message)]
                    intent.update(target="/projects/new", expected_outcomes=[])
                else:
                    goals = [_testid("success-message"), _testid("project-list")]
                    intent.update(target="/projects", expected_outcomes=[])
                expect = deepcopy(goals)
            if kind == "recovery":
                if sid == "teamflow-login":
                    initial = _testid("login-error")
                elif sid == "project-query":
                    initial = {
                        "kind": "page_state",
                        "name": "initial_not_found",
                        "field": "text",
                        "value": '"NOT_FOUND"',
                        "required": True,
                    }
                elif sid == "page-navigation":
                    initial = _testid("project-list")
                else:
                    initial = _testid("form-error")
                expect.append(
                    {
                        "kind": "multi_step_goal",
                        "name": "observed_recovery_sequence",
                        "required": True,
                        "assertions": [
                            {"match": "first_after_previous", "assertion": initial},
                            {
                                "match": "first_after_previous",
                                "assertion": {
                                    "kind": "all_of",
                                    "name": "recovered_state",
                                    "assertions": deepcopy(expect),
                                },
                            },
                        ],
                    }
                )
            spec.update(
                expected_goal_status=expected,
                expect=expect,
                goal_definition=goals,
                intent_templates=intent,
            )
        registry.register(replace(original, version="2.1.0", paths=paths), explicit_only=True)
