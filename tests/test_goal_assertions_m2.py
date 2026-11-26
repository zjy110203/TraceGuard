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
        ("NOT_EVALUATED", "VERIFIED", ["PASSED"], CaseVerdict.INCONCLUSIVE),
        ("NOT_EVALUATED", "VERIFIED", ["FAILED"], CaseVerdict.FAILED),
        ("NOT_VERIFIED", "NOT_VERIFIED", ["PASSED"], CaseVerdict.PASSED),
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


def test_missing_relation_and_evidence_capture_are_inconclusive() -> None:
    report = evaluate_assertions(
        [
            {
                "name": "relation",
                "kind": "element_relation",
                "relation": "row_in_table",
                "testid": "project-row",
            },
            {"name": "evidence", "kind": "evidence_complete", "evidence_ref": "ev-1"},
        ],
        {"url_path": "/projects", "observation_ref": "obs-1"},
    )
    assert report.verdict == "INCONCLUSIVE"
    assert [result.verdict for result in report.results] == ["INCONCLUSIVE", "INCONCLUSIVE"]


@pytest.mark.parametrize(
    "child",
    [
        {"kind": "page_state", "field": "text", "value": "missing-marker"},
        {"kind": "dynamic_match", "pattern": "missing-marker"},
    ],
)
def test_truncated_page_cannot_prove_negated_absence(child: dict[str, object]) -> None:
    child = {**child, "name": "truncated-child"}
    observation = {**OBS, "text_excerpt": "x" * 3000}
    direct = evaluate_assertions([child], observation)
    assert direct.verdict == "INCONCLUSIVE"
    negated = evaluate_assertions(
        [{"kind": "not", "name": "negated", "assertions": [child]}], observation
    )
    assert negated.verdict == "INCONCLUSIVE"
    # Positive observations remain useful; structure is independent of excerpt length.
    structured = evaluate_assertions(
        [
            {
                "kind": "element_attr",
                "name": "structured",
                "testid": "project-link",
                "attribute": "href",
                "value": "/projects/42",
            }
        ],
        observation,
    )
    assert structured.verdict == "VERIFIED"


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "url_exact", "value": "/projects"},
        {"kind": "url_regex", "value": ".*"},
        {"kind": "page_state", "field": "title", "value": "Project"},
        {"kind": "page_state", "field": "text", "value": "Project"},
        {"kind": "dynamic_match", "pattern": ".*"},
    ],
)
def test_missing_specific_observation_field_is_inconclusive(spec: dict[str, object]) -> None:
    spec = {**spec, "name": "missing-field"}
    observation = {"visible_testids": ["unrelated"]}
    assert evaluate_assertions([spec], observation).verdict == "INCONCLUSIVE"
    assert (
        evaluate_assertions(
            [{"kind": "not", "name": "negative", "assertions": [spec]}], observation
        ).verdict
        == "INCONCLUSIVE"
    )


def test_regex_end_anchor_cannot_treat_excerpt_end_as_page_end() -> None:
    report = evaluate_assertions(
        [{"kind": "dynamic_match", "name": "end", "pattern": "x$"}],
        {**OBS, "text_excerpt": "x" * 3000},
    )
    assert report.verdict == "INCONCLUSIVE"


@pytest.mark.parametrize("reverse", [False, True])
def test_multistep_reference_order_is_enforced(reverse: bool) -> None:
    observations = [
        {**OBS, "observation_ref": "first", "url_path": "/error"},
        {**OBS, "observation_ref": "second", "url_path": "/success"},
    ]
    steps = [
        {
            "observation_ref": "first",
            "assertion": {"kind": "url_exact", "name": "error", "value": "/error"},
        },
        {
            "observation_ref": "second",
            "assertion": {"kind": "url_exact", "name": "success", "value": "/success"},
        },
    ]
    if reverse:
        steps.reverse()
    result = evaluate_assertions(
        [{"kind": "multi_step_goal", "name": "ordered", "assertions": steps}],
        observations[-1],
        observations=observations,
    )
    assert result.verdict == ("NOT_VERIFIED" if reverse else "VERIFIED")


def test_multistep_match_requires_actual_error_then_recovery() -> None:
    spec = {
        "kind": "multi_step_goal",
        "name": "recovery",
        "assertions": [
            {
                "match": "first_after_previous",
                "assertion": {"kind": "url_exact", "name": "error", "value": "/error"},
            },
            {
                "match": "first_after_previous",
                "assertion": {"kind": "url_exact", "name": "success", "value": "/success"},
            },
        ],
    }
    observations = [
        {**OBS, "observation_ref": "a", "url_path": "/error"},
        {**OBS, "observation_ref": "b", "url_path": "/success"},
    ]
    report = evaluate_assertions([spec], observations[-1], observations=observations)
    assert report.verdict == "VERIFIED"
    assert len(set(report.results[0].evidence_refs)) == 2
    # A success alone or observations in the wrong order cannot prove recovery.
    assert (
        evaluate_assertions([spec], observations[-1], observations=observations[1:]).verdict
        == "INCONCLUSIVE"
    )
    assert (
        evaluate_assertions([spec], observations[0], observations=observations[::-1]).verdict
        == "INCONCLUSIVE"
    )


@pytest.mark.parametrize(
    "child",
    [
        {"kind": "element_exists", "testid": "outside-sample"},
        {"kind": "element_visible", "testid": "outside-sample"},
        {"kind": "element_text", "testid": "outside-sample", "contains": "expected"},
        {
            "kind": "element_attr",
            "testid": "outside-sample",
            "attribute": "href",
            "value": "/expected",
        },
        {"kind": "form_state", "testid": "outside-sample", "state": "absent"},
    ],
)
def test_bounded_element_sample_does_not_prove_absence(child: dict[str, object]) -> None:
    child = {**child, "name": "bounded"}
    observation = {
        **OBS,
        "visible_elements": [{"testid": f"row-{i}", "tag": "a"} for i in range(40)],
    }
    assert evaluate_assertions([child], observation).verdict == "INCONCLUSIVE"
    assert (
        evaluate_assertions(
            [{"kind": "not", "name": "negated", "assertions": [child]}], observation
        ).verdict
        == "INCONCLUSIVE"
    )


@pytest.mark.parametrize("composite", [False, True])
def test_structured_count_and_composite_evidence_resolves(composite: bool) -> None:
    import hashlib
    import json

    selector = 'table[data-testid="rows"] tr.active'
    observation = {
        "url_path": "/projects",
        "step_id": "step-1",
        "visible_testids": ["rows"],
        "selector_counts": {selector: 2},
        "dom_counts": {"table_row_count": 99},
    }
    canonical = json.dumps(observation, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    observation["observation_ref"] = "obs_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
    predicate = {"kind": "table_row_count", "name": "selected", "selector": selector, "value": 2}
    if composite:
        predicate = {"kind": "all_of", "name": "composite", "assertions": [predicate]}
        report = evaluate_assertions(
            [
                {
                    "kind": "multi_step_goal",
                    "name": "sequence",
                    "assertions": [
                        {"observation_ref": observation["observation_ref"], "assertion": predicate}
                    ],
                }
            ],
            observation,
            observations=[observation],
        )
        reference = report.results[0].evidence_refs[0]
        field_path = "composite"
    else:
        report = evaluate_assertions([predicate], observation)
        reference = report.results[0].evidence_ref
        field_path = report.results[0].field_path
    assert report.verdict == "VERIFIED"
    resolved = resolve_evidence_reference(
        [observation],
        step_id="step-1",
        observation_ref=observation["observation_ref"],
        field_path=field_path,
        evidence_ref=reference,
    )
    assert resolved == (observation if composite else 2)
    altered = {**observation, "selector_counts": {selector: 3}}
    with pytest.raises(ValueError, match="stable reference"):
        resolve_evidence_reference(
            [altered],
            step_id="step-1",
            observation_ref=observation["observation_ref"],
            field_path=field_path,
            evidence_ref=reference,
        )
