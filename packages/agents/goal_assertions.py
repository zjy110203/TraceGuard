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

import re
from dataclasses import dataclass, field
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
)


class AssertionDefinitionError(ValueError):
    """The assertion itself is malformed (author error, not a page verdict)."""


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
                return element
        return None

    @property
    def sampled(self) -> bool:
        """Did the adapter actually capture structure for this page?"""
        return bool(self.testids or self.elements)


def validate_assertion(spec: dict[str, Any]) -> list[str]:
    """Return every definition problem (empty list = usable)."""
    problems: list[str] = []
    kind = str(spec.get("kind") or "")
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

    if kind == "url_exact":
        expected = str(spec["value"])
        actual = view.url_path or view.url
        return _result(
            spec,
            PASSED if actual == expected else FAILED,
            expected=expected,
            observed=actual,
            evidence="observations[-1].url_path",
        )

    if kind == "url_regex":
        pattern = str(spec["value"])
        actual = view.url_path or view.url
        return _result(
            spec,
            PASSED if re.search(pattern, actual) else FAILED,
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
        source = {"title": view.title, "url_path": view.url_path, "text": view.text}[field_name]
        return _result(
            spec,
            PASSED if needle in source else FAILED,
            expected=f"{field_name} contains {needle!r}",
            observed=source[:120],
            evidence=f"observations[-1].{field_name}",
        )

    raise AssertionDefinitionError(f"unsupported assertion kind {kind!r}")  # pragma: no cover


def evaluate_assertions(
    assertions: list[dict[str, Any]], observation: dict[str, Any]
) -> AssertionReport:
    """Evaluate a whole goal. All assertions run; none short-circuits another."""
    if not assertions:
        return AssertionReport(INCONCLUSIVE, (), "goal declares no assertions")
    view = _ObservationView(dict(observation or {}))
    results = tuple(evaluate_assertion(spec, view) for spec in assertions)
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
