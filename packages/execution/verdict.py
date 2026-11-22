"""Deterministic Test Verdict aggregation — stage 2 frozen rules.

This module is the only component of the stage-2 kernel whose rules are
FROZEN in the v1.1 roadmap (lines 441-487). It deliberately has no runtime
dependency on Playwright, FastAPI, the database, or any Agent: given the same
inputs it always produces the same output.

Frozen rule summary (verbatim intent from the roadmap):
  * A formal TestCase must have at least one required Assertion. An empty
    required set is ILLEGAL and must never auto-PASS.
  * Assertion-level states: PASSED / FAILED / INCONCLUSIVE / NOT_EVALUATED.
  * Any required FAILED -> TestVerdict.FAILED (never masked by later aborts).
  * All required PASSED + clean execution + valid oracles -> PASSED.
  * No confirmed failure but some required not adjudicated -> INCONCLUSIVE.
  * No required assertion effectively evaluated -> NOT_EVALUATED.
  * Optional assertions never decide the verdict, but keep their own result.
  * A required assertion with an unavailable oracle must not yield PASSED.
  * Conflicting oracles that cannot be adjudicated by a FORMAL rule ->
    INCONCLUSIVE.  P03 (multi-source priority) is still pending, so there is
    deliberately NO built-in "Primary overrides Supporting" policy here.
  * execution_status and test_verdict are independent dimensions. This
    module never derives execution_status from a verdict; it only takes an
    independent observation ``execution_completed`` as a gate against PASSED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AssertionVerdict(StrEnum):
    """Four assertion-level states (frozen)."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EVALUATED = "NOT_EVALUATED"


class TestVerdict(StrEnum):
    """Four run-level states (frozen)."""

    __test__ = False  # prevent pytest from collecting this enum as a test class

    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EVALUATED = "NOT_EVALUATED"


class ExecutionStatus(StrEnum):
    """Run lifecycle (frozen in 状态机规范 §3). Independent of test_verdict.

    This enum is a shared vocabulary only. Nothing in this module derives an
    ExecutionStatus from a TestVerdict, or vice versa.
    """

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class OracleVerdict(StrEnum):
    """What a single Oracle source says about one assertion."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class InvalidTestCaseError(ValueError):
    """Raised for illegal inputs, e.g. an empty required-assertion set."""


@dataclass(frozen=True)
class AssertionResult:
    """Per-assertion evaluation result.

    ``verdict`` is the effective state after Oracle evaluation. The three
    flags carry the reasons that must never be collapsed into PASSED:
      - ``oracle_available``: was the formal Oracle readable at evaluation time?
      - ``oracle_conflict``: did Primary/Supporting disagree with no formal rule?
      - ``evaluated``: did this assertion actually run to a usable conclusion?
    """

    assertion_id: str
    required: bool
    verdict: AssertionVerdict
    oracle_available: bool = True
    oracle_conflict: bool = False
    evaluated: bool = True
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def is_effective(self) -> bool:
        """Was this assertion effectively evaluated?

        Per roadmap line 464, INCONCLUSIVE is "evaluated but not adjudicable",
        so it still counts as an effective evaluation. NOT_EVALUATED (never
        ran) and an unavailable Oracle do NOT count. Conflict alone does not
        erase the fact that evaluation happened; the aggregation step routes
        conflicts to INCONCLUSIVE separately.
        """
        return (
            self.evaluated
            and self.oracle_available
            and self.verdict is not AssertionVerdict.NOT_EVALUATED
        )


@dataclass(frozen=True)
class VerdictOutcome:
    """Verdict plus an audit trail of why it was reached."""

    verdict: TestVerdict
    required_count: int
    effective_count: int
    failed_ids: tuple[str, ...] = ()
    unresolved_ids: tuple[str, ...] = ()
    optional_results: tuple[AssertionResult, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OracleBindingEvaluation:
    """Result of evaluating one Oracle binding for one assertion."""

    verdict: AssertionVerdict
    oracle_available: bool
    oracle_conflict: bool
    evaluated: bool
    notes: tuple[str, ...] = ()


@dataclass
class _Buckets:
    failed: list[str] = field(default_factory=list)
    effective: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def evaluate_oracle_binding(
    primary: OracleVerdict | None,
    supporting: tuple[OracleVerdict, ...] = (),
    *,
    observed_match: bool | None = None,
) -> OracleBindingEvaluation:
    """Evaluate a Primary/Supporting Oracle binding into an assertion state.

    P03 (multi-source priority, trust levels, complex conflict adjudication)
    is still pending. Therefore this function implements the FROZEN fallback
    only: disagreement that cannot be resolved by a formal rule yields
    INCONCLUSIVE. It deliberately does NOT let Primary override Supporting.

    ``observed_match`` is the runner's observation of the behaviour; it is
    compared with the oracle's stated expectation to produce PASSED/FAILED.
    When the observation itself is unavailable, the assertion is NOT_EVALUATED.
    """
    if primary is None and not supporting:
        return OracleBindingEvaluation(
            verdict=AssertionVerdict.NOT_EVALUATED,
            oracle_available=False,
            oracle_conflict=False,
            evaluated=False,
            notes=("no oracle source bound",),
        )

    declared = [v for v in (primary, *supporting) if v is not None]
    usable = [v for v in declared if v is not OracleVerdict.UNAVAILABLE]
    if not usable:
        return OracleBindingEvaluation(
            verdict=AssertionVerdict.NOT_EVALUATED,
            oracle_available=False,
            oracle_conflict=False,
            evaluated=False,
            notes=("all bound oracles unavailable",),
        )

    passed = [v for v in usable if v is OracleVerdict.PASSED]
    failed = [v for v in usable if v is OracleVerdict.FAILED]
    if passed and failed:
        return OracleBindingEvaluation(
            verdict=AssertionVerdict.INCONCLUSIVE,
            oracle_available=True,
            oracle_conflict=True,
            evaluated=True,
            notes=(
                "oracle sources disagree; P03 pending, no formal adjudication "
                "rule, so no side is allowed to override the other",
            ),
        )

    if observed_match is None:
        return OracleBindingEvaluation(
            verdict=AssertionVerdict.NOT_EVALUATED,
            oracle_available=True,
            oracle_conflict=False,
            evaluated=False,
            notes=("behaviour observation unavailable",),
        )

    expected = passed[0] if passed else failed[0]
    agree = (expected is OracleVerdict.PASSED and observed_match) or (
        expected is OracleVerdict.FAILED and not observed_match
    )
    return OracleBindingEvaluation(
        verdict=AssertionVerdict.PASSED if agree else AssertionVerdict.FAILED,
        oracle_available=True,
        oracle_conflict=False,
        evaluated=True,
        notes=("all available oracles agree on the expectation",),
    )


def aggregate_verdict(
    results: tuple[AssertionResult, ...] | list[AssertionResult],
    *,
    execution_completed: bool = True,
) -> VerdictOutcome:
    """Aggregate assertion results into the frozen Test Verdict.

    ``execution_completed`` is an independent observation of whether the run
    reached normal termination. It is NOT the execution_status field and is
    never derived from a verdict; it exists solely to stop a run that aborted
    after all assertions from being auto-labelled PASSED (rule 6).
    """
    required = [r for r in results if r.required]
    optional = tuple(r for r in results if not r.required)

    if not required:
        raise InvalidTestCaseError(
            "a formal TestCase must declare at least one required Assertion; "
            "an empty required set must never auto-PASS"
        )

    buckets = _Buckets()
    for result in required:
        if result.verdict is AssertionVerdict.FAILED:
            buckets.failed.append(result.assertion_id)
        if result.is_effective():
            buckets.effective.append(result.assertion_id)
        # Unresolved = no effective evaluation at all, OR an evaluation that
        # reached INCONCLUSIVE, OR an unresolved oracle conflict (P03 pending).
        if (
            not result.is_effective()
            or result.verdict is AssertionVerdict.INCONCLUSIVE
            or result.oracle_conflict
        ):
            buckets.unresolved.append(result.assertion_id)

    notes: list[str] = []
    if buckets.failed:
        verdict = TestVerdict.FAILED
        notes.append("at least one required assertion confirmed FAILED")
    elif not buckets.effective:
        verdict = TestVerdict.NOT_EVALUATED
        notes.append("no required assertion was effectively evaluated")
    elif buckets.unresolved:
        verdict = TestVerdict.INCONCLUSIVE
        notes.append("no confirmed failure, but some required assertions are unresolved")
    elif not execution_completed:
        verdict = TestVerdict.INCONCLUSIVE
        notes.append("all required assertions passed but execution did not complete cleanly")
    elif any(r.oracle_conflict for r in required):
        # Defence in depth: even a hand-built result claiming PASSED must not
        # win while an oracle conflict is flagged (frozen rule 3).
        verdict = TestVerdict.INCONCLUSIVE
        notes.append("oracle conflict flagged on a required assertion")
    else:
        verdict = TestVerdict.PASSED
        notes.append("all required assertions passed on valid oracles and execution completed")

    return VerdictOutcome(
        verdict=verdict,
        required_count=len(required),
        effective_count=len(buckets.effective),
        failed_ids=tuple(buckets.failed),
        unresolved_ids=tuple(buckets.unresolved),
        optional_results=optional,
        notes=tuple(notes),
    )
