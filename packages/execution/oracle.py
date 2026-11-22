"""Oracle Evaluator (stage 2, C03).

Critical trust boundary: an Oracle reads observable facts — the business-plane
independent state API and UI observations — and compares them with an
approved expected value. It NEVER reads ``/__eval__``, Ground Truth, or fault
config. That independence is what makes the verdict trustworthy.

Multi-source adjudication follows the FROZEN fallback: sources that agree
produce a decisive verdict; disagreement with no formal rule (P03 pending)
yields INCONCLUSIVE — Primary is never allowed to silently override Support.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from packages.execution.models import OracleBinding, OracleSpec
from packages.execution.verdict import (
    AssertionVerdict,
    OracleBindingEvaluation,
    OracleVerdict,
)

# A source reader maps a logical source ref to an observed value.
#   "state.project_count"   -> int (from independent business API)
#   "ui.success_visible"    -> bool (from browser observation)
#   None                    -> source unavailable
SourceReader = Callable[[str], Any | None]


@dataclass(frozen=True)
class SourceObservation:
    ref: str
    available: bool
    value: Any = None


class OracleEvaluator:
    """Evaluates Oracle bindings from observations collected by the runner."""

    def __init__(self, source_reader: SourceReader) -> None:
        self._read_source = source_reader

    def observe(self, ref: str) -> SourceObservation:
        value = self._read_source(ref)
        if value is None:
            return SourceObservation(ref=ref, available=False)
        if isinstance(value, dict) and value.get("available") is False:
            return SourceObservation(ref=ref, available=False)
        if isinstance(value, dict) and "value" in value:
            return SourceObservation(ref=ref, available=True, value=value["value"])
        return SourceObservation(ref=ref, available=True, value=value)

    def evaluate(self, binding: OracleBinding) -> OracleBindingEvaluation:
        primary = self._judge(binding.primary)
        supporting = tuple(self._judge(o) for o in binding.supporting)
        return self._aggregate(primary, supporting, binding)

    def _judge(self, spec: OracleSpec) -> OracleVerdict:
        observation = self.observe(spec.source_ref)
        if not observation.available:
            return OracleVerdict.UNAVAILABLE
        if spec.source_type == "manual" or spec.expected_value is None:
            # Direct verdict supplied by an approved manual Oracle.
            return spec.expected
        matched = observation.value == spec.expected_value
        return OracleVerdict.PASSED if matched else OracleVerdict.FAILED

    @staticmethod
    def _aggregate(
        primary: OracleVerdict,
        supporting: tuple[OracleVerdict, ...],
        binding: OracleBinding,
    ) -> OracleBindingEvaluation:
        usable = [v for v in (primary, *supporting) if v is not OracleVerdict.UNAVAILABLE]
        all_sources = (primary, *supporting)

        if not usable:
            return OracleBindingEvaluation(
                verdict=AssertionVerdict.NOT_EVALUATED,
                oracle_available=False,
                oracle_conflict=False,
                evaluated=False,
                notes=("all bound oracles unavailable",),
            )

        passed = all(v is OracleVerdict.PASSED for v in usable)
        failed = all(v is OracleVerdict.FAILED for v in usable)
        mixed = not passed and not failed

        if mixed:
            return OracleBindingEvaluation(
                verdict=AssertionVerdict.INCONCLUSIVE,
                oracle_available=True,
                oracle_conflict=True,
                evaluated=True,
                notes=(
                    f"oracle sources disagree ({[v.value for v in all_sources]}); "
                    "P03 pending, no formal adjudication rule, so no source overrides another",
                ),
            )

        if len(usable) < len(all_sources):
            # Some sources unavailable but no disagreement — evidence incomplete.
            # An incomplete evidence set can still FAILED (a confirmed failure
            # must not be masked), but cannot be a clean PASSED.
            verdict = AssertionVerdict.FAILED if failed else AssertionVerdict.INCONCLUSIVE
            return OracleBindingEvaluation(
                verdict=verdict,
                oracle_available=True,
                oracle_conflict=False,
                evaluated=True,
                notes=("some oracle sources unavailable; evidence incomplete",),
            )

        verdict = AssertionVerdict.PASSED if passed else AssertionVerdict.FAILED
        return OracleBindingEvaluation(
            verdict=verdict,
            oracle_available=True,
            oracle_conflict=False,
            evaluated=True,
            notes=(f"all oracle sources agree: {verdict.value}",),
        )
