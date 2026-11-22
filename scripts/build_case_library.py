"""Generate the stage-2 TeamFlow case library (12 formal TestCaseVersions).

Every case binds assertions to ORACLES THAT READ ONLY observable state:
  * ``state.<key>``  -> independent business-state API (never Ground Truth)
  * ``ui.<step>.passed`` -> the runner's own observation of a browser assert step

No case reads ``/__eval__``, Ground Truth, or fault config. That is the
traceability property that keeps a verdict trustworthy (Agent is a capability
layer, not a fact layer).

Regenerate with:
    .venv/Scripts/python.exe -m scripts.build_case_library
"""

from __future__ import annotations

import json
from pathlib import Path

from packages.execution.models import (
    AssertionSpec,
    CleanupPolicy,
    OracleBinding,
    OracleSpec,
    TestCaseVersion,
    TestDataRef,
    TestIntent,
    TestStep,
)
from packages.execution.verdict import OracleVerdict

CASE_DIR = Path("benchmark/cases")


def _state_oracle(
    assertion_id: str, key: str, expected: object, source: str
) -> tuple[AssertionSpec, OracleBinding]:
    spec = OracleSpec(
        oracle_id=f"o-{assertion_id}-state",
        source_type="independent_api",
        source_ref=f"state.{key}",
        expected=OracleVerdict.PASSED,
        expected_value=expected,
        source=source,
    )
    return (
        AssertionSpec(assertion_id, f"{key} == {expected!r} in independent state"),
        OracleBinding(assertion_id, primary=spec),
    )


def _ui_oracle(
    assertion_id: str, step_id: str, source: str, description: str
) -> tuple[AssertionSpec, OracleBinding]:
    spec = OracleSpec(
        oracle_id=f"o-{assertion_id}-ui",
        source_type="ui",
        source_ref=f"ui.{step_id}.passed",
        expected=OracleVerdict.PASSED,
        expected_value=True,
        source=source,
    )
    return (
        AssertionSpec(assertion_id, description),
        OracleBinding(assertion_id, primary=spec),
    )


def _build(
    *,
    case_id: str,
    version: int,
    title: str,
    description: str,
    steps: tuple[TestStep, ...],
    parts: tuple[tuple[AssertionSpec, OracleBinding], ...],
    data_refs: tuple[TestDataRef, ...] = (),
) -> TestCaseVersion:
    assertions = tuple(a for a, _ in parts)
    bindings = tuple(b for _, b in parts)
    case = TestCaseVersion(
        case_id=case_id,
        version=version,
        title=title,
        intent=TestIntent(f"i-{case_id}", description),
        steps=steps,
        assertions=assertions,
        oracle_bindings=bindings,
        data_refs=data_refs,
        cleanup=CleanupPolicy(),
        compiled_from="manual",
        approved_by="human",
    )
    case.validate()
    return case


# Shared step fragments. Raw testid selectors are proven by the stage-2 E2E;
# TC-LOGIN-001 deliberately uses logical ``loc:`` targets to exercise the
# LocatorRegistry priority policy end-to-end.
LOGIN_RAW = (
    TestStep("s1", "navigate", "/"),
    TestStep("s2", "fill", "[data-testid=username]", value="tester"),
    TestStep("s3", "fill", "[data-testid=password]", value="test-pass-123"),
    TestStep("s4", "click", "[data-testid=login-submit]"),
)
LOGIN_LOGICAL = (
    TestStep("s1", "navigate", "/"),
    TestStep("s2", "fill", "loc:login.username", value="tester"),
    TestStep("s3", "fill", "loc:login.password", value="test-pass-123"),
    TestStep("s4", "click", "loc:login.submit"),
)
NEW_PROJECT = (
    TestStep("s5", "assert_visible", "[data-testid=new-project-link]"),
    TestStep("s6", "click", "[data-testid=new-project-link]"),
)
CREATE_FORM = (
    TestStep("s7", "fill", "[data-testid=project-name]", value="Proj Alpha"),
    TestStep("s8", "click", "[data-testid=create-submit]"),
)
SRC = "stage2 manual case library v1 (human approved)"


def build_cases() -> tuple[TestCaseVersion, ...]:
    cases: list[TestCaseVersion] = []

    # 1. Login + logical locators (registry policy exercised in a real browser).
    cases.append(
        _build(
            case_id="TC-LOGIN-001",
            version=1,
            title="login and land on projects page (logical locators)",
            description="a valid tester can log in and reach the project list",
            steps=(*LOGIN_LOGICAL, TestStep("s5", "assert_visible", "loc:nav.new_project")),
            parts=(_ui_oracle("a1", "s5", SRC, "project list link visible after login"),),
        )
    )

    # 2. Invalid login shows an error (negative path, no GT).
    cases.append(
        _build(
            case_id="TC-LOGIN-002",
            version=1,
            title="invalid password shows login error",
            description="a wrong password is rejected with a visible error",
            steps=(
                TestStep("s1", "navigate", "/"),
                TestStep("s2", "fill", "[data-testid=username]", value="tester"),
                TestStep("s3", "fill", "[data-testid=password]", value="wrong-password"),
                TestStep("s4", "click", "[data-testid=login-submit]"),
                TestStep("s5", "assert_visible", "[data-testid=login-error]"),
            ),
            parts=(_ui_oracle("a1", "s5", SRC, "login error visible"),),
        )
    )

    # 3. Logout returns to login form.
    cases.append(
        _build(
            case_id="TC-LOGIN-003",
            version=1,
            title="logout returns to login form",
            description="an authenticated user can log out",
            steps=(
                *LOGIN_RAW,
                TestStep("s5", "navigate", "/logout"),
                TestStep("s6", "assert_visible", "[data-testid=login-submit]"),
            ),
            parts=(_ui_oracle("a1", "s6", SRC, "login form visible after logout"),),
        )
    )

    # 4. Create + persistence (the canonical happy path).
    cases.append(
        _build(
            case_id="TC-CREATE-001",
            version=1,
            title="create project and verify persistence",
            description="a created project is persisted in independent state",
            steps=(*LOGIN_RAW, *NEW_PROJECT, *CREATE_FORM),
            parts=(_state_oracle("a1", "project_count", 1, SRC),),
        )
    )

    # 5. Create with description + success banner (two independent oracles).
    cases.append(
        _build(
            case_id="TC-CREATE-002",
            version=1,
            title="create project with description and success banner",
            description="description is accepted and success banner is shown",
            steps=(
                *LOGIN_RAW,
                *NEW_PROJECT,
                TestStep("s7", "fill", "[data-testid=project-name]", value="Proj With Desc"),
                TestStep("s8", "fill", "[data-testid=project-description]", value="hello"),
                TestStep("s9", "click", "[data-testid=create-submit]"),
                TestStep("s10", "assert_visible", "[data-testid=success-message]"),
            ),
            parts=(
                _ui_oracle("a1", "s10", SRC, "success banner visible"),
                _state_oracle("a2", "project_count", 1, SRC),
            ),
        )
    )

    # 6. Two distinct projects -> count 2.
    cases.append(
        _build(
            case_id="TC-CREATE-003",
            version=1,
            title="create two distinct projects",
            description="two sequential creates both persist",
            steps=(
                *LOGIN_RAW,
                *NEW_PROJECT,
                TestStep("s7", "fill", "[data-testid=project-name]", value="Proj One"),
                TestStep("s8", "click", "[data-testid=create-submit]"),
                TestStep("s9", "navigate", "/projects/new"),
                TestStep("s10", "fill", "[data-testid=project-name]", value="Proj Two"),
                TestStep("s11", "click", "[data-testid=create-submit]"),
            ),
            parts=(_state_oracle("a1", "project_count", 2, SRC),),
        )
    )

    # 7. Duplicate name is rejected and state stays at one project.
    cases.append(
        _build(
            case_id="TC-CREATE-DUP-001",
            version=1,
            title="duplicate project name is rejected",
            description="a duplicate name shows a form error and does not persist twice",
            steps=(
                *LOGIN_RAW,
                *NEW_PROJECT,
                *CREATE_FORM,
                TestStep("s9", "navigate", "/projects/new"),
                TestStep("s10", "fill", "[data-testid=project-name]", value="Proj Alpha"),
                TestStep("s11", "click", "[data-testid=create-submit]"),
                TestStep("s12", "assert_visible", "[data-testid=form-error]"),
            ),
            parts=(
                _ui_oracle("a1", "s12", SRC, "duplicate-name form error visible"),
                _state_oracle("a2", "project_count", 1, SRC),
            ),
        )
    )

    # 8. Empty project list is visible after login.
    cases.append(
        _build(
            case_id="TC-LIST-001",
            version=1,
            title="empty project list page renders with zero projects",
            description="a fresh workspace shows the list page with zero persisted projects",
            steps=(
                *LOGIN_RAW,
                # An empty <ul> has zero bounding box and is "hidden" to
                # Playwright, so assert a genuinely visible page element.
                # Emptiness is verified authoritatively by the state oracle.
                TestStep("s5", "assert_visible", "[data-testid=new-project-link]"),
            ),
            parts=(
                _ui_oracle("a1", "s5", SRC, "project list page rendered"),
                _state_oracle("a2", "project_count", 0, SRC),
            ),
        )
    )

    # 9. Created project appears as a list item.
    cases.append(
        _build(
            case_id="TC-LIST-002",
            version=1,
            title="created project appears in list",
            description="a persisted project is rendered as a list item",
            steps=(
                *LOGIN_RAW,
                *NEW_PROJECT,
                *CREATE_FORM,
                TestStep("s9", "assert_visible", "[data-testid=project-item]"),
            ),
            parts=(
                _ui_oracle("a1", "s9", SRC, "project item visible"),
                _state_oracle("a2", "project_count", 1, SRC),
            ),
        )
    )

    # 10. Detail page shows the created project's name and status.
    cases.append(
        _build(
            case_id="TC-DETAIL-001",
            version=1,
            title="project detail shows name and status",
            description="opening a project shows its title and ACTIVE status",
            steps=(
                *LOGIN_RAW,
                *NEW_PROJECT,
                *CREATE_FORM,
                TestStep("s9", "click", "[data-testid=project-item] a"),
                TestStep("s10", "assert_text", "[data-testid=project-title]", value="Proj Alpha"),
                TestStep("s11", "assert_text", "[data-testid=project-status]", value="ACTIVE"),
            ),
            parts=(
                _ui_oracle("a1", "s10", SRC, "detail title matches"),
                _ui_oracle("a2", "s11", SRC, "detail status is ACTIVE"),
                _state_oracle("a3", "project_count", 1, SRC),
            ),
        )
    )

    # 11. Navigation to the new-project form exposes required fields.
    cases.append(
        _build(
            case_id="TC-NAV-001",
            version=1,
            title="new project form exposes name field",
            description="the create form is reachable and has a name input",
            steps=(
                *LOGIN_RAW,
                *NEW_PROJECT,
                TestStep("s7", "assert_visible", "[data-testid=project-name]"),
                TestStep("s8", "assert_visible", "[data-testid=create-submit]"),
            ),
            parts=(_ui_oracle("a1", "s7", SRC, "name input visible"),),
        )
    )

    # 12. Wait action then assert the created item (async timing surface).
    cases.append(
        _build(
            case_id="TC-TIME-001",
            version=1,
            title="wait then assert created project",
            description="an explicit wait is followed by a stable list assertion",
            steps=(
                *LOGIN_RAW,
                *NEW_PROJECT,
                *CREATE_FORM,
                TestStep("s9", "wait", "", value="200"),
                TestStep("s10", "assert_visible", "[data-testid=project-item]"),
            ),
            parts=(
                _ui_oracle("a1", "s10", SRC, "project item visible after wait"),
                _state_oracle("a2", "project_count", 1, SRC),
            ),
        )
    )

    return tuple(cases)


def main() -> None:
    CASE_DIR.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    for case in cases:
        path = CASE_DIR / f"{case.case_id}.json"
        path.write_text(json.dumps(case.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {path} ({len(case.steps)} steps, {len(case.assertions)} assertions)")


if __name__ == "__main__":
    main()
