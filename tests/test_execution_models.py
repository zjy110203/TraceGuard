"""Tests for append-only TestCaseVersion and Oracle binding integrity."""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.execution.models import (
    AssertionSpec,
    CleanupPolicy,
    OracleBinding,
    OracleSpec,
    TestCaseRepository,
    TestCaseVersion,
    TestDataRef,
    TestIntent,
    TestStep,
    next_version,
)
from packages.execution.verdict import OracleVerdict

# These are domain models imported into the test module's namespace; mark them
# so pytest does not try to collect them as test classes.
for _model in (TestIntent, TestDataRef, TestStep, TestCaseVersion, TestCaseRepository):
    _model.__test__ = False


def make_version(case_id: str = "TC-001", version: int = 1) -> TestCaseVersion:
    oracle = OracleSpec(
        oracle_id="o1",
        source_type="independent_api",
        source_ref="GET /api/v1/state/independent#project_count",
        expected=OracleVerdict.PASSED,
    )
    return TestCaseVersion(
        case_id=case_id,
        version=version,
        title="create project",
        intent=TestIntent("i1", "verify a project can be created"),
        steps=(TestStep("s1", "click", "#new-project"),),
        assertions=(AssertionSpec("a1", "project appears in independent state", required=True),),
        oracle_bindings=(OracleBinding("a1", primary=oracle),),
        data_refs=(TestDataRef("d1", "teamflow.project.name", "Demo"),),
        cleanup=CleanupPolicy(),
    )


def test_empty_required_set_rejected():
    v = make_version()
    no_assertions = next_version(v, assertions=(), oracle_bindings=())
    with pytest.raises(ValueError, match="no assertions"):
        no_assertions.validate()


def test_assertion_without_oracle_binding_rejected():
    v = make_version()
    unbound = next_version(v, oracle_bindings=())
    with pytest.raises(ValueError, match="without oracle binding"):
        unbound.validate()


def test_unapproved_oracle_rejected():
    v = make_version()
    bad_oracle = OracleSpec(
        oracle_id="o2",
        source_type="ui",
        source_ref="#msg",
        expected=OracleVerdict.PASSED,
        approved=False,
    )
    modified = next_version(v, oracle_bindings=(OracleBinding("a1", bad_oracle),))
    with pytest.raises(ValueError, match="unapproved"):
        modified.validate()


def test_duplicate_version_rejected():
    repo = TestCaseRepository()
    v = make_version()
    repo.add_version(v)
    with pytest.raises(ValueError, match="append-only"):
        repo.add_version(v)


def test_non_monotonic_version_rejected():
    repo = TestCaseRepository()
    repo.add_version(make_version(version=2))
    with pytest.raises(ValueError, match="monotonically"):
        repo.add_version(make_version(version=1))


def test_next_version_does_not_mutate_original():
    v1 = make_version()
    v2 = next_version(v1, title="renamed")
    assert v1.version == 1
    assert v1.title == "create project"
    assert v2.version == 2
    assert v2.title == "renamed"


def test_repository_persists_and_reloads(tmp_path: Path):
    repo = TestCaseRepository(tmp_path)
    repo.add_version(make_version())
    reloaded = TestCaseRepository(tmp_path)
    v = reloaded.get("TC-001")
    assert v.title == "create project"
    assert v.oracle_bindings[0].primary.expected is OracleVerdict.PASSED
    assert v.assertions[0].required is True
