"""Stage-6 TestCase Compiler — deterministic Trace -> Candidate TestCase.

Pipeline (v1.1 §阶段6): Trace Normalizer -> Action Extractor -> Locator Resolver
-> Assertion Constructor -> Oracle Binder -> Static Validation -> Stability
Validator -> Human Review -> Formal TestCaseVersion.

R5-4.1 — why this file was rewritten
------------------------------------
The previous compiler proved only that the explored click still worked:
``ui.<first_click>.passed``. That is why a wrong password could still come out
PASSED — the button was clickable, so "the test passed" while the business goal
had not been reached.

Assertions are now derived from *goal evidence* in the structured observation
sequence, and every assertion states where it came from:

  * ``ui.url_path``            — the page the goal lives on was actually reached
  * ``ui.<step>.passed``       — a destination marker is visible / a source-page
                                 control is gone (the state transition happened)
  * ``state.<key>``            — an independent business-state Oracle, only when
                                 a human supplied the expectation in the intent
                                 (``required_assertions=["state.project_count=3"]``)

A click is never an Oracle. When no goal-level assertion can be justified, the
compiler REFUSES to compile instead of emitting a candidate that looks verified.

HONESTY / GUARDRAILS:
  * A Candidate is NOT a formal TestCaseVersion and is NOT approved. Promotion
    requires an explicit human approval record (enforced by ``promote``).
  * The compiler never invents an expected business value: expectations come
    from the human-written intent or from an observed transition, and both are
    named in the assertion's ``source``.
  * Sensitive inputs become ``value_ref`` (provider-resolved at run time). The
    redaction placeholder is never copied into a step value.
  * Provenance is recorded: source_exploration_id, source_trace_hash,
    compiler_version, locator_strategy_version, reviewer, approval_record.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from packages.agents.exploration import ExplorationTrace
from packages.core.errors import ValidationError
from packages.execution.dataref import derive_value_ref, is_placeholder
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

COMPILER_VERSION = "1.1.0"
LOCATOR_STRATEGY_VERSION = "1.0.0"

_STATE_ASSERTION = re.compile(r"^state\.([A-Za-z0-9_.]+)\s*=\s*(.+)$")
_NAV_TOOLS = {"navigate", "fill", "click", "wait", "snapshot"}


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
    # Review surface: a human must be able to judge the candidate without
    # re-reading the raw trace.
    target: str = ""
    observations: tuple[dict[str, Any], ...] = ()
    data_refs: tuple[TestDataRef, ...] = ()
    review_checklist: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _AssertionDraft:
    """One assertion plus the step that produces its observation."""

    assertion: AssertionSpec
    binding: OracleBinding
    step: TestStep | None = None


class TestCaseCompiler:
    """Pure, deterministic compiler from an ExplorationTrace to a Candidate."""

    # ------------------------------------------------------------- normalize

    def normalize(self, trace: ExplorationTrace) -> list[dict[str, Any]]:
        """Trace Normalizer: drop rejected calls, keep ordered tool calls."""
        return [
            dict(a)
            for a in trace.action_sequence
            if not a.get("rejected") and a.get("tool") in _NAV_TOOLS and not a.get("error")
        ]

    def extract(
        self, actions: list[dict[str, Any]]
    ) -> tuple[list[TestStep], list[TestDataRef], list[str]]:
        """Action Extractor: map tool calls to TestSteps, bindings and notes."""
        steps: list[TestStep] = []
        data_refs: list[TestDataRef] = []
        notes: list[str] = []
        for i, action in enumerate(actions, start=1):
            tool = action["tool"]
            args = action.get("args", {})
            if tool == "navigate":
                steps.append(TestStep(f"s{i}", "navigate", str(args.get("url", ""))))
            elif tool == "fill":
                selector = str(args.get("selector", ""))
                raw_value = str(args.get("value", ""))
                ref = str(args.get("value_ref") or "")
                if ref or is_placeholder(raw_value):
                    # The trace never holds the plaintext; the case must not
                    # either. Bind the field to a provider-resolved reference.
                    ref = ref or derive_value_ref(selector)
                    steps.append(TestStep(f"s{i}", "fill", selector, value_ref=ref))
                    data_refs.append(TestDataRef(ref_id=ref, locator=selector, value=""))
                    notes.append(
                        f"s{i}: sensitive input bound to data ref {ref!r}; "
                        "plaintext is supplied by the test-data provider at run time"
                    )
                else:
                    steps.append(TestStep(f"s{i}", "fill", selector, value=raw_value))
            elif tool == "click":
                steps.append(TestStep(f"s{i}", "click", str(args.get("selector", ""))))
            elif tool == "wait":
                steps.append(TestStep(f"s{i}", "wait", "", value=str(args.get("ms", 200))))
            elif tool == "snapshot":
                notes.append(f"s{i}: snapshot is observation-only, not an assertion")
        return steps, data_refs, notes

    # -------------------------------------------------------------- assertions

    def build_assertions(
        self,
        trace: ExplorationTrace,
        steps: list[TestStep],
        intent: TestIntent | None,
    ) -> tuple[list[TestStep], list[_AssertionDraft], list[str], list[str]]:
        """Assertion Constructor + Oracle Binder.

        Returns (extra_steps, drafts, notes, checklist). Every draft carries the
        step that produces its observation and a Primary Oracle whose ``source``
        names the evidence it came from.
        """
        observations = self._observations(trace)
        notes: list[str] = []
        checklist: list[str] = []
        drafts: list[_AssertionDraft] = []
        extra_steps: list[TestStep] = []
        next_step = len(steps) + 1

        if not observations:
            notes.append(
                "trace contains no structured observation; no goal-level assertion can be derived"
            )
            return extra_steps, drafts, notes, checklist

        first = observations[0]
        final = observations[-1]
        initial_path = str(first.get("url_path") or "/")
        final_path = str(final.get("url_path") or "/")
        initial_order = [str(t) for t in (first.get("visible_testids") or ())]
        final_order = [str(t) for t in (final.get("visible_testids") or ())]
        initial_testids = set(initial_order)
        final_testids = set(final_order)

        expected_path = self._expected_path(intent, final_path)
        path_expectation_source = (
            f"intent.target={intent.target!r}"
            if intent and intent.target
            else "observed transition"
        )
        checklist.append(
            f"确认目标路径 {expected_path!r} 是否代表业务成功（来源：{path_expectation_source}）"
        )
        drafts.append(
            self._draft(
                assertion_id=f"a{len(drafts) + 1}",
                description=f"页面到达目标路径 {expected_path}",
                oracle_id="o-url-path",
                source_type="ui",
                source_ref="ui.url_path",
                expected_value=expected_path,
                source=f"observed exploration {trace.exploration_id} / {path_expectation_source}",
                required=True,
            )
        )

        # 2. Destination marker: an element that the goal page has and the entry
        #    page did not. This is what proves the transition, not the click.
        marker = self._destination_marker(
            initial_testids, final_order, list(final.get("visible_elements") or ())
        )
        if marker:
            step = TestStep(f"s{next_step}", "assert_visible", f"[data-testid={marker}]")
            extra_steps.append(step)
            next_step += 1
            drafts.append(
                self._draft(
                    assertion_id=f"a{len(drafts) + 1}",
                    description=f"目标页面元素 {marker} 可见",
                    oracle_id=f"o-{step.step_id}-visible",
                    source_type="ui",
                    source_ref=f"ui.{step.step_id}.passed",
                    expected_value=True,
                    source=f"observed in trace {trace.exploration_id} after {final_path}",
                    required=True,
                    step=step,
                )
            )
            checklist.append(f"确认 {marker} 是否确实是目标页面的稳定标识（而非临时元素）")
        else:
            notes.append(
                "no new data-testid appeared on the final page; destination could not be "
                "anchored to an element (URL assertion only)"
            )

        # 3. Source-page control is gone: the transition really replaced the old
        #    state (this is what makes a failed login fail, instead of "the
        #    button was clickable").
        vanished = self._vanished_control(initial_order, final_testids)
        if vanished:
            step = TestStep(f"s{next_step}", "assert_not_visible", f"[data-testid={vanished}]")
            extra_steps.append(step)
            next_step += 1
            drafts.append(
                self._draft(
                    assertion_id=f"a{len(drafts) + 1}",
                    description=f"来源页面控件 {vanished} 已不再显示（状态已完成切换）",
                    oracle_id=f"o-{step.step_id}-hidden",
                    source_type="ui",
                    source_ref=f"ui.{step.step_id}.passed",
                    expected_value=True,
                    source=f"observed before/after transition in {trace.exploration_id}",
                    required=True,
                    step=step,
                )
            )
        elif initial_path != final_path:
            notes.append(
                "page changed but no prior control disappeared; state transition is only "
                "partially evidenced"
            )

        # 4. Human-declared expected outcomes that are literally observable.
        for outcome in (intent.expected_outcomes if intent else ())[:6]:
            text = str(outcome).strip()
            if not text:
                continue
            if text in str(final.get("text_excerpt") or ""):
                step = TestStep(f"s{next_step}", "assert_text", "body", value=text)
                extra_steps.append(step)
                next_step += 1
                drafts.append(
                    self._draft(
                        assertion_id=f"a{len(drafts) + 1}",
                        description=f"页面显示预期结果：{text}",
                        oracle_id=f"o-{step.step_id}-text",
                        source_type="ui",
                        source_ref=f"ui.{step.step_id}.passed",
                        expected_value=True,
                        source="intent.expected_outcomes (human) + observed text",
                        required=True,
                        step=step,
                    )
                )
            else:
                checklist.append(f"预期结果未在探索轨迹中观察到，需人工确认：{text}")

        # 5. Independent business-state Oracles: only when a human wrote the
        #    expected value into the intent. The compiler never guesses one.
        for requirement in intent.required_assertions if intent else ():
            match = _STATE_ASSERTION.match(str(requirement).strip())
            if not match:
                if str(requirement).strip():
                    checklist.append(f"未自动生成（非 state.<key>=<json> 形式）：{requirement}")
                continue
            key, raw_expected = match.group(1), match.group(2)
            try:
                expected = json.loads(raw_expected)
            except json.JSONDecodeError:
                expected = raw_expected.strip().strip("'\"")
            drafts.append(
                self._draft(
                    assertion_id=f"a{len(drafts) + 1}",
                    description=f"业务状态 {key} 等于 {expected!r}",
                    oracle_id=f"o-state-{key.replace('.', '-')}",
                    source_type="independent_api",
                    source_ref=f"state.{key}",
                    expected_value=expected,
                    source="intent.required_assertions (human-declared expectation)",
                    required=True,
                )
            )

        return extra_steps, drafts, notes, checklist

    # ----------------------------------------------------------------- compile

    def compile(
        self,
        trace: ExplorationTrace,
        *,
        case_id: str,
        title: str,
        intent: TestIntent | None = None,
    ) -> CandidateTestCase:
        actions = self.normalize(trace)
        steps, data_refs, step_notes = self.extract(actions)
        extra_steps, drafts, oracle_notes, checklist = self.build_assertions(trace, steps, intent)
        if not drafts:
            raise ValidationError(
                "candidate has no business-meaningful assertion; refusing to compile a case "
                "that would only prove a click worked",
                details={"exploration_id": trace.exploration_id},
            )

        all_steps = tuple([*steps, *extra_steps])
        notes = list(step_notes) + oracle_notes
        if not any(d.binding.primary.source_type == "independent_api" for d in drafts):
            notes.append(
                "business persistence NOT verified (no independent state assertion supplied "
                "in intent.required_assertions)"
            )

        effective_intent = intent or TestIntent(
            intent_id=f"i-{trace.exploration_id}",
            description=f"compiled from exploration {trace.exploration_id}",
            source_ref=trace.test_intent_ref,
        )
        candidate = CandidateTestCase(
            candidate_id=f"cand_{hashlib.sha256(trace.source_trace_hash.encode()).hexdigest()[:10]}",
            case_id=case_id,
            title=title,
            intent=effective_intent,
            steps=all_steps,
            assertions=tuple(d.assertion for d in drafts),
            oracle_bindings=tuple(d.binding for d in drafts),
            cleanup=CleanupPolicy(),
            source_exploration_id=trace.exploration_id,
            source_trace_hash=trace.source_trace_hash,
            uncertainty_notes=tuple(notes),
            target=str(effective_intent.target or ""),
            observations=tuple(self._observations(trace)[-4:]),
            data_refs=tuple(data_refs),
            review_checklist=tuple(dict.fromkeys(checklist)),
        )
        self.validate(candidate)
        return candidate

    # -------------------------------------------------------------- validation

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

        # A click alone is never an Oracle (R5-4.1 false-positive guard): every
        # binding must point at an assertion step, a page-level observation or a
        # business-state source — never at the mere success of an action.
        def _is_business_source(source_ref: str) -> bool:
            if source_ref in {"ui.url_path", "ui.title"}:
                return True
            if source_ref.startswith("state."):
                return True
            step_id = source_ref.split(".")[1] if source_ref.count(".") >= 2 else ""
            action = self._action_for(candidate, source_ref) or ""
            return bool(step_id) and action.startswith("assert")

        if not any(_is_business_source(b.primary.source_ref) for b in candidate.oracle_bindings):
            raise ValidationError(
                "every assertion is bound to a plain action outcome; no business-level "
                "assertion was produced"
            )
        declared = {ref.ref_id for ref in candidate.data_refs}
        for step in candidate.steps:
            if step.value_ref and step.value_ref not in declared:
                raise ValidationError(f"step {step.step_id} references undeclared data ref")
            if step.value_ref and step.value:
                raise ValidationError(f"step {step.step_id} carries plaintext next to a data ref")

    # ------------------------------------------------------------------ promote

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
            data_refs=candidate.data_refs,
            cleanup=candidate.cleanup,
            compiled_from=f"compiler:{candidate.compiler_version}",
            approved_by=f"{reviewer}:{approval_record}",
        )
        # Formal TestCaseVersion.validate() rejects unapproved oracles — this is
        # the structural guard that prevents auto-approval. Caller must obtain a
        # human-approved Oracle binding before promotion succeeds.
        formal.validate()
        return formal

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _observations(trace: ExplorationTrace) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for obs in trace.observations or ():
            url = str(obs.get("url") or "")
            if not url or url.startswith("about:"):
                continue
            out.append(dict(obs))
        return out

    @staticmethod
    def _expected_path(intent: TestIntent | None, observed_final: str) -> str:
        """Where the goal lives: the human's target wins over what was observed."""
        raw = (intent.target if intent else "") or ""
        if raw:
            parsed = urlparse(raw) if "://" in raw else None
            path = parsed.path if parsed else raw
            if path and path.startswith("/"):
                return path.rstrip("/") or "/"
        return observed_final or "/"

    @staticmethod
    def _destination_marker(
        initial: set[str], final_order: list[str], elements: list[dict[str, Any]]
    ) -> str:
        """A stable, *visible* testid that only the destination page has.

        Page order is preserved (a browser reports testids in DOM order) and an
        interactive element wins: an empty container is technically present but
        has a zero-size box, so asserting its visibility would fail for a reason
        that has nothing to do with the business goal.
        """
        new = [t for t in final_order if t not in initial and "error" not in t.lower()]
        if not new:
            return ""
        clickable = {
            str(e.get("testid") or "")
            for e in elements
            if str(e.get("tag") or "") in {"a", "button"}
        }
        preferred = [t for t in new if t in clickable]
        return (preferred or new)[0]

    @staticmethod
    def _vanished_control(initial_order: list[str], final: set[str]) -> str:
        """A control the entry page had and the destination page does not."""
        gone = [t for t in initial_order if t not in final and "error" not in t.lower()]
        if not gone:
            return ""
        preferred = [t for t in gone if "submit" in t or "login" in t]
        return (preferred or gone)[0]

    @staticmethod
    def _action_for(candidate: CandidateTestCase, source_ref: str) -> str:
        step_id = source_ref.split(".")[1] if "." in source_ref else ""
        for step in candidate.steps:
            if step.step_id == step_id:
                return step.action
        return ""

    def _draft(
        self,
        *,
        assertion_id: str,
        description: str,
        oracle_id: str,
        source_type: str,
        source_ref: str,
        expected_value: Any,
        source: str,
        required: bool,
        step: TestStep | None = None,
    ) -> _AssertionDraft:
        assertion = AssertionSpec(assertion_id, description, required=required)
        binding = OracleBinding(
            assertion_id,
            primary=OracleSpec(
                oracle_id=oracle_id,
                source_type=source_type,
                source_ref=source_ref,
                expected=OracleVerdict.PASSED,
                expected_value=expected_value,
                source=source,
                approved=False,  # candidate Oracle is NEVER pre-approved
                approved_by="",
            ),
        )
        return _AssertionDraft(assertion=assertion, binding=binding, step=step)
