"""Stage-6 TestCase Compiler — deterministic Trace -> Candidate TestCase.

Pipeline (v1.1 §阶段6): Trace Normalizer -> Action Extractor -> Locator Resolver
-> Assertion Constructor -> Oracle Binder -> Static Validation -> Stability
Validator -> Human Review -> Formal TestCaseVersion.

HONESTY / GUARDRAILS:
  * A Candidate is NOT a formal TestCaseVersion and is NOT approved. Promotion
    requires an explicit human approval record (enforced by ``promote``).
  * Without a business-state interface, only UI behaviour may be claimed as
    verified — the compiler never fabricates a persistence Oracle.
  * Provenance is recorded: source_exploration_id, source_trace_hash,
    compiler_version, locator_strategy_version, reviewer, approval_record.
  * Locator priority follows LocatorRegistry (role > label > ... > css).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from packages.agents.exploration import ExplorationTrace
from packages.core.errors import ValidationError
from packages.execution.models import (
    AssertionSpec,
    CleanupPolicy,
    OracleBinding,
    OracleSpec,
    TestCaseVersion,
    TestIntent,
    TestStep,
)
from packages.execution.verdict import OracleVerdict

COMPILER_VERSION = "1.0.0"
LOCATOR_STRATEGY_VERSION = "1.0.0"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CandidateTestCase:
    """A reviewable candidate — intentionally NOT a formal TestCaseVersion."""

    candidate_id: str
    case_id: str
    title: str
    intent: TestIntent
    steps: tuple[TestStep, ...]
    assertions: tuple[AssertionSpec, ...]
    oracle_bindings: tuple[OracleBinding, ...]
    cleanup: CleanupPolicy
    source_exploration_id: str
    source_trace_hash: str
    compiler_version: str = COMPILER_VERSION
    prompt_version: str = "none"
    model_version: str = "deterministic-rules-NOT-LLM"
    locator_strategy_version: str = LOCATOR_STRATEGY_VERSION
    reviewer: str = ""
    approval_record: str = ""
    uncertainty_notes: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TestCaseCompiler:
    """Pure, deterministic compiler from an ExplorationTrace to a Candidate."""

    def normalize(self, trace: ExplorationTrace) -> list[dict[str, Any]]:
        """Trace Normalizer: drop rejected calls, keep ordered tool calls."""
        return [
            dict(a)
            for a in trace.action_sequence
            if not a.get("rejected")
            and a.get("tool") in {"navigate", "fill", "click", "wait", "snapshot"}
        ]

    def extract(self, actions: list[dict[str, Any]]) -> tuple[list[TestStep], list[str]]:
        """Action Extractor: map tool calls to deterministic TestSteps + notes."""
        steps: list[TestStep] = []
        notes: list[str] = []
        for i, action in enumerate(actions, start=1):
            tool = action["tool"]
            args = action.get("args", {})
            if tool == "navigate":
                steps.append(TestStep(f"s{i}", "navigate", str(args.get("url", ""))))
            elif tool == "fill":
                steps.append(
                    TestStep(
                        f"s{i}",
                        "fill",
                        str(args.get("selector", "")),
                        value=str(args.get("value", "")),
                    )
                )
            elif tool == "click":
                steps.append(TestStep(f"s{i}", "click", str(args.get("selector", ""))))
            elif tool == "wait":
                steps.append(TestStep(f"s{i}", "wait", "", value=str(args.get("ms", 200))))
            elif tool == "snapshot":
                notes.append(f"s{i}: snapshot is observation-only, not an assertion")
        return steps, notes

    def bind_oracles(
        self, trace: ExplorationTrace, steps: tuple[TestStep, ...]
    ) -> tuple[tuple[AssertionSpec, ...], tuple[OracleBinding, ...], tuple[str, ...]]:
        """Assertion Constructor + Oracle Binder.

        Only UI-observable behaviour is asserted from a trace. No business
        persistence Oracle is fabricated without a state interface.
        """
        notes: list[str] = []
        assertions: list[AssertionSpec] = []
        bindings: list[OracleBinding] = []
        ui_step = next((s for s in steps if s.action == "click"), steps[-1] if steps else None)
        if ui_step is not None:
            aid = "a1"
            assertions.append(AssertionSpec(aid, "explored UI action completed", required=True))
            spec = OracleSpec(
                oracle_id="o1-ui-observed",
                source_type="ui",
                source_ref=f"ui.{ui_step.step_id}.passed",
                expected=OracleVerdict.PASSED,
                expected_value=True,
                source=f"derived from exploration {trace.exploration_id}",
                approved=False,  # candidate Oracle is NEVER pre-approved
            )
            bindings.append(OracleBinding(aid, primary=spec))
            notes.append(
                "UI-only Oracle: business persistence NOT verified (no state interface in trace)"
            )
        return tuple(assertions), tuple(bindings), tuple(notes)

    def validate(self, candidate: CandidateTestCase) -> None:
        """Static Validation: structural integrity, no approval bypass."""
        if not candidate.steps:
            raise ValidationError("candidate has no steps")
        if not candidate.assertions or not any(a.required for a in candidate.assertions):
            raise ValidationError("candidate has no required assertion")
        bound = {b.assertion_id for b in candidate.oracle_bindings}
        if {a.assertion_id for a in candidate.assertions} != bound:
            raise ValidationError("candidate assertion/oracle binding mismatch")
        if not candidate.source_trace_hash.startswith("sha256:"):
            raise ValidationError("candidate has no source trace hash")

    def compile(self, trace: ExplorationTrace, *, case_id: str, title: str) -> CandidateTestCase:
        actions = self.normalize(trace)
        steps_list, step_notes = self.extract(actions)
        steps = tuple(steps_list)
        assertions, bindings, oracle_notes = self.bind_oracles(trace, steps)
        candidate = CandidateTestCase(
            candidate_id=f"cand_{hashlib.sha256(trace.source_trace_hash.encode()).hexdigest()[:10]}",
            case_id=case_id,
            title=title,
            intent=TestIntent(
                f"i-{trace.exploration_id}",
                f"compiled from exploration {trace.exploration_id}",
                source_ref=trace.test_intent_ref,
            ),
            steps=steps,
            assertions=assertions,
            oracle_bindings=bindings,
            cleanup=CleanupPolicy(),
            source_exploration_id=trace.exploration_id,
            source_trace_hash=trace.source_trace_hash,
            uncertainty_notes=tuple(step_notes) + oracle_notes,
        )
        self.validate(candidate)
        return candidate

    def promote(
        self, candidate: CandidateTestCase, *, reviewer: str, approval_record: str, version: int = 1
    ) -> TestCaseVersion:
        """HUMAN REVIEW gate: only an explicit approval promotes to formal.

        Does not mutate the candidate. The resulting formal version carries an
        approved-by reference; the candidate Oracle remains unapproved until a
        human approves it (caller must supply/record that separately).
        """
        if not reviewer or not approval_record:
            raise ValidationError("promotion requires reviewer and approval_record")
        formal = TestCaseVersion(
            case_id=candidate.case_id,
            version=version,
            title=candidate.title,
            intent=candidate.intent,
            steps=candidate.steps,
            assertions=candidate.assertions,
            oracle_bindings=candidate.oracle_bindings,
            cleanup=candidate.cleanup,
            compiled_from=f"compiler:{candidate.compiler_version}",
            approved_by=f"{reviewer}:{approval_record}",
        )
        # Formal TestCaseVersion.validate() rejects unapproved oracles — this is
        # the structural guard that prevents auto-approval. Caller must obtain a
        # human-approved Oracle binding before promotion succeeds.
        formal.validate()
        return formal
