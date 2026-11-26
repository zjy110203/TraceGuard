"""Structured, observable goal assertions (R5-5.2 §15).

Why this exists
---------------
R5-4.6 verified a goal by asking "does the final ``text_excerpt`` contain this
string?". That is a single, blunt check: it cannot express "we are on
``/projects`` **and** the project row exists **and** the title says Project
List", and it silently treats a typo'd string as a genuine failure.

This module turns a goal into a list of independent assertions. Each one is
evaluated against a real page observation and gets its own verdict:

* ``PASSED``       — observed and true;
* ``FAILED``       — observed and false;
* ``INCONCLUSIVE`` — the observation cannot answer the question (no DOM sample,
  element text not captured, ...). An unobserved thing is never a failure.

Contract rules
--------------
* Every assertion carries the evidence path it read (``observations[-1].url``,
  ``...visible_testids``, ...), so a reviewer can re-check it.
* A goal is ``VERIFIED`` only when every **required** assertion PASSED and no
  required assertion is INCONCLUSIVE. Optional assertions are reported but can
  never turn a pass into a fail.
* Nothing here reads input values: the observation adapter never captures them
  (a password is not an observation). ``form_state`` therefore only reports
  presence / visibility / disabled-ness, never the value.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

PASSED = "PASSED"
FAILED = "FAILED"
INCONCLUSIVE = "INCONCLUSIVE"
VERIFIED = "VERIFIED"
NOT_VERIFIED = "NOT_VERIFIED"

# Kinds an author may declare. Anything else is a definition error, not a
# silently ignored assertion (a typo must not weaken a goal).
SUPPORTED_KINDS: tuple[str, ...] = (
    "url_exact",
    "url_regex",
    "testid",
    "element_exists",
    "element_visible",
    "element_text",
    "form_state",
    "page_state",
    "all_of",
    "any_of",
    "not",
    "table_row_count",
    "list_item_count",
    "element_attr",
    "element_relation",
    "page_transition",
    "multi_step_goal",
    "dynamic_match",
    "evidence_complete",
)
DECLARED_KINDS = (*SUPPORTED_KINDS, "url_and_content")


def expand_assertion_sugar(spec: dict[str, Any]) -> dict[str, Any]:
    """Expand declaration sugar once when a scenario is registered."""
    if spec.get("kind") == "url_and_content":
        return {
            "kind": "all_of",
            "name": spec.get("name", "url_and_content"),
            "required": spec.get("required", True),
            "assertions": [
                {"kind": "url_exact", "name": "url", "value": spec.get("url", "")},
                {
                    "kind": "page_state",
                    "name": "content",
                    "field": "text",
                    "value": spec.get("contains", ""),
                },
            ],
        }
    result = dict(spec)
    if isinstance(result.get("assertions"), list):
        result["assertions"] = [
            expand_assertion_sugar(child)
            for child in result["assertions"]
            if isinstance(child, dict)
        ]
    return result


class AssertionDefinitionError(ValueError):
    """The assertion itself is malformed (author error, not a page verdict)."""


class TestCaseVerdict(StrEnum):
    """Business verdict, distinct from execution-layer TestVerdict."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


def evaluate_test_case_verdict(
    *, goal_status: str, expected_goal_status: str, required_expect_verdicts: list[str]
) -> TestCaseVerdict:
    if expected_goal_status not in {VERIFIED, NOT_VERIFIED}:
        return TestCaseVerdict.INCONCLUSIVE
    if FAILED in required_expect_verdicts:
        return TestCaseVerdict.FAILED
    if goal_status == "NOT_EVALUATED":
        return TestCaseVerdict.INCONCLUSIVE
    if goal_status not in {VERIFIED, NOT_VERIFIED, INCONCLUSIVE}:
        return TestCaseVerdict.INCONCLUSIVE
    if goal_status != expected_goal_status:
        return TestCaseVerdict.FAILED
    if not required_expect_verdicts or INCONCLUSIVE in required_expect_verdicts:
        return TestCaseVerdict.INCONCLUSIVE
    if all(v == PASSED for v in required_expect_verdicts):
        return TestCaseVerdict.PASSED
    return TestCaseVerdict.INCONCLUSIVE


@dataclass(frozen=True)
class AssertionResult:
    name: str
    kind: str
    verdict: str
    expected: str
    observed: str
    evidence: str
    required: bool = True
    detail: str = ""
    observation_ref: str = ""
    evidence_ref: str = ""
    field_path: str = ""
    step_id: str = ""
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "verdict": self.verdict,
            "expected": self.expected,
            "observed": self.observed,
            "evidence": self.evidence,
            "required": self.required,
            "detail": self.detail,
            "observation_ref": self.observation_ref,
            "evidence_ref": self.evidence_ref,
            "field_path": self.field_path,
            "step_id": self.step_id,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class AssertionReport:
    verdict: str
    results: tuple[AssertionResult, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "results": [r.to_dict() for r in self.results],
            "required_failed": [r.name for r in self.results if r.required and r.verdict == FAILED],
            "required_inconclusive": [
                r.name for r in self.results if r.required and r.verdict == INCONCLUSIVE
            ],
            "optional_failed": [
                r.name for r in self.results if not r.required and r.verdict == FAILED
            ],
        }


@dataclass(frozen=True)
class _ObservationView:
    """Read-only, defensive view over one persisted observation dict."""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return str(self.raw.get("url") or "")

    @property
    def url_path(self) -> str:
        return str(self.raw.get("url_path") or "")

    @property
    def title(self) -> str:
        return str(self.raw.get("title") or "")

    @property
    def text(self) -> str:
        return str(self.raw.get("text_excerpt") or "")

    @property
    def testids(self) -> tuple[str, ...]:
        return tuple(str(t) for t in (self.raw.get("visible_testids") or ()))

    @property
    def elements(self) -> tuple[dict[str, Any], ...]:
        return tuple(e for e in (self.raw.get("visible_elements") or ()) if isinstance(e, dict))

    def element(self, testid: str) -> dict[str, Any] | None:
        for element in self.elements:
            if str(element.get("testid") or "") == testid:
                attrs = self.raw.get("attrs") or []
                observed_attrs = next(
                    (
                        item
                        for item in attrs
                        if isinstance(item, dict) and str(item.get("testid") or "") == testid
                    ),
                    {},
                )
                return {**observed_attrs, **element}
        return None

    @property
    def sampled(self) -> bool:
        """Did the adapter actually capture structure for this page?"""
        return bool(
            self.testids
            or self.elements
            or self.raw.get("dom_counts")
            or self.raw.get("dom_relations")
        )

    @property
    def observation_ref(self) -> str:
        existing = str(self.raw.get("observation_ref") or "")
        if existing:
            return existing
        canonical = json.dumps(self.raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "obs_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]

    @property
    def step_id(self) -> str:
        return str(self.raw.get("step_id") or f"step-{self.raw.get('step_index', 0)}")


def resolve_evidence_reference(
    observations: list[dict[str, Any]],
    *,
    step_id: str,
    observation_ref: str,
    field_path: str,
    evidence_ref: str,
) -> Any:
    """Re-resolve a stable evidence reference and reject stale/tampered refs."""
    expected = (
        "ev_"
        + hashlib.sha256(f"{step_id}:{observation_ref}:{field_path}".encode()).hexdigest()[:24]
    )
    if expected != evidence_ref:
        raise ValueError("evidence reference digest mismatch")
    record = next(
        (
            row
            for row in observations
            if str(row.get("step_id") or f"step-{row.get('step_index', 0)}") == step_id
            and _record_observation_ref(row) == observation_ref
        ),
        None,
    )
    if record is None:
        raise ValueError("evidence observation reference is missing")
    declared_ref = str(record.get("observation_ref") or "")
    if declared_ref:
        content = dict(record)
        content.pop("observation_ref", None)
        canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        actual_ref = "obs_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]
        if actual_ref != declared_ref:
            raise ValueError("observation content no longer matches its stable reference")
    if field_path == "composite":
        # Composite predicates read the whole immutable observation. Preserve
        # their existing reference IDs while making that source resolvable.
        return dict(record)
    if field_path.startswith("selector_counts[") and field_path.endswith("]"):
        selector = json.loads(field_path[len("selector_counts[") : -1])
        counts = record.get("selector_counts") or {}
        if not isinstance(selector, str) or selector not in counts:
            raise ValueError("evidence selector count no longer resolves")
        return counts[selector]
    current: Any = record
    for component in field_path.split("."):
        if component.startswith("visible_elements[testid=") and component.endswith("]"):
            testid = component[len("visible_elements[testid=") : -1]
            current = next(
                (e for e in record.get("visible_elements", []) if e.get("testid") == testid),
                None,
            )
        elif isinstance(current, dict):
            current = current.get(component)
        else:
            current = None
        if current is None:
            raise ValueError("evidence field path no longer resolves")
    return current


def _record_observation_ref(record: dict[str, Any]) -> str:
    existing = str(record.get("observation_ref") or "")
    if existing:
        return existing
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "obs_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _stable_field_path(spec: dict[str, Any], result: AssertionResult) -> str:
    kind = str(spec.get("kind") or "")
    if kind in {"element_exists", "element_visible", "element_text", "form_state", "element_attr"}:
        field_name = {
            "element_exists": "testid",
            "element_visible": "visible",
            "element_text": "name",
            "form_state": str(spec.get("state") or "present"),
            "element_attr": str(spec.get("attribute") or ""),
        }[kind]
        return f"visible_elements[testid={spec.get('testid', '')}].{field_name}"
    if kind in {"table_row_count", "list_item_count"}:
        if spec.get("selector"):
            return f"selector_counts[{json.dumps(str(spec['selector']), ensure_ascii=False)}]"
        return f"dom_counts.{kind}"
    if kind == "element_relation":
        return "dom_relations"
    if kind == "page_transition":
        return "previous_url_path"
    if kind == "evidence_complete":
        return "evidence_ref"
    fields = re.findall(r"observations\[-1\]\.([A-Za-z0-9_.]+)", result.evidence)
    return ",".join(fields) or result.evidence


def validate_assertion(spec: dict[str, Any]) -> list[str]:
    """Return every definition problem (empty list = usable)."""
    problems: list[str] = []
    kind = str(spec.get("kind") or "")
    if kind == "url_and_content":
        return validate_assertion(
            {
                "kind": "all_of",
                "name": spec.get("name", "url_and_content"),
                "assertions": [
                    {"kind": "url_exact", "name": "url", "value": spec.get("url", "")},
                    {
                        "kind": "page_state",
                        "name": "content",
                        "field": "text",
                        "value": spec.get("contains", ""),
                    },
                ],
            }
        )
    if kind not in SUPPORTED_KINDS:
        problems.append(f"unsupported assertion kind {kind!r}")
        return problems
    if not str(spec.get("name") or "").strip():
        problems.append("assertion needs a non-empty name")
    if (
        kind in ("url_exact", "url_regex", "testid", "page_state")
        and not str(spec.get("value") or "").strip()
    ):
        problems.append(f"{kind} needs a non-empty value")
    if kind.startswith("element_") or kind == "form_state":
        if not str(spec.get("testid") or "").strip():
            problems.append(f"{kind} needs a testid")
    if kind == "element_text" and not str(spec.get("contains") or "").strip():
        problems.append("element_text needs a non-empty contains")
    if kind == "url_regex":
        try:
            re.compile(str(spec.get("value") or ""))
        except re.error as exc:
            problems.append(f"url_regex does not compile: {exc}")
    if kind in ("all_of", "any_of", "not"):
        children = spec.get("assertions")
        if not isinstance(children, list) or not children:
            problems.append(f"{kind} needs a non-empty assertions list")
        else:
            for index, child in enumerate(children):
                if not isinstance(child, dict):
                    problems.append(f"{kind}[{index}] is not an object")
                    continue
                for problem in validate_assertion(child):
                    problems.append(f"{kind}[{index}]: {problem}")
    if kind == "form_state" and str(spec.get("state") or "") not in (
        "present",
        "absent",
        "visible",
        "disabled",
    ):
        problems.append("form_state.state must be one of present/absent/visible/disabled")
    if kind in ("table_row_count", "list_item_count"):
        try:
            if int(str(spec.get("value"))) < 0:
                problems.append(f"{kind}.value must be non-negative")
        except (TypeError, ValueError):
            problems.append(f"{kind}.value must be an integer")
        selector = spec.get("selector")
        if selector is not None and (
            not isinstance(selector, str) or not selector.strip() or len(selector) > 200
        ):
            problems.append(f"{kind}.selector must be a non-empty CSS selector up to 200 chars")
    if kind == "element_attr":
        if not str(spec.get("testid") or "").strip():
            problems.append("element_attr needs a testid")
        if str(spec.get("attribute") or "") not in {"role", "href", "type"}:
            problems.append("element_attr.attribute is not in the safe allowlist")
    if kind == "element_relation" and not str(spec.get("relation") or "").strip():
        problems.append("element_relation needs a relation")
    if kind == "dynamic_match":
        try:
            re.compile(str(spec.get("pattern") or ""))
        except re.error as exc:
            problems.append(f"dynamic_match.pattern does not compile: {exc}")
    if kind == "page_transition" and not str(spec.get("to") or "").strip():
        problems.append("page_transition needs a destination path")
    if kind == "multi_step_goal":
        children = spec.get("assertions")
        if not isinstance(children, list) or not children:
            problems.append("multi_step_goal needs a non-empty assertions list")
        else:
            for child in children:
                if not isinstance(child, dict) or not isinstance(child.get("assertion"), dict):
                    problems.append("multi_step_goal requires assertion objects")
                    continue
                if child.get("match") not in (None, "first_after_previous"):
                    problems.append("unknown multi_step_goal match policy")
                if not child.get("observation_ref") and not child.get("match"):
                    problems.append("multi_step_goal requires an observation_ref or match policy")
                problems.extend(validate_assertion(child["assertion"]))
    if kind == "evidence_complete" and not str(spec.get("evidence_ref") or "").strip():
        problems.append("evidence_complete needs an evidence_ref")
    if kind == "page_state" and str(spec.get("field") or "title") not in (
        "title",
        "url_path",
        "text",
    ):
        problems.append("page_state.field must be one of title/url_path/text")
    return problems


def _result(
    spec: dict[str, Any],
    verdict: str,
    *,
    expected: str,
    observed: str,
    evidence: str,
    detail: str = "",
) -> AssertionResult:
    return AssertionResult(
        name=str(spec.get("name") or spec.get("kind") or "assertion"),
        kind=str(spec.get("kind") or ""),
        verdict=verdict,
        expected=expected,
        observed=observed,
        evidence=evidence,
        required=bool(spec.get("required", True)),
        detail=detail,
    )


def evaluate_assertion(spec: dict[str, Any], view: _ObservationView) -> AssertionResult:
    """Evaluate one assertion. Never raises for a well-formed spec."""
    problems = validate_assertion(spec)
    if problems:
        raise AssertionDefinitionError("; ".join(problems))

    kind = str(spec["kind"])

    if kind == "url_and_content":
        expanded = {
            "kind": "all_of",
            "name": spec.get("name", kind),
            "required": spec.get("required", True),
            "assertions": [
                {"kind": "url_exact", "name": "url", "value": spec.get("url", "")},
                {
                    "kind": "page_state",
                    "name": "content",
                    "field": "text",
                    "value": spec.get("contains", ""),
                },
            ],
        }
        child = evaluate_assertion(expanded, view)
        return _result(
            spec,
            child.verdict,
            expected=child.expected,
            observed=child.observed,
            evidence=child.evidence,
            detail="url_and_content expanded to all_of at evaluation",
        )

    if kind == "all_of":
        children = [evaluate_assertion(child, view) for child in spec["assertions"]]
        verdicts = [child.verdict for child in children]
        if FAILED in verdicts:
            verdict = FAILED
        elif INCONCLUSIVE in verdicts:
            verdict = INCONCLUSIVE
        else:
            verdict = PASSED
        return _result(
            spec,
            verdict,
            expected=f"all {len(children)} sub-assertions",
            observed=", ".join(f"{c.name}={c.verdict}" for c in children),
            evidence="composite",
            detail="all_of: any FAILED wins, then INCONCLUSIVE, else PASSED",
        )

    if kind == "any_of":
        children = [evaluate_assertion(child, view) for child in spec["assertions"]]
        verdicts = [child.verdict for child in children]
        if PASSED in verdicts:
            verdict = PASSED
        elif INCONCLUSIVE in verdicts:
            verdict = INCONCLUSIVE
        else:
            verdict = FAILED
        return _result(
            spec,
            verdict,
            expected=f"any of {len(children)} sub-assertions",
            observed=", ".join(f"{c.name}={c.verdict}" for c in children),
            evidence="composite",
            detail="any_of: any PASSED wins, then INCONCLUSIVE, else FAILED",
        )

    if kind == "not":
        child = evaluate_assertion(spec["assertions"][0], view)
        if child.verdict == PASSED:
            verdict, detail = FAILED, f"child {child.name} passed but must not"
        elif child.verdict == FAILED:
            verdict, detail = PASSED, f"child {child.name} failed as required"
        else:
            verdict, detail = INCONCLUSIVE, f"child {child.name} is inconclusive"
        return _result(
            spec,
            verdict,
            expected=f"not({child.name})",
            observed=child.verdict,
            evidence=child.evidence,
            detail=detail,
        )

    if not view.sampled and kind != "url_exact" and kind != "url_regex":
        # No DOM sample at all: the page may be fine, we simply did not look.
        return _result(
            spec,
            INCONCLUSIVE,
            expected=str(spec.get("value") or spec.get("testid") or ""),
            observed="",
            evidence="observations[-1].visible_elements",
            detail="the observation carries no DOM sample, so this cannot be decided",
        )

    if (
        kind in {"element_exists", "element_visible", "element_text", "element_attr", "form_state"}
        and len(view.elements) >= 40
        and view.element(str(spec.get("testid") or "")) is None
    ):
        return _result(
            spec,
            INCONCLUSIVE,
            expected=str(spec.get("testid") or ""),
            observed="element sample is bounded",
            evidence="observations[-1].visible_elements",
            detail="absence from a full bounded sample cannot prove absence from the page",
        )

    if kind == "url_exact":
        expected = str(spec["value"])
        actual = view.url_path or view.url
        return _result(
            spec,
            INCONCLUSIVE if not actual else PASSED if actual == expected else FAILED,
            expected=expected,
            observed=actual,
            evidence="observations[-1].url_path",
        )

    if kind == "url_regex":
        pattern = str(spec["value"])
        actual = view.url_path or view.url
        return _result(
            spec,
            INCONCLUSIVE if not actual else PASSED if re.search(pattern, actual) else FAILED,
            expected=f"matches {pattern}",
            observed=actual,
            evidence="observations[-1].url_path",
        )

    if kind == "testid":
        expected = str(spec["value"])
        present = expected in view.testids
        return _result(
            spec,
            PASSED if present else FAILED,
            expected=expected,
            observed=f"{len(view.testids)} visible testids",
            evidence="observations[-1].visible_testids",
        )

    if kind == "element_exists":
        testid = str(spec["testid"])
        found = view.element(testid) is not None
        return _result(
            spec,
            PASSED if found else FAILED,
            expected=f"element {testid} exists",
            observed=f"{len(view.elements)} sampled elements",
            evidence="observations[-1].visible_elements",
        )

    if kind == "element_visible":
        testid = str(spec["testid"])
        element = view.element(testid)
        if element is None:
            return _result(
                spec,
                FAILED,
                expected=f"{testid} visible",
                observed="element absent",
                evidence="observations[-1].visible_elements",
            )
        if "visible" not in element:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=f"{testid} visible",
                observed="visibility not captured",
                evidence="observations[-1].visible_elements.visible",
                detail="this observation predates visibility capture",
            )
        visible = bool(element.get("visible"))
        return _result(
            spec,
            PASSED if visible else FAILED,
            expected=f"{testid} visible",
            observed=f"visible={visible}",
            evidence="observations[-1].visible_elements.visible",
        )

    if kind == "element_text":
        testid = str(spec["testid"])
        needle = str(spec["contains"])
        element = view.element(testid)
        if element is None:
            return _result(
                spec,
                FAILED,
                expected=f"{testid} contains {needle!r}",
                observed="element absent",
                evidence="observations[-1].visible_elements",
            )
        # The adapter records a bounded label (<=60 chars), never a field value.
        label = str(element.get("name") or "")
        if len(label) >= 60 and needle not in label:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=needle,
                observed=label,
                evidence="observations[-1].visible_elements.name",
                detail="element label may be truncated; absence is not established",
            )
        if not label:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=needle,
                observed="no text captured for this element",
                evidence="observations[-1].visible_elements.name",
            )
        return _result(
            spec,
            PASSED if needle in label else FAILED,
            expected=f"{testid} contains {needle!r}",
            observed=label[:120],
            evidence="observations[-1].visible_elements.name",
            detail="element text is captured as a bounded label, not full innerText",
        )

    if kind == "form_state":
        testid = str(spec["testid"])
        wanted = str(spec.get("state") or "present")
        element = view.element(testid)
        if element is None:
            verdict = PASSED if wanted == "absent" else FAILED
            return _result(
                spec,
                verdict,
                expected=f"{testid} {wanted}",
                observed="absent",
                evidence="observations[-1].visible_elements",
            )
        if wanted == "absent":
            return _result(
                spec,
                FAILED,
                expected=f"{testid} absent",
                observed="present",
                evidence="observations[-1].visible_elements",
            )
        if wanted == "present":
            return _result(
                spec,
                PASSED,
                expected=f"{testid} present",
                observed="present",
                evidence="observations[-1].visible_elements",
            )
        flag = "visible" if wanted == "visible" else "disabled"
        if flag not in element:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=f"{testid} {wanted}",
                observed=f"{flag} not captured",
                evidence=f"observations[-1].visible_elements.{flag}",
            )
        is_set = bool(element.get(flag))
        return _result(
            spec,
            PASSED if is_set else FAILED,
            expected=f"{testid} {wanted}",
            observed=f"{flag}={is_set}",
            evidence=f"observations[-1].visible_elements.{flag}",
        )

    if kind == "page_state":
        field_name = str(spec.get("field") or "title")
        needle = str(spec["value"])
        source_key = "text_excerpt" if field_name == "text" else field_name
        if source_key not in view.raw:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=f"{field_name} contains {needle!r}",
                observed="field unavailable",
                evidence=f"observations[-1].{source_key}",
            )
        source = {"title": view.title, "url_path": view.url_path, "text": view.text}[field_name]
        if field_name == "text" and needle not in source and len(source) >= 3000:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=f"text contains {needle!r}",
                observed=source[:120],
                evidence="observations[-1].text_excerpt",
                detail="text excerpt may be truncated; absence cannot establish a negative",
            )
        return _result(
            spec,
            PASSED if needle in source else FAILED,
            expected=f"{field_name} contains {needle!r}",
            observed=source[:120],
            evidence=f"observations[-1].{field_name}",
        )

    if kind in ("table_row_count", "list_item_count"):
        key = kind
        selector = str(spec.get("selector") or "")
        if selector:
            counts = view.raw.get("selector_counts") or {}
            if selector not in counts:
                return _result(
                    spec,
                    INCONCLUSIVE,
                    expected=str(spec["value"]),
                    observed="selector count unavailable",
                    evidence=f"observations[-1].selector_counts[{selector!r}]",
                )
            actual_count = int(counts[selector])
            expected_count = int(str(spec["value"]))
            return _result(
                spec,
                PASSED if actual_count == expected_count else FAILED,
                expected=str(expected_count),
                observed=str(actual_count),
                evidence=f"observations[-1].selector_counts[{selector!r}]",
            )
        counts = view.raw.get("dom_counts") or {}
        if key not in counts:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=str(spec["value"]),
                observed="count unavailable",
                evidence=f"observations[-1].dom_counts.{key}",
            )
        actual_count = int(counts[key])
        expected_count = int(str(spec["value"]))
        return _result(
            spec,
            PASSED if actual_count == expected_count else FAILED,
            expected=str(expected_count),
            observed=str(actual_count),
            evidence=f"observations[-1].dom_counts.{key}",
        )

    if kind == "element_attr":
        item = view.element(str(spec["testid"]))
        attribute = str(spec["attribute"])
        if item is None or attribute not in item:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=str(spec.get("value", "")),
                observed="attribute unavailable",
                evidence=f"observations[-1].visible_elements.{attribute}",
            )
        actual = str(item[attribute])
        expected = str(spec.get("value", ""))
        return _result(
            spec,
            PASSED if actual == expected else FAILED,
            expected=expected,
            observed=actual[:200],
            evidence=f"observations[-1].visible_elements.{attribute}",
        )

    if kind == "element_relation":
        if "dom_relations" not in view.raw:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=str(spec["relation"]),
                observed="relation capture unavailable",
                evidence="observations[-1].dom_relations",
            )
        relations = view.raw.get("dom_relations") or []
        relation = str(spec["relation"])
        found = any(
            isinstance(x, dict)
            and x.get("relation") == relation
            and (not spec.get("testid") or x.get("testid") == spec["testid"])
            and (not spec.get("parent_testid") or x.get("parent_testid") == spec["parent_testid"])
            for x in relations
        )
        return _result(
            spec,
            PASSED if found else FAILED,
            expected=relation,
            observed=f"{len(relations)} captured relations",
            evidence="observations[-1].dom_relations",
        )

    if kind == "page_transition":
        previous = str(view.raw.get("previous_url_path") or "")
        current = view.url_path
        if not previous:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=str(spec["to"]),
                observed="previous path unavailable",
                evidence="observations[-1].previous_url_path",
            )
        expected = str(spec["to"])
        changed = previous != current and current == expected
        return _result(
            spec,
            PASSED if changed else FAILED,
            expected=f"{previous} -> {expected}",
            observed=f"{previous} -> {current}",
            evidence="observations[-1].previous_url_path,observations[-1].url_path",
        )

    if kind == "multi_step_goal":
        history = view.raw.get("observations")
        children = []
        previous_index = -1
        if isinstance(history, list):
            for step in spec["assertions"]:
                ref = str(step.get("observation_ref") or "")
                selected = next(
                    (
                        (index, row)
                        for index, row in enumerate(history)
                        if isinstance(row, dict)
                        and (
                            index > previous_index
                            and evaluate_assertion(step["assertion"], _ObservationView(row)).verdict
                            == PASSED
                            if step.get("match") == "first_after_previous"
                            else row.get("observation_ref") == ref
                        )
                    ),
                    None,
                )
                target = selected[1] if selected is not None else None
                if target is None:
                    children.append(
                        _result(
                            step,
                            INCONCLUSIVE,
                            expected=ref,
                            observed="reference missing",
                            evidence="observations[*].observation_ref",
                        )
                    )
                elif selected is not None and selected[0] <= previous_index:
                    children.append(
                        _result(
                            step,
                            FAILED,
                            expected="strict observation order",
                            observed=ref,
                            evidence="observations[*].observation_ref",
                            detail="multi-step observations are out of order or reused",
                        )
                    )
                else:
                    assert selected is not None
                    previous_index = selected[0]
                    assertion = step.get("assertion", step)
                    target_view = _ObservationView(target)
                    child = evaluate_assertion(assertion, target_view)
                    child_path = _stable_field_path(assertion, child)
                    child_ref = (
                        "ev_"
                        + hashlib.sha256(
                            f"{target_view.step_id}:{target_view.observation_ref}:{child_path}".encode()
                        ).hexdigest()[:24]
                    )
                    children.append(
                        replace(
                            child,
                            step_id=target_view.step_id,
                            observation_ref=target_view.observation_ref,
                            field_path=child_path,
                            evidence_ref=child_ref,
                        )
                    )
        else:
            children = [
                _result(
                    step,
                    INCONCLUSIVE,
                    expected=str(step.get("observation_ref") or "stable observation ref"),
                    observed="observation history unavailable",
                    evidence="observations[*].observation_ref",
                )
                for step in spec["assertions"]
            ]
        verdicts = [x.verdict for x in children]
        verdict = (
            FAILED if FAILED in verdicts else INCONCLUSIVE if INCONCLUSIVE in verdicts else PASSED
        )
        result = _result(
            spec,
            verdict,
            expected=f"{len(children)} steps",
            observed=", ".join(x.verdict for x in children),
            evidence="composite",
            detail="ordered multi-step assertions share stable observation references",
        )
        return replace(
            result, evidence_refs=tuple(x.evidence_ref for x in children if x.evidence_ref)
        )

    if kind == "dynamic_match":
        pattern = str(spec["pattern"])
        matched = re.search(pattern, view.text) is not None
        if "text_excerpt" not in view.raw or len(view.text) >= 3000:
            return _result(
                spec,
                INCONCLUSIVE,
                expected=f"matches {pattern}",
                observed=view.text[:120],
                evidence="observations[-1].text_excerpt",
                detail="text excerpt may be truncated; use a structured element assertion",
            )
        return _result(
            spec,
            PASSED if matched else FAILED,
            expected=f"matches {pattern}",
            observed=view.text[:120],
            evidence="observations[-1].text_excerpt",
        )

    if kind == "evidence_complete":
        expected = str(spec["evidence_ref"])
        actual = str(view.raw.get("evidence_ref") or "")
        ref = str(view.raw.get("observation_ref") or "")
        complete = actual == expected and bool(ref)
        return _result(
            spec,
            PASSED if complete else INCONCLUSIVE if not actual or not ref else FAILED,
            expected=expected,
            observed=actual or "missing",
            evidence="observations[-1].evidence_ref,observations[-1].observation_ref",
        )

    raise AssertionDefinitionError(f"unsupported assertion kind {kind!r}")  # pragma: no cover


def evaluate_assertions(
    assertions: list[dict[str, Any]],
    observation: dict[str, Any],
    *,
    observations: list[dict[str, Any]] | None = None,
) -> AssertionReport:
    """Evaluate a whole goal. All assertions run; none short-circuits another."""
    if not assertions:
        return AssertionReport(INCONCLUSIVE, (), "goal declares no assertions")
    raw = dict(observation or {})
    if observations is not None:
        raw["observations"] = observations
    # Registration sugar: keep the external declaration at 20 forms while
    # ensuring evaluation is only performed by the existing all_of evaluator.
    assertions = [
        (
            {
                "kind": "all_of",
                "name": a.get("name", "url_and_content"),
                "required": a.get("required", True),
                "assertions": [
                    {"kind": "url_exact", "name": "url", "value": a.get("url", "")},
                    {
                        "kind": "page_state",
                        "name": "content",
                        "field": "text",
                        "value": a.get("contains", ""),
                    },
                ],
            }
            if a.get("kind") == "url_and_content"
            else a
        )
        for a in assertions
    ]
    view = _ObservationView(raw)
    raw_results = tuple(evaluate_assertion(spec, view) for spec in assertions)
    results = tuple(
        replace(
            result,
            observation_ref=view.observation_ref,
            step_id=view.step_id,
            field_path=_stable_field_path(spec, result),
            evidence_ref=(
                "ev_"
                + hashlib.sha256(
                    (
                        f"{view.step_id}:{view.observation_ref}:{_stable_field_path(spec, result)}"
                    ).encode()
                ).hexdigest()[:24]
                if result.evidence != "composite"
                else ""
            ),
        )
        for spec, result in zip(assertions, raw_results, strict=True)
    )
    required = [r for r in results if r.required]
    if not required:
        return AssertionReport(
            INCONCLUSIVE,
            results,
            "goal has only optional assertions; a pass would prove nothing",
        )
    if any(r.verdict == FAILED for r in required):
        failed = [r.name for r in required if r.verdict == FAILED]
        return AssertionReport(NOT_VERIFIED, results, f"required assertions failed: {failed}")
    if any(r.verdict == INCONCLUSIVE for r in required):
        unknown = [r.name for r in required if r.verdict == INCONCLUSIVE]
        return AssertionReport(
            INCONCLUSIVE, results, f"required assertions could not be observed: {unknown}"
        )
    return AssertionReport(VERIFIED, results, "every required assertion passed")
