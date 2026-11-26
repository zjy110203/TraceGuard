from __future__ import annotations

import pytest

from packages.agents.exploration import Observation
from packages.agents.goal_assertions import (
    DECLARED_KINDS,
    SUPPORTED_KINDS,
    evaluate_assertions,
    evaluate_test_case_verdict,
    resolve_evidence_reference,
    validate_assertion,
)
from packages.agents.goal_assertions import (
    TestCaseVerdict as CaseVerdict,
)
from packages.agents.scenarios import Scenario, ScenarioRegistry, default_registry

OBS = {
    "url": "http://localhost/projects/42",
    "url_path": "/projects/42",
    "title": "Project detail",
    "text_excerpt": "Project Alpha saved",
    "visible_testids": ["project-link"],
    "visible_elements": [
        {"testid": "project-link", "tag": "a", "role": "link", "href": "/projects/42"}
    ],
    "dom_counts": {"table_row_count": 2, "list_item_count": 3},
    "dom_relations": [{"relation": "row_in_table", "testid": "row-1"}],
    "page_state": "loaded",
    "previous_url_path": "/projects",
    "evidence_ref": "ev-source-1",
    "observation_ref": "obs-source-1",
}
OBS["observations"] = [dict(OBS)]


@pytest.mark.parametrize(
    "spec",
    [{"kind": "table_row_count", "name": f"rows-{n}", "value": 2} for n in range(3)]
    + [{"kind": "list_item_count", "name": f"items-{n}", "value": 3} for n in range(3)]
    + [
        {
            "kind": "element_attr",
            "name": f"href-{n}",
            "testid": "project-link",
            "attribute": "href",
            "value": "/projects/42",
        }
        for n in range(3)
    ]
    + [
        {
            "kind": "element_relation",
            "name": f"relation-{n}",
            "relation": "row_in_table",
            "testid": "row-1",
        }
        for n in range(3)
    ]
    + [
        {"kind": "page_transition", "name": f"transition-{n}", "to": "/projects/42"}
        for n in range(3)
    ]
    + [
        {"kind": "dynamic_match", "name": f"dynamic-{n}", "pattern": "Project \\w+ saved"}
        for n in range(3)
    ]
    + [
        {"kind": "evidence_complete", "name": f"evidence-{n}", "evidence_ref": "ev-source-1"}
        for n in range(3)
    ]
    + [
        {
            "kind": "multi_step_goal",
            "name": f"multi-{n}",
            "assertions": [
                {
                    "observation_ref": "obs-source-1",
                    "assertion": {"kind": "url_exact", "name": "landed", "value": "/projects/42"},
                }
            ],
        }
        for n in range(3)
    ],
)
def test_24_new_assertion_cases_are_supported(spec: dict[str, object]) -> None:
    report = evaluate_assertions([spec], OBS)
    assert report.verdict == "VERIFIED"
    result = report.results[0].to_dict()
    assert result["observation_ref"] == "obs-source-1"
    if spec["kind"] == "multi_step_goal":
        assert result["evidence_refs"] and all(
            ref.startswith("ev_") for ref in result["evidence_refs"]
        )
    else:
        assert result["evidence_ref"].startswith("ev_")


def test_o3_assertion_kind_cardinality_and_url_sugar() -> None:
    assert len(SUPPORTED_KINDS) == 19
    assert len(DECLARED_KINDS) == 20
    assert (
        validate_assertion(
            {"kind": "url_and_content", "name": "both", "url": "/projects/42", "contains": "Alpha"}
        )
        == []
    )
    report = evaluate_assertions(
        [{"kind": "url_and_content", "name": "both", "url": "/projects/42", "contains": "Alpha"}],
        OBS,
    )
    assert report.verdict == "VERIFIED"


def test_url_and_content_is_expanded_when_scenario_registers() -> None:
    base = default_registry().require("teamflow-login")
    scenario = Scenario(
        **{
            **base.__dict__,
            "scenario_id": "url-sugar-test",
            "goal_definition": (
                {
                    "kind": "url_and_content",
                    "name": "both",
                    "url": "/projects",
                    "contains": "Projects",
                },
            ),
        }
    )
    registered = ScenarioRegistry().register(scenario)
    assertion = registered.goal_definition[0]
    assert assertion["kind"] == "all_of"
    assert [child["kind"] for child in assertion["assertions"]] == ["url_exact", "page_state"]


def test_observation_v2_is_bounded_and_masks_sensitive_attributes() -> None:
    record = Observation(
        url="http://localhost/form",
        page_text="x" * 4000,
        visible_elements=tuple(
            {"tag": "input", "testid": f"field-{i}", "name": "safe", "value": "secret"}
            for i in range(50)
        )
        + ({"tag": "input", "testid": "password", "name": "password", "type": "password"},),
        dom_counts={"table_row_count": 10001},
        attrs=({"testid": "password", "value": "secret"},),
    ).to_record(max_text=5000)
    assert len(record["text_excerpt"]) <= 3000
    assert len(record["visible_elements"]) == 40
    assert record["dom_counts"]["table_row_count"] == 10001
    assert all("value" not in item for item in record["visible_elements"])
    assert "secret" not in str(record)
    assert record["observation_ref"].startswith("obs_")


def test_stable_evidence_reference_resolves_and_detects_tampering() -> None:
    observation = Observation(
        url="http://localhost/projects/42",
        page_text="saved",
        visible_testids=("project-link",),
        step_index=2,
        visible_elements=({"tag": "a", "testid": "project-link", "href": "/projects/42"},),
    ).to_record()
    report = evaluate_assertions(
        [
            {
                "kind": "element_attr",
                "name": "project-link",
                "testid": "project-link",
                "attribute": "href",
                "value": "/projects/42",
            }
        ],
        observation,
    )
    result = report.results[0]
    resolved = resolve_evidence_reference(
        [observation],
        step_id=result.step_id,
        observation_ref=result.observation_ref,
        field_path=result.field_path,
        evidence_ref=result.evidence_ref,
    )
    assert resolved == "/projects/42"
    altered = dict(observation)
    altered["visible_elements"] = [{"tag": "a", "testid": "project-link", "href": "/evil"}]
    with pytest.raises(ValueError, match="stable reference"):
        resolve_evidence_reference(
            [altered],
            step_id=result.step_id,
            observation_ref=result.observation_ref,
            field_path=result.field_path,
            evidence_ref=result.evidence_ref,
        )


@pytest.mark.parametrize(
    "goal,expected,assertions,want",
    [
        ("VERIFIED", "NOT_VERIFIED", ["PASSED"], CaseVerdict.FAILED),
        ("VERIFIED", "VERIFIED", ["FAILED"], CaseVerdict.FAILED),
        ("VERIFIED", "VERIFIED", ["NOT_EVALUATED"], CaseVerdict.INCONCLUSIVE),
        ("VERIFIED", "VERIFIED", ["PASSED", "NOT_EVALUATED"], CaseVerdict.INCONCLUSIVE),
        ("VERIFIED", "VERIFIED", ["FAILED", "NOT_EVALUATED"], CaseVerdict.FAILED),
        ("VERIFIED", "VERIFIED", ["INCONCLUSIVE"], CaseVerdict.INCONCLUSIVE),
        ("VERIFIED", "VERIFIED", ["PASSED"], CaseVerdict.PASSED),
    ],
)
def test_test_case_verdict_priority_matrix(
    goal: str, expected: str, assertions: list[str], want: CaseVerdict
) -> None:
    assert (
        evaluate_test_case_verdict(
            goal_status=goal, expected_goal_status=expected, required_expect_verdicts=assertions
        )
        is want
    )
