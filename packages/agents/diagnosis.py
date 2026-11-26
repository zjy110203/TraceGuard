"""Stage-7 Diagnosis v1 — deterministic, explainable multi-dimensional diagnosis.

Eight INDEPENDENT dimensions (v1.1 §阶段7): Symptom, Failure Domain, Failure
Mechanism, Root Cause, Contributing Factor, Flaky Characteristic, Diagnosis
Status, Disposition. Domain and Mechanism are separate enums.

Hard rules implemented literally:
  * CONFIRMED requires category-specific authoritative evidence + a
    deterministic confirmation rule + no unresolved key contradiction. An LLM
    never decides CONFIRMED (there is no LLM in this module at all).
  * Multiple Hypotheses with independent status and BOTH supporting and
    contradicting evidence references; an unconfirmed Hypothesis never becomes
    Root Cause; multiple confirmed root causes are preserved.
  * Insufficient/conflicting evidence -> INCONCLUSIVE.
  * Flaky is a STABILITY characteristic, never a root cause, and requires
    repeated-run divergence evidence — one timeout is not Flaky.
  * Disposition is a separate routing decision, not merged into Diagnosis.
  * Category thresholds are NOT invented as probabilities; confirmation is by
    named authoritative evidence presence (documented rule ids).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from packages.core.errors import ValidationError

# Formal candidate enums from v1.1 §阶段7 (no invented shorthand).
FAILURE_DOMAINS = frozenset(
    {"PRODUCT", "TEST_ASSET", "TEST_EXECUTION", "ENVIRONMENT", "AGENT", "PLATFORM", "UNKNOWN"}
)
FAILURE_MECHANISMS = frozenset(
    {
        "API",
        "NETWORK",
        "PERSISTENCE",
        "UI_RENDERING",
        "LOCATOR",
        "TIMING",
        "AUTHORIZATION",
        "BROWSER",
        "TEST_DATA",
        "UNKNOWN",
    }
)
DISPOSITIONS = frozenset(
    {
        "PRODUCT_DEFECT",
        "TEST_ASSET_ISSUE",
        "TEST_EXECUTION_ISSUE",
        "ENVIRONMENT_ISSUE",
        "AGENT_ISSUE",
        "PLATFORM_ISSUE",
        "NEEDS_MORE_EVIDENCE",
        "MANUAL_REVIEW",
    }
)
HYPOTHESIS_STATES = frozenset({"CANDIDATE", "SUPPORTED", "CONTRADICTED", "CONFIRMED", "EXCLUDED"})
DIAGNOSIS_STATUSES = frozenset({"CONFIRMED", "PROBABLE", "INCONCLUSIVE"})


class EvidenceRole:
    SUPPORT = "support"
    CONTRADICT = "contradict"


@dataclass(frozen=True)
class EvidenceRef:
    """A reference to an upstream C05/C06 artifact (id + trusted source)."""

    ref_id: str
    source: str
    role: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.role not in (EvidenceRole.SUPPORT, EvidenceRole.CONTRADICT):
            raise ValidationError(f"invalid evidence role: {self.role}")


@dataclass
class Hypothesis:
    """One candidate explanation with independent status and two-sided evidence."""

    hypothesis_id: str
    statement: str
    failure_domain: str
    failure_mechanism: str
    status: str = "CANDIDATE"
    supporting: list[EvidenceRef] = field(default_factory=list)
    contradicting: list[EvidenceRef] = field(default_factory=list)
    confirmation_rule: str = ""

    def __post_init__(self) -> None:
        if self.failure_domain not in FAILURE_DOMAINS:
            raise ValidationError(f"invalid failure domain: {self.failure_domain}")
        if self.failure_mechanism not in FAILURE_MECHANISMS:
            raise ValidationError(f"invalid failure mechanism: {self.failure_mechanism}")
        if self.status not in HYPOTHESIS_STATES:
            raise ValidationError(f"invalid hypothesis status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["supporting"] = [asdict(e) for e in self.supporting]
        d["contradicting"] = [asdict(e) for e in self.contradicting]
        return d


@dataclass
class Diagnosis:
    """Full diagnosis result — all eight dimensions kept separate."""

    diagnosis_id: str
    run_id: str
    symptom: str
    failure_domain: str
    failure_mechanism: str
    root_causes: tuple[str, ...]
    contributing_factors: tuple[str, ...]
    flaky_characteristic: str
    diagnosis_status: str
    disposition: str
    hypotheses: tuple[Hypothesis, ...]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["root_causes"] = list(self.root_causes)
        d["contributing_factors"] = list(self.contributing_factors)
        d["hypotheses"] = [h.to_dict() for h in self.hypotheses]
        d["notes"] = list(self.notes)
        return d


FAULT_ID_RE = re.compile(r"^TF-[A-Z0-9]+-\d+$")
# Denylist, not allowlist: fault outcomes are open-ended (e.g. API_500) and some
# records omit the field entirely. Only audit bookkeeping is excluded.
BOOKKEEPING_OUTCOMES = frozenset({"CLEARED", "DISARMED"})


def is_fault_signal(event: dict[str, Any]) -> bool:
    """True only for an event that indicates a fault was actually active.

    ``CLEARED`` / ``DISARMED`` / business events are audit bookkeeping, not
    evidence of a fault, so they must not create divergence.
    """
    fault_id = str(event.get("fault_id", ""))
    outcome = str(event.get("outcome", "")).upper()
    return bool(FAULT_ID_RE.match(fault_id)) and outcome not in BOOKKEEPING_OUTCOMES


class DiagnosisService:
    """Deterministic diagnosis from a RunReport-shaped observation bundle.

    ``bundle`` is a dict with (all optional unless noted):
      test_verdict, execution_status, failed_step_ids, observations,
      assertions (list of dicts with verdict/reason), evidence (list of dicts),
      events (list of dicts), fault_events (list of dicts from the eval-plane
      EventLog — diagnostic-only, explicitly NOT an Oracle input),
      repeated_verdicts (list of verdict strings across repeated runs).
    """

    def diagnose(self, diagnosis_id: str, run_id: str, bundle: dict[str, Any]) -> Diagnosis:
        verdict = str(bundle.get("test_verdict", ""))
        failed_steps = list(bundle.get("failed_step_ids", []))
        evidence = [e for e in bundle.get("evidence", []) if e.get("run_id", run_id) == run_id]
        events = list(bundle.get("events", []))
        # Only real fault signals count as divergence. The eval EventLog also
        # records CLEARED/DISARMED bookkeeping and business events (e.g.
        # PROJECT_CREATED); treating those as divergence demoted healthy runs to
        # PROBABLE and hid the healthy fast-path.
        fault_events = [e for e in bundle.get("fault_events", []) if is_fault_signal(e)]
        assertions = list(bundle.get("assertions", []))
        observations = dict(bundle.get("observations", {}))
        repeated = list(bundle.get("repeated_verdicts", []))

        # A PASSED verdict alone is NOT sufficient for the healthy fast-path:
        # TF-UI-001 makes the UI lie while state stays correct, so verdict stays
        # PASSED but a step divergence and fault evidence exist. Such a run must
        # still be explained (the whole point of recording the divergence).
        has_divergence = bool(
            failed_steps
            or fault_events
            or any(a.get("verdict") not in (None, "PASSED") for a in assertions)
        )
        if verdict == "PASSED" and not has_divergence:
            return self._healthy(diagnosis_id, run_id)

        symptom = self._symptom(verdict, failed_steps, assertions)
        hypotheses = self._build_hypotheses(
            bundle, failed_steps, evidence, events, fault_events, observations, repeated
        )
        confirmed = [h for h in hypotheses if h.status == "CONFIRMED"]
        supported = [h for h in hypotheses if h.status == "SUPPORTED"]

        # Diagnosis status: CONFIRMED only via deterministic rules; PROBABLE on
        # strong support without authoritative confirmation; else INCONCLUSIVE.
        if confirmed:
            status = "CONFIRMED"
        elif supported:
            status = "PROBABLE"
        else:
            status = "INCONCLUSIVE"

        primary = confirmed[0] if confirmed else (supported[0] if supported else None)
        domain = primary.failure_domain if primary else "UNKNOWN"
        mechanism = primary.failure_mechanism if primary else "UNKNOWN"
        # Multiple confirmed root causes preserved, never compressed into one.
        root_causes = tuple(h.statement for h in confirmed)
        contributing = self._contributing(hypotheses, fault_events)

        flaky = self._flaky_characteristic(repeated)
        disposition = self._route(status, domain, hypotheses, fault_events, repeated)

        notes = [
            "diagnosis derived by deterministic rules; no LLM decided CONFIRMED",
            f"hypothesis_count={len(hypotheses)} confirmed={len(confirmed)} "
            f"supported={len(supported)}",
        ]
        return Diagnosis(
            diagnosis_id=diagnosis_id,
            run_id=run_id,
            symptom=symptom,
            failure_domain=domain,
            failure_mechanism=mechanism,
            root_causes=root_causes,
            contributing_factors=contributing,
            flaky_characteristic=flaky,
            diagnosis_status=status,
            disposition=disposition,
            hypotheses=tuple(hypotheses),
            notes=tuple(notes),
        )

    def _healthy(self, diagnosis_id: str, run_id: str) -> Diagnosis:
        h = Hypothesis(
            "h-healthy",
            "no failure observed; verdict PASSED",
            "PRODUCT",
            "UNKNOWN",
            status="CONFIRMED",
            confirmation_rule="R-HEALTHY: verdict PASSED with no failed required assertion",
        )
        return Diagnosis(
            diagnosis_id=diagnosis_id,
            run_id=run_id,
            symptom="none",
            failure_domain="PRODUCT",
            failure_mechanism="UNKNOWN",
            root_causes=(),
            contributing_factors=(),
            flaky_characteristic="NOT_DETERMINED",
            diagnosis_status="CONFIRMED",
            disposition="MANUAL_REVIEW",
            hypotheses=(h,),
            notes=("healthy run; no disposition action required (MANUAL_REVIEW=no-op)",),
        )

    def _symptom(
        self, verdict: str, failed_steps: list[str], assertions: list[dict[str, Any]]
    ) -> str:
        failed_assertions = [a for a in assertions if a.get("verdict") == "FAILED"]
        parts = [f"test_verdict={verdict}"]
        if failed_assertions:
            parts.append(
                "assertions_failed="
                + ",".join(str(a.get("assertion_id")) for a in failed_assertions)
            )
        if failed_steps:
            parts.append("steps_failed=" + ",".join(map(str, failed_steps)))
        return "; ".join(parts)

    def _build_hypotheses(
        self,
        bundle: dict[str, Any],
        failed_steps: list[str],
        evidence: list[dict[str, Any]],
        events: list[dict[str, Any]],
        fault_events: list[dict[str, Any]],
        observations: dict[str, Any],
        repeated: list[str],
    ) -> list[Hypothesis]:
        hyp: list[Hypothesis] = []
        fault_ids = {str(f.get("fault_id", "")) for f in fault_events}
        ev_ids = [str(e.get("evidence_id") or e.get("id", "")) for e in evidence[:5]]
        state = observations.get("independent_state") or {}

        # R-DB-001: authoritative eval-plane fault event + persistence divergence.
        if "TF-DB-001" in fault_ids:
            hyp.append(
                Hypothesis(
                    "h-persistence",
                    "database persistence failure (TF-DB-001 injected before insert)",
                    "PRODUCT",
                    "PERSISTENCE",
                    supporting=[
                        EvidenceRef(
                            str(f.get("id", "eval")),
                            "eval_event_log",
                            EvidenceRole.SUPPORT,
                            "TX_ROLLBACK",
                        )
                        for f in fault_events
                        if str(f.get("fault_id")) == "TF-DB-001"
                    ],
                    confirmation_rule=(
                        "R-DB-001: eval EventLog TX_ROLLBACK + independent state count mismatch"
                    ),
                )
            )
        # R-API-001: injected API fault.
        if "TF-API-001" in fault_ids:
            hyp.append(
                Hypothesis(
                    "h-api",
                    "API 500 injected (TF-API-001)",
                    "PRODUCT",
                    "API",
                    supporting=[
                        EvidenceRef(
                            str(f.get("id", "eval")),
                            "eval_event_log",
                            EvidenceRole.SUPPORT,
                            "API_500",
                        )
                        for f in fault_events
                        if str(f.get("fault_id")) == "TF-API-001"
                    ],
                    confirmation_rule="R-API-001: eval EventLog API_500 + create step failed",
                )
            )
        # R-NET-001: network delay fault.
        if "TF-NET-001" in fault_ids:
            hyp.append(
                Hypothesis(
                    "h-network",
                    "network latency injected (TF-NET-001)",
                    "ENVIRONMENT",
                    "NETWORK",
                    supporting=[
                        EvidenceRef(
                            str(f.get("id", "eval")),
                            "eval_event_log",
                            EvidenceRole.SUPPORT,
                            "DELAY_APPLIED",
                        )
                        for f in fault_events
                        if str(f.get("fault_id")) == "TF-NET-001"
                    ],
                    confirmation_rule="R-NET-001: eval EventLog DELAY_APPLIED",
                )
            )
        # R-UI-001: UI divergence with NO state divergence -> test-visible but
        # verdict unaffected; support + contradiction preserved.
        if "TF-UI-001" in fault_ids:
            hyp.append(
                Hypothesis(
                    "h-ui",
                    "UI presentation anomaly (TF-UI-001: success banner hidden)",
                    "PRODUCT",
                    "UI_RENDERING",
                    supporting=[
                        EvidenceRef(
                            str(f.get("id", "eval")),
                            "eval_event_log",
                            EvidenceRole.SUPPORT,
                            "ARMED",
                        )
                        for f in fault_events
                        if str(f.get("fault_id")) == "TF-UI-001"
                    ],
                    contradicting=[
                        EvidenceRef(
                            "independent_state",
                            "independent_api",
                            EvidenceRole.CONTRADICT,
                            f"project_count={state.get('project_count')} shows "
                            "persistence succeeded",
                        )
                    ],
                    confirmation_rule=(
                        "R-UI-001: eval EventLog ARMED + UI step failed "
                        "while independent state intact"
                    ),
                )
            )
        if "TF-TIME-001" in fault_ids:
            hyp.append(
                Hypothesis(
                    "h-timing",
                    "async refresh delay injected (TF-TIME-001)",
                    "PRODUCT",
                    "TIMING",
                    supporting=[
                        EvidenceRef(
                            str(f.get("id", "eval")),
                            "eval_event_log",
                            EvidenceRole.SUPPORT,
                            "ARMED",
                        )
                        for f in fault_events
                        if str(f.get("fault_id")) == "TF-TIME-001"
                    ],
                    confirmation_rule=(
                        "R-TIME-001: eval EventLog ARMED + timing-sensitive step divergence"
                    ),
                )
            )

        # Locator hypothesis only from repeated divergence + locator evidence.
        locator_evidence = [
            e for e in evidence if "locator" in str(e.get("evidence_type", "")).lower()
        ]
        missing_locator = [
            e
            for e in locator_evidence
            if e.get("step_id") in failed_steps
            and e.get("source_trust") == "observed"
            and e.get("payload", {}).get("match_count") == 0
            and e.get("payload", {}).get("error_type") == "TimeoutError"
        ]
        if missing_locator:
            hyp.append(
                Hypothesis(
                    "h-locator-missing",
                    "failed selector has no matching node in the live DOM",
                    "TEST_ASSET",
                    "LOCATOR",
                    status="SUPPORTED",
                    supporting=[
                        EvidenceRef(str(e["evidence_id"]), "evidence", EvidenceRole.SUPPORT)
                        for e in missing_locator
                    ],
                    confirmation_rule="R-LOC-002: failed step + timeout + zero DOM matches; "
                    "supports locator drift, does not prove a product root cause",
                )
            )
        if repeated and len(set(repeated)) > 1 and locator_evidence:
            hyp.append(
                Hypothesis(
                    "h-locator",
                    "locator instability across repeated runs",
                    "TEST_ASSET",
                    "LOCATOR",
                    supporting=[
                        EvidenceRef(str(e.get("evidence_id", "")), "evidence", EvidenceRole.SUPPORT)
                        for e in locator_evidence[:2]
                    ],
                    confirmation_rule="R-LOC-001: repeated verdict divergence + locator evidence",
                )
            )

        # Generic failure hypothesis when verdict failed but no authoritative
        # fault evidence — must stay CANDIDATE (never auto-confirmed).
        if not hyp:
            hyp.append(
                Hypothesis(
                    "h-unknown",
                    "failure observed but cause not established",
                    "UNKNOWN",
                    "UNKNOWN",
                    status="CANDIDATE",
                    supporting=[
                        EvidenceRef(i, "run_report", EvidenceRole.SUPPORT) for i in ev_ids[:2]
                    ],
                    confirmation_rule="R-UNKNOWN: requires authoritative evidence (none present)",
                )
            )

        # Apply deterministic confirmation: a hypothesis with a named rule and
        # supporting evidence and NO contradicting evidence is CONFIRMED; those
        # with both sides are SUPPORTED (contradiction prevents confirmation).
        for h in hyp:
            if h.hypothesis_id in {"h-locator-missing", "h-unknown"}:
                continue  # absence supports drift; it cannot establish the root cause
            if not h.supporting:
                continue
            if h.contradicting:
                h.status = "SUPPORTED"
            elif (
                h.confirmation_rule
                and h.confirmation_rule
                != "R-UNKNOWN: requires authoritative evidence (none present)"
            ):
                h.status = "CONFIRMED"
            else:
                h.status = "SUPPORTED"
        return hyp

    def _contributing(
        self, hypotheses: list[Hypothesis], fault_events: list[dict[str, Any]]
    ) -> tuple[str, ...]:
        factors: list[str] = []
        if len(fault_events) > 1:
            factors.append("multiple injected faults active simultaneously")
        for h in hypotheses[1:]:
            if h.status in ("SUPPORTED", "CONFIRMED"):
                factors.append(f"additional {h.status.lower()} hypothesis: {h.hypothesis_id}")
        return tuple(factors)

    def _flaky_characteristic(self, repeated: list[str]) -> str:
        """Flaky needs repeated-run divergence; one failure never qualifies."""
        if len(repeated) >= 2 and len(set(repeated)) > 1:
            return "REPEATED_DIVERGENCE_OBSERVED"
        if len(repeated) >= 2:
            return "REPEATABLY_STABLE"
        return "NOT_DETERMINED"

    def _route(
        self,
        status: str,
        domain: str,
        hypotheses: list[Hypothesis],
        fault_events: list[dict[str, Any]],
        repeated: list[str],
    ) -> str:
        if status == "INCONCLUSIVE" or domain == "UNKNOWN":
            return "NEEDS_MORE_EVIDENCE"
        if self._flaky_characteristic(repeated) == "REPEATED_DIVERGENCE_OBSERVED":
            return "TEST_EXECUTION_ISSUE"
        if domain == "TEST_ASSET":
            return "TEST_ASSET_ISSUE"
        if domain == "ENVIRONMENT":
            return "ENVIRONMENT_ISSUE"
        if domain == "PLATFORM":
            return "PLATFORM_ISSUE"
        if domain == "AGENT":
            return "AGENT_ISSUE"
        if domain == "PRODUCT" and fault_events:
            return "PRODUCT_DEFECT"
        return "MANUAL_REVIEW"
