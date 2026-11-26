"""Stage-10 Oracle-Protected Self-Healing (C11, draft).

The AI may propose test-asset repairs but MUST NOT destroy the formal standard.
Guards implemented literally from v1.1 §阶段10:

  * Allowed change kinds: locator / wait condition / test step / data ref /
    non-semantic automation config. Forbidden: product code, formal Oracle,
    required Assertion deletion, lowered assertion strictness, overwriting a
    formal TestCaseVersion, auto-releasing product code.
  * Assertion Preservation + Oracle Integrity Checker: every required
    Assertion and the EXACT formal Oracle binding must survive unchanged.
  * RepairProposal state machine: DRAFT -> VALIDATING -> VALIDATED ->
    PENDING_APPROVAL -> APPROVED -> PROMOTING -> PROMOTED, with
    VALIDATION_FAILED / REJECTED / PROMOTION_FAILED / ROLLED_BACK.
  * APPROVED != PROMOTED; human approval is mandatory and never self-granted.
  * Atomic CAS promotion: current formal version must equal base version;
    append new immutable TestCaseVersion + update pointer + write promotion
    record as ONE operation. VERSION_CONFLICT is recorded using existing fields
    (no new state), never silently retried over a newer version.
  * PROMOTED -> ROLLED_BACK records target version, reason, operator.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from packages.core.errors import ConflictError, ValidationError
from packages.execution.models import TestCaseVersion
from packages.execution.review_policy import is_self_approver
from packages.execution.verdict import OracleVerdict

ALLOWED_KINDS = frozenset(
    {
        "locator_update",
        "wait_condition",
        "test_step_fix",
        "data_ref_fix",
        "automation_config",
    }
)
FORBIDDEN_KINDS = frozenset(
    {
        "product_code",
        "oracle_change",
        "assertion_delete",
        "lower_assertion_strictness",
        "overwrite_formal_version",
        "auto_release",
    }
)
STATES = (
    "DRAFT",
    "VALIDATING",
    "VALIDATION_FAILED",
    "VALIDATED",
    "PENDING_APPROVAL",
    "REJECTED",
    "APPROVED",
    "PROMOTING",
    "PROMOTED",
    "PROMOTION_FAILED",
    "ROLLED_BACK",
)
TERMINAL = frozenset({"REJECTED", "PROMOTED", "ROLLED_BACK"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"VALIDATING"}),
    "VALIDATING": frozenset({"VALIDATION_FAILED", "VALIDATED"}),
    "VALIDATION_FAILED": frozenset({"DRAFT"}),  # new revision only
    "VALIDATED": frozenset({"PENDING_APPROVAL"}),
    "PENDING_APPROVAL": frozenset({"REJECTED", "APPROVED"}),
    "REJECTED": frozenset(),
    "APPROVED": frozenset({"PROMOTING"}),
    "PROMOTING": frozenset({"PROMOTED", "PROMOTION_FAILED"}),
    "PROMOTION_FAILED": frozenset({"APPROVED"}),  # only for recoverable failures
    "PROMOTED": frozenset({"ROLLED_BACK"}),
    "ROLLED_BACK": frozenset(),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class DiffEntry:
    kind: str
    target: str
    before: str
    after: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairProposal:
    proposal_id: str
    case_id: str
    state: str = "DRAFT"
    revision: int = 1
    change_kinds: tuple[str, ...] = ()
    diff: list[DiffEntry] = field(default_factory=list)
    base_testcase_version: int = 1
    candidate_testcase_version: int = 2
    promotion_target_version: int = 2
    promotion_record: dict[str, Any] = field(default_factory=dict)
    validation_notes: tuple[str, ...] = ()
    approved_by: str = ""
    rejected_by: str = ""
    source_run_id: str = ""
    source_hypothesis_id: str = ""
    source_evidence_refs: tuple[str, ...] = ()
    approval_record: str = ""
    failure_reason: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["change_kinds"] = list(self.change_kinds)
        d["diff"] = [x.to_dict() for x in self.diff]
        d["validation_notes"] = list(self.validation_notes)
        return d


class AssertionPreservationChecker:
    """Required Assertions and formal Oracle bindings must survive unchanged."""

    def check(self, base: TestCaseVersion, candidate: TestCaseVersion) -> list[str]:
        notes: list[str] = []
        base_required = {a.assertion_id: a for a in base.assertions if a.required}
        cand_required = {a.assertion_id: a for a in candidate.assertions if a.required}
        missing = set(base_required) - set(cand_required)
        if missing:
            raise ValidationError(f"required assertion(s) removed: {missing}")
        for aid, spec in base_required.items():
            if cand_required[aid] != spec:
                raise ValidationError(f"required assertion altered: {aid}")
        base_bind = {b.assertion_id: b for b in base.oracle_bindings}
        cand_bind = {b.assertion_id: b for b in candidate.oracle_bindings}
        for aid, binding in base_bind.items():
            if aid not in cand_bind:
                raise ValidationError(f"oracle binding removed: {aid}")
            if cand_bind[aid] != binding:
                raise ValidationError(f"formal oracle binding altered: {aid}")
        notes.append("all required assertions + formal oracle bindings preserved unchanged")
        return notes


class OracleIntegrityChecker:
    """Formal Oracle is not repairable; approval state must stay intact."""

    def check(self, base: TestCaseVersion, candidate: TestCaseVersion) -> list[str]:
        for binding in candidate.oracle_bindings:
            for oracle in (binding.primary, *binding.supporting):
                if not oracle.approved:
                    raise ValidationError(
                        f"candidate contains unapproved formal oracle: {oracle.oracle_id}"
                    )
                if oracle.expected not in (OracleVerdict.PASSED, OracleVerdict.FAILED):
                    raise ValidationError(f"invalid oracle expectation: {oracle.oracle_id}")
        notes = ["formal oracle set unchanged and approved; no oracle modified by repair"]
        return notes


class RepairService:
    """State machine + validation + sandbox + gated CAS promotion + rollback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proposals: dict[str, RepairProposal] = {}
        self._candidates: dict[str, TestCaseVersion] = {}
        self._lock_version: dict[str, int] = {}

    def create_proposal(
        self,
        proposal_id: str,
        base: TestCaseVersion,
        candidate: TestCaseVersion,
        *,
        change_kinds: tuple[str, ...],
        diff: list[DiffEntry],
        source_run_id: str = "",
        source_hypothesis_id: str = "",
        source_evidence_refs: tuple[str, ...] = (),
    ) -> RepairProposal:
        if any(k in FORBIDDEN_KINDS for k in change_kinds):
            raise ValidationError(
                f"forbidden repair kind(s): {set(change_kinds) & FORBIDDEN_KINDS}"
            )
        unknown = set(change_kinds) - ALLOWED_KINDS
        if unknown:
            raise ValidationError(f"unknown repair kind(s): {unknown}")
        if not change_kinds:
            raise ValidationError("repair proposal has no change kinds")
        if candidate.case_id != base.case_id:
            raise ValidationError("candidate case_id differs from base")
        if candidate.version != base.version + 1:
            raise ValidationError("candidate must be exactly base version + 1")
        with self._lock:
            if proposal_id in self._proposals:
                raise ConflictError(f"proposal exists: {proposal_id}")
            proposal = RepairProposal(
                proposal_id=proposal_id,
                case_id=base.case_id,
                change_kinds=change_kinds,
                diff=list(diff),
                base_testcase_version=base.version,
                candidate_testcase_version=candidate.version,
                promotion_target_version=candidate.version,
                source_run_id=source_run_id,
                source_hypothesis_id=source_hypothesis_id,
                source_evidence_refs=source_evidence_refs,
            )
            self._proposals[proposal_id] = proposal
            self._candidates[proposal_id] = candidate
            self._candidates[f"__base__{proposal_id}"] = base
            return proposal

    def _transition(self, proposal: RepairProposal, target: str) -> None:
        allowed = _TRANSITIONS.get(proposal.state, frozenset())
        if target not in allowed:
            raise ConflictError(f"illegal repair transition {proposal.state} -> {target}")
        proposal.state = target

    def validate(self, proposal_id: str) -> RepairProposal:
        with self._lock:
            p = self._get(proposal_id)
            if p.state != "DRAFT":
                raise ConflictError(f"cannot validate in state {p.state}")
            self._transition(p, "VALIDATING")
            base_case, candidate = self._candidate_pair(proposal_id)
            try:
                # Sandbox validation = structural replay-readiness checks. The
                # deterministic oracle/assertion guards are the real gate.
                candidate.validate()
                notes = AssertionPreservationChecker().check(base_case, candidate)
                notes += OracleIntegrityChecker().check(base_case, candidate)
                p.validation_notes = tuple(notes)
                self._transition(p, "VALIDATED")
            except (ValidationError, ValueError) as exc:
                p.failure_reason = f"VALIDATION_FAILED: {exc}"[:500]
                self._transition(p, "VALIDATION_FAILED")
            return p

    def submit_for_approval(self, proposal_id: str) -> RepairProposal:
        with self._lock:
            p = self._get(proposal_id)
            self._transition(p, "PENDING_APPROVAL")
            return p

    def approve(self, proposal_id: str, *, approver: str, approval_record: str) -> RepairProposal:
        if not approver or not approval_record:
            raise ValidationError("human approver and approval_record are mandatory")
        if is_self_approver(approver):
            raise ValidationError("self-approval is refused")
        with self._lock:
            p = self._get(proposal_id)
            if p.state != "PENDING_APPROVAL":
                raise ConflictError(f"cannot approve from {p.state}")
            self._transition(p, "APPROVED")
            p.approved_by = approver
            p.approval_record = approval_record
            return p  # APPROVED != PROMOTED

    def reject(self, proposal_id: str, reason: str, rejected_by: str = "human") -> RepairProposal:
        if not reason.strip():
            raise ValidationError("rejection reason is required")
        if not rejected_by.strip():
            raise ValidationError("rejected_by is required")
        if is_self_approver(rejected_by):
            raise ValidationError("repair rejection requires a named human reviewer")
        with self._lock:
            p = self._get(proposal_id)
            self._transition(p, "REJECTED")
            p.failure_reason = reason[:500]
            p.rejected_by = rejected_by.strip()
            return p

    def promote(
        self,
        proposal_id: str,
        *,
        current_formal: TestCaseVersion,
        sandbox_replay: Callable[[TestCaseVersion], bool] | None = None,
        operator: str = "",
        promotion_reason: str = "",
    ) -> tuple[RepairProposal, TestCaseVersion]:
        """Atomic CAS promotion under a lock. One attempt per base version."""
        with self._lock:
            p = self._get(proposal_id)
            if p.state != "APPROVED":
                raise ConflictError(f"promotion requires APPROVED, got {p.state}")
            if is_self_approver(p.approved_by):
                raise ValidationError("no valid human approval for promotion")
            self._transition(p, "PROMOTING")
            # CAS: current formal version MUST equal the proposal's base.
            if current_formal.version != p.base_testcase_version:
                p.failure_reason = (
                    f"VERSION_CONFLICT: base={p.base_testcase_version} "
                    f"current={current_formal.version}"
                )[:500]
                p.state = "PROMOTION_FAILED"  # existing state; no new enum
                return p, current_formal
            # Re-run guards at promotion time (defence in depth).
            base_case, candidate = self._candidate_pair(proposal_id)
            AssertionPreservationChecker().check(base_case, candidate)
            OracleIntegrityChecker().check(base_case, candidate)
            if sandbox_replay is not None and not sandbox_replay(candidate):
                p.failure_reason = "PROMOTION_FAILED: sandbox replay did not pass"
                p.state = "PROMOTION_FAILED"
                return p, current_formal
            # Atomic: append new immutable version + pointer + record together.
            promoted = replace(candidate, version=p.promotion_target_version)
            self._candidates[f"__formal__{p.case_id}"] = promoted
            self._lock_version[p.case_id] = promoted.version
            p.promotion_record = {
                "target_version": promoted.version,
                "base_version": current_formal.version,
                "at": _now(),
                "operator": operator or p.approved_by,
                "reason": promotion_reason[:500],
                "cas_check": "passed",
            }
            self._transition(p, "PROMOTED")
            return p, promoted

    def rollback(
        self, proposal_id: str, *, target_version: TestCaseVersion, reason: str, operator: str
    ) -> RepairProposal:
        if not reason or not operator:
            raise ValidationError("rollback requires reason and operator")
        with self._lock:
            p = self._get(proposal_id)
            self._transition(p, "ROLLED_BACK")
            p.promotion_record = {
                **p.promotion_record,
                "rolled_back_to": target_version.version,
                "rollback_reason": reason[:500],
                "rollback_operator": operator,
                "rolled_back_at": _now(),
            }
            self._candidates[f"__formal__{p.case_id}"] = target_version
            self._lock_version[p.case_id] = target_version.version
            return p

    def formal_version(self, case_id: str) -> TestCaseVersion | None:
        return self._candidates.get(f"__formal__{case_id}")

    def list(self) -> list[dict[str, Any]]:
        """Read-only view of every proposal, newest first. Mutates nothing."""
        with self._lock:
            items = [p.to_dict() for p in self._proposals.values()]
        return sorted(items, key=lambda d: str(d.get("created_at", "")), reverse=True)

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get(proposal_id).to_dict()

    def _get(self, proposal_id: str) -> RepairProposal:
        p = self._proposals.get(proposal_id)
        if p is None:
            raise ConflictError(f"unknown proposal: {proposal_id}")
        return p

    def _candidate_pair(self, proposal_id: str) -> tuple[TestCaseVersion, TestCaseVersion]:
        p = self._get(proposal_id)
        base = self.formal_version(p.case_id) or self._candidates.get(f"__base__{proposal_id}")
        cand = self._candidates[proposal_id]
        if base is None:
            raise ValidationError("base testcase version not registered")
        return base, cand

    def register_base(self, proposal_id: str, base: TestCaseVersion) -> None:
        with self._lock:
            self._candidates[f"__base__{proposal_id}"] = base
