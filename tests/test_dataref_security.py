"""R5-4.7 §4.2: sensitive test data never lands in a persisted asset.

Illegal-combination matrix over TestDataRef.value / TestStep.value / value_ref,
plus the runtime guarantee: resolved plaintext stays in memory and never
re-enters a report, an event, an evidence record or a case file.
"""

from __future__ import annotations

import json

import pytest

from packages.execution.dataref import (
    REDACTED_MARKER,
    MappingTestDataProvider,
    resolve_step_value,
)
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

TestIntent.__test__ = False
TestStep.__test__ = False
TestCaseVersion.__test__ = False
TestDataRef.__test__ = False


def _case(**overrides: object) -> TestCaseVersion:
    base: dict[str, object] = {
        "case_id": "TC-DATAREF",
        "version": 1,
        "title": "dataref security",
        "intent": TestIntent("i-1", "d"),
        "steps": (TestStep("s1", "fill", "[data-testid=password]", value_ref="teamflow.password"),),
        "assertions": (AssertionSpec("a1", "a"),),
        "oracle_bindings": (
            OracleBinding(
                "a1",
                primary=OracleSpec(
                    oracle_id="o1",
                    source_type="ui",
                    source_ref="ui.url_path",
                    approved=True,
                    expected_value="/projects",
                ),
            ),
        ),
        "data_refs": (TestDataRef("teamflow.password", "[data-testid=password]"),),
        "approved_by": "human",
    }
    base.update(overrides)
    return TestCaseVersion(**base)  # type: ignore[arg-type]


# ----------------------------------------------------- structural invariants


def test_value_ref_with_plaintext_is_illegal() -> None:
    case = _case(
        steps=(
            TestStep(
                "s1",
                "fill",
                "[data-testid=password]",
                value="hunter2",
                value_ref="teamflow.password",
            ),
        )
    )
    with pytest.raises(ValueError, match="both"):
        case.validate()


def test_undeclared_value_ref_is_illegal() -> None:
    case = _case(data_refs=())
    with pytest.raises(ValueError, match="undeclared"):
        case.validate()


def test_placeholder_as_value_is_illegal() -> None:
    case = _case(steps=(TestStep("s1", "fill", "[data-testid=password]", value=REDACTED_MARKER),))
    with pytest.raises(ValueError, match="placeholder"):
        case.validate()


def test_sensitive_data_ref_with_inline_value_is_illegal() -> None:
    case = _case(
        data_refs=(TestDataRef("teamflow.password", "[data-testid=password]", value="hunter2"),)
    )
    with pytest.raises(ValueError, match="TestDataProvider"):
        case.validate()


def test_nonsensitive_ref_may_carry_a_fixture_value() -> None:
    case = _case(
        steps=(TestStep("s1", "fill", "[data-testid=project-name]", value="demo"),),
        data_refs=(
            TestDataRef("teamflow.project-name", "[data-testid=project-name]", value="demo"),
        ),
    )
    case.validate()  # non-secret fixture values are legitimate


# ------------------------------------------------------------- runtime layer


def test_unresolved_ref_blocks_the_run_without_a_browser(tmp_path, monkeypatch) -> None:
    case = _case()
    runner = LocalRunner(str(tmp_path), report_dir=tmp_path / "reports")
    report = runner.run(case, run_id="run_blocked")
    assert report.blocked is True
    assert "teamflow.password" in report.blocked_reason
    assert report.test_verdict != "PASSED"
    assert report.steps == []  # nothing executed
    # no browser was started: no screenshot / playwright trace evidence
    assert not any(
        e["evidence_type"] in {"screenshot", "playwright_trace"} for e in report.evidence
    )


def test_placeholder_input_blocks_the_run(tmp_path) -> None:
    case = _case(
        steps=(TestStep("s1", "fill", "[data-testid=password]", value=REDACTED_MARKER),),
        data_refs=(),
    )
    report = LocalRunner(str(tmp_path)).run(case, run_id="run_ph")
    assert report.blocked is True
    assert "placeholder" in report.blocked_reason


def test_resolved_plaintext_never_reenters_persisted_assets(tmp_path) -> None:
    """The provider resolves the secret at run time; it never lands on disk."""
    secret = "runtime-secret-ckpg-331"
    case = _case()
    provider = MappingTestDataProvider({"teamflow.password": secret})
    runner = LocalRunner(str(tmp_path), report_dir=tmp_path / "reports", data_provider=provider)
    report = runner.run(
        case,
        run_id="run_secret",
        execution_completed=False,  # offline: no browser
    )
    blob = json.dumps(report.to_dict(), ensure_ascii=False)
    assert secret not in blob
    # the persisted report file does not contain it either
    persisted = (tmp_path / "reports" / "run_secret.json").read_text(encoding="utf-8")
    assert secret not in persisted


def test_resolve_step_value_combinations() -> None:
    provider = MappingTestDataProvider({"teamflow.password": "x"})
    # ref + plaintext -> refuse
    r = resolve_step_value(
        step_id="s", value="plain", value_ref="teamflow.password", provider=provider
    )
    assert not r.ok and "both" in r.reason
    # ref only -> resolve
    r = resolve_step_value(step_id="s", value="", value_ref="teamflow.password", provider=provider)
    assert r.ok and r.value == "x"
    # unknown ref -> block
    r = resolve_step_value(step_id="s", value="", value_ref="teamflow.unknown", provider=provider)
    assert not r.ok and "unresolved" in r.reason
    # placeholder without ref -> block
    r = resolve_step_value(step_id="s", value=REDACTED_MARKER, value_ref="", provider=provider)
    assert not r.ok and "placeholder" in r.reason
