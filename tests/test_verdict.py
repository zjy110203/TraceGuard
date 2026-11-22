"""Table-driven tests for the FROZEN stage-2 verdict rules.

Every case here maps to a legal/illegal example in the v1.1 roadmap lines
473-487, or to a supplemental rule in lines 456-467. If these tests pass,
the aggregation semantics are pinned to the design.
"""

from __future__ import annotations

import pytest

from packages.execution.verdict import (
    AssertionResult,
    AssertionVerdict,
    InvalidTestCaseError,
    OracleVerdict,
    TestVerdict,
    aggregate_verdict,
    evaluate_oracle_binding,
)


def req(
    aid: str,
    verdict: AssertionVerdict,
    *,
    oracle_available: bool = True,
    oracle_conflict: bool = False,
    evaluated: bool = True,
) -> AssertionResult:
    return AssertionResult(
        assertion_id=aid,
        required=True,
        verdict=verdict,
        oracle_available=oracle_available,
        oracle_conflict=oracle_conflict,
        evaluated=evaluated,
    )


def opt(aid: str, verdict: AssertionVerdict) -> AssertionResult:
    return AssertionResult(assertion_id=aid, required=False, verdict=verdict)


P = AssertionVerdict.PASSED
F = AssertionVerdict.FAILED
INC = AssertionVerdict.INCONCLUSIVE
N = AssertionVerdict.NOT_EVALUATED


@pytest.mark.parametrize(
    ("results", "execution_completed", "expected"),
    [
        pytest.param([req("a1", P)], True, TestVerdict.PASSED, id="all-required-pass"),
        pytest.param(
            [req("a1", P), req("a2", P)], True, TestVerdict.PASSED, id="two-required-pass"
        ),
        pytest.param(
            [req("a1", F), req("a2", P)], True, TestVerdict.FAILED, id="one-required-fails"
        ),
        pytest.param(
            [req("a1", F), req("a2", N)],
            False,
            TestVerdict.FAILED,
            id="failure-not-masked-by-abort",
        ),
        pytest.param(
            [req("a1", N), req("a2", P)],
            True,
            TestVerdict.INCONCLUSIVE,
            id="partial-not-evaluated",
        ),
        pytest.param(
            [req("a1", N), req("a2", N)],
            True,
            TestVerdict.NOT_EVALUATED,
            id="all-not-evaluated",
        ),
        pytest.param(
            [req("a1", P, oracle_available=False)],
            True,
            TestVerdict.NOT_EVALUATED,
            id="all-oracles-unavailable",
        ),
        pytest.param(
            [
                req("a1", P, oracle_available=False),
                req("a2", P),
            ],
            True,
            TestVerdict.INCONCLUSIVE,
            id="partial-oracle-unavailable",
        ),
        pytest.param(
            [req("a1", P, oracle_conflict=True)],
            True,
            TestVerdict.INCONCLUSIVE,
            id="unresolved-oracle-conflict",
        ),
        pytest.param(
            [req("a1", INC), req("a2", P)],
            True,
            TestVerdict.INCONCLUSIVE,
            id="inconclusive-plus-pass",
        ),
        pytest.param(
            [req("a1", P)],
            False,
            TestVerdict.INCONCLUSIVE,
            id="abort-after-all-pass-not-passed",
        ),
        pytest.param(
            [req("a1", P), opt("o1", F)],
            True,
            TestVerdict.PASSED,
            id="optional-failure-keeps-pass",
        ),
        pytest.param(
            [req("a1", F), opt("o1", P)],
            True,
            TestVerdict.FAILED,
            id="optional-pass-cannot-mask-required-failure",
        ),
        pytest.param(
            [req("a1", P, evaluated=False)],
            True,
            TestVerdict.NOT_EVALUATED,
            id="unevaluated-assertion",
        ),
    ],
)
def test_frozen_verdict_matrix(results, execution_completed, expected):
    outcome = aggregate_verdict(results, execution_completed=execution_completed)
    assert outcome.verdict is expected


def test_empty_required_set_is_illegal():
    with pytest.raises(InvalidTestCaseError):
        aggregate_verdict([opt("o1", P)])


def test_no_assertions_at_all_is_illegal():
    with pytest.raises(InvalidTestCaseError):
        aggregate_verdict([])


def test_optional_results_preserved():
    outcome = aggregate_verdict([req("a1", P), opt("o1", F), opt("o2", INC)])
    assert outcome.verdict is TestVerdict.PASSED
    assert len(outcome.optional_results) == 2
    assert outcome.optional_results[0].verdict is F
    assert outcome.optional_results[1].verdict is INC


def test_effective_count_and_failed_ids_audited():
    outcome = aggregate_verdict(
        [req("a1", F), req("a2", P), req("a3", N)], execution_completed=True
    )
    assert outcome.verdict is TestVerdict.FAILED
    assert outcome.failed_ids == ("a1",)
    assert outcome.effective_count == 2
    assert outcome.unresolved_ids == ("a3",)


@pytest.mark.parametrize(
    ("primary", "supporting", "observed_match", "expected_verdict", "expected_conflict"),
    [
        (OracleVerdict.PASSED, (), True, AssertionVerdict.PASSED, False),
        (OracleVerdict.PASSED, (), False, AssertionVerdict.FAILED, False),
        (OracleVerdict.FAILED, (), False, AssertionVerdict.PASSED, False),
        (
            OracleVerdict.PASSED,
            (OracleVerdict.PASSED,),
            True,
            AssertionVerdict.PASSED,
            False,
        ),
        (
            OracleVerdict.PASSED,
            (OracleVerdict.FAILED,),
            True,
            AssertionVerdict.INCONCLUSIVE,
            True,
        ),
        (
            OracleVerdict.FAILED,
            (OracleVerdict.PASSED,),
            False,
            AssertionVerdict.INCONCLUSIVE,
            True,
        ),
        (None, (), True, AssertionVerdict.NOT_EVALUATED, False),
        (
            OracleVerdict.UNAVAILABLE,
            (),
            True,
            AssertionVerdict.NOT_EVALUATED,
            False,
        ),
        (OracleVerdict.PASSED, (), None, AssertionVerdict.NOT_EVALUATED, False),
    ],
)
def test_oracle_binding_adjudication(
    primary, supporting, observed_match, expected_verdict, expected_conflict
):
    evaluation = evaluate_oracle_binding(primary, supporting, observed_match=observed_match)
    assert evaluation.verdict is expected_verdict
    assert evaluation.oracle_conflict is expected_conflict


def test_primary_never_overrides_supporting():
    """P03 is pending: disagreement must stay INCONCLUSIVE, never resolved
    by assuming Primary wins."""
    evaluation = evaluate_oracle_binding(
        OracleVerdict.PASSED,
        (OracleVerdict.FAILED,),
        observed_match=True,
    )
    assert evaluation.verdict is AssertionVerdict.INCONCLUSIVE
    assert "P03 pending" in " ".join(evaluation.notes)


def test_verdict_and_execution_status_are_independent():
    """Same verdict can accompany different execution observations and vice
    versa; aggregation never invents an execution_status."""
    from packages.execution.verdict import ExecutionStatus  # noqa: F401

    passed_complete = aggregate_verdict([req("a1", P)], execution_completed=True)
    passed_aborted = aggregate_verdict([req("a1", P)], execution_completed=False)
    assert passed_complete.verdict is not passed_aborted.verdict
    assert not hasattr(passed_complete, "execution_status")
