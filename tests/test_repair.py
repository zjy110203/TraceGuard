"""Stage-10 acceptance: Oracle-Protected Self-Healing.

Verifies:
  * forbidden change kinds rejected; allowed locator/wait/data-ref fixes pass;
  * assertion preservation + oracle integrity (can't delete/alter required
    assertion or formal Oracle; can't lower strictness);
  * full RepairProposal state machine incl. validation failure, rejection;
  * APPROVED != PROMOTED; no agent self-approval;
  * CAS promotion: version conflict blocks and does NOT overwrite newer version;
  * sandbox replay gate; promotion record; rollback records target/reason/operator.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from packages.core.errors import ValidationError
from packages.execution.models import (
    AssertionSpec,
    OracleBinding,
    OracleSpec,
    TestCaseVersion,
    TestIntent,
    TestStep,
)  # noqa: F401
from packages.execution.repair import (
    ALLOWED_KINDS,
    FORBIDDEN_KINDS,
    STATES,
    AssertionPreservationChecker,
    DiffEntry,
    OracleIntegrityChecker,
    RepairService,
)
from packages.execution.review_policy import SELF_APPROVER_IDENTIFIERS

for _m in (TestCaseVersion, TestIntent, TestStep):
    _m.__test__ = False  # type: ignore[attr-defined]


def _base() -> TestCaseVersion:
    return TestCaseVersion(
        case_id="TC-REPAIR",
        version=1,
        title="repair target",
        intent=TestIntent("i", "x"),
        steps=(
            TestStep("s1", "navigate", "[data-testid=old]"),
            TestStep("s2", "click", "[data-testid=old-btn]"),
        ),
        assertions=(AssertionSpec("a1", "still works", required=True),),
        oracle_bindings=(
            OracleBinding(
                "a1",
                primary=OracleSpec("o1", "ui", "ui.s2.passed", expected_value=True),
            ),
        ),
    )


def _locator_fix(base: TestCaseVersion) -> TestCaseVersion:
    steps = tuple(
        __import__("dataclasses").replace(s, target="[data-testid=new]") if s.step_id == "s1" else s
        for s in base.steps
    )
    return __import__("dataclasses").replace(base, version=2, steps=steps)


def test_state_machine_has_11_states() -> None:
    assert len(STATES) == 11
    assert ALLOWED_KINDS.isdisjoint(FORBIDDEN_KINDS)


def test_forbidden_change_kinds_rejected() -> None:
    svc = RepairService()
    base = _base()
    with pytest.raises(ValidationError):
        svc.create_proposal(
            "p-bad",
            base,
            _locator_fix(base),
            change_kinds=("oracle_change",),
            diff=[],
        )
    with pytest.raises(ValidationError):
        svc.create_proposal(
            "p-bad2",
            base,
            _locator_fix(base),
            change_kinds=("locator_update", "product_code"),
            diff=[],
        )


def test_assertion_preservation_and_oracle_integrity() -> None:
    base = _base()
    # deleting required assertion -> rejected
    cand_no_assertion = __import__("dataclasses").replace(
        base, version=2, assertions=(), oracle_bindings=()
    )
    with pytest.raises(ValidationError):
        AssertionPreservationChecker().check(base, cand_no_assertion)
    # changing the formal oracle binding -> rejected
    altered_oracle = OracleSpec("o1", "ui", "ui.s2.passed", expected_value=False)
    cand_altered = __import__("dataclasses").replace(
        base,
        version=2,
        oracle_bindings=(OracleBinding("a1", primary=altered_oracle),),
    )
    with pytest.raises(ValidationError):
        AssertionPreservationChecker().check(base, cand_altered)
    # lower strictness (required -> optional) -> rejected
    cand_optional = __import__("dataclasses").replace(
        base, version=2, assertions=(AssertionSpec("a1", "still works", required=False),)
    )
    with pytest.raises(ValidationError):
        AssertionPreservationChecker().check(base, cand_optional)
    # unapproved oracle -> rejected
    unapproved = OracleSpec("o1", "ui", "ui.s2.passed", expected_value=True, approved=False)
    cand_unapproved = __import__("dataclasses").replace(
        base, version=2, oracle_bindings=(OracleBinding("a1", primary=unapproved),)
    )
    with pytest.raises(ValidationError):
        OracleIntegrityChecker().check(base, cand_unapproved)


def _valid_proposal(svc: RepairService, pid: str = "p1") -> tuple[TestCaseVersion, object]:
    base = _base()
    cand = _locator_fix(base)
    p = svc.create_proposal(
        pid,
        base,
        cand,
        change_kinds=("locator_update",),
        diff=[DiffEntry("locator_update", "s1", "[data-testid=old]", "[data-testid=new]")],
    )
    return base, p


def test_full_lifecycle_to_promoted() -> None:
    svc = RepairService()
    base, p = _valid_proposal(svc)
    assert p.state == "DRAFT"
    p = svc.validate(p.proposal_id)
    assert p.state == "VALIDATED"
    p = svc.submit_for_approval(p.proposal_id)
    assert p.state == "PENDING_APPROVAL"
    p = svc.approve(p.proposal_id, approver="human-reviewer", approval_record="review-42")
    assert p.state == "APPROVED"  # APPROVED != PROMOTED
    promoted, version = svc.promote(
        p.proposal_id, current_formal=base, sandbox_replay=lambda _c: True
    )
    assert promoted.state == "PROMOTED"
    assert version.version == 2
    assert promoted.promotion_record["cas_check"] == "passed"
    assert svc.formal_version("TC-REPAIR").version == 2


def test_repair_store_restores_proposal_and_requires_matching_replay(tmp_path) -> None:
    store_path = tmp_path / "repairs.jsonl"
    svc = RepairService(store_path)
    base, proposal = _valid_proposal(svc, "p-persist")
    svc.validate(proposal.proposal_id)
    svc.submit_for_approval(proposal.proposal_id)
    svc.approve(proposal.proposal_id, approver="reviewer", approval_record="approval-1")

    restored = RepairService(store_path)
    assert restored.get(proposal.proposal_id)["state"] == "APPROVED"
    restored_candidate = restored._candidate_pair(proposal.proposal_id)[1]
    replay_evidence = {"proposal": proposal.proposal_id, "result": "validated"}
    replay_evidence_hash = hashlib.sha256(
        json.dumps(replay_evidence, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    restored.save_replay(
        proposal.proposal_id,
        {
            "proposal_id": proposal.proposal_id,
            "candidate_digest": restored.case_digest(restored_candidate),
            "base_version": 1,
            "evidence": replay_evidence,
            "evidence_sha256": replay_evidence_hash,
            "evidence_hash": replay_evidence_hash,
            "status": "PASSED",
        },
    )
    restored = RepairService(store_path)
    replay = restored.get_replay(proposal.proposal_id)
    assert replay and replay["evidence_hash"] == replay_evidence_hash
    promoted, version = restored.promote(
        proposal.proposal_id,
        current_formal=base,
        sandbox_replay=lambda _candidate: True,
    )
    assert promoted.state == "PROMOTED" and version.version == 2


def test_persist_formal_failure_does_not_mark_repair_promoted() -> None:
    svc = RepairService()
    base, proposal = _valid_proposal(svc, "p-write-failure")
    svc.validate(proposal.proposal_id)
    svc.submit_for_approval(proposal.proposal_id)
    svc.approve(proposal.proposal_id, approver="reviewer", approval_record="approval-2")

    def fail_write(_candidate):
        raise OSError("simulated disk failure")

    with pytest.raises(OSError, match="simulated disk failure"):
        svc.promote(
            proposal.proposal_id,
            current_formal=base,
            sandbox_replay=lambda _candidate: True,
            persist_formal=fail_write,
        )
    assert svc.get(proposal.proposal_id)["state"] == "APPROVED"
    assert svc.formal_version(base.case_id) is None


def test_agent_cannot_self_approve() -> None:
    svc = RepairService()
    _base_, p = _valid_proposal(svc, "p-self")
    svc.validate(p.proposal_id)
    svc.submit_for_approval(p.proposal_id)
    with pytest.raises(ValidationError):
        svc.approve(p.proposal_id, approver="agent", approval_record="auto")


@pytest.mark.parametrize("actor", sorted(SELF_APPROVER_IDENTIFIERS))
def test_repair_reject_requires_human_actor(actor: str) -> None:
    svc = RepairService()
    _base_case, proposal = _valid_proposal(svc, f"p-reject-{actor or 'blank'}")
    svc.validate(proposal.proposal_id)
    svc.submit_for_approval(proposal.proposal_id)
    if actor:
        with pytest.raises(ValidationError):
            svc.reject(proposal.proposal_id, "not acceptable", rejected_by=actor)
    else:
        with pytest.raises(ValidationError):
            svc.reject(proposal.proposal_id, "not acceptable", rejected_by="")


def test_repair_rejection_persists_operator() -> None:
    svc = RepairService()
    _base_case, proposal = _valid_proposal(svc, "p-rejected-by")
    svc.validate(proposal.proposal_id)
    svc.submit_for_approval(proposal.proposal_id)
    rejected = svc.reject(proposal.proposal_id, "evidence insufficient", rejected_by="qa-reviewer")
    assert rejected.rejected_by == "qa-reviewer"
    assert svc.get(proposal.proposal_id)["rejected_by"] == "qa-reviewer"


def test_validation_failure_tracked() -> None:
    svc = RepairService()
    base = _base()
    # candidate with changed required assertion -> validation fails
    bad = __import__("dataclasses").replace(
        base,
        version=2,
        assertions=(AssertionSpec("a1", "changed semantics", required=True),),
    )
    p = svc.create_proposal("p-vfail", base, bad, change_kinds=("test_step_fix",), diff=[])
    p = svc.validate(p.proposal_id)
    assert p.state == "VALIDATION_FAILED"
    assert "VALIDATION_FAILED" in p.failure_reason


def test_version_conflict_blocks_and_preserves_newer() -> None:
    svc = RepairService()
    base, p = _valid_proposal(svc, "p-conflict")
    svc.validate(p.proposal_id)
    svc.submit_for_approval(p.proposal_id)
    svc.approve(p.proposal_id, approver="human", approval_record="r1")
    # current formal has advanced to v3 by someone else
    newer = __import__("dataclasses").replace(base, version=3)
    result, returned = svc.promote(p.proposal_id, current_formal=newer)
    assert result.state == "PROMOTION_FAILED"
    assert "VERSION_CONFLICT" in result.failure_reason
    assert returned.version == 3  # newer version NOT overwritten
    assert svc.formal_version("TC-REPAIR") is None  # no false promotion recorded


def test_sandbox_replay_gate() -> None:
    svc = RepairService()
    base, p = _valid_proposal(svc, "p-sandbox")
    svc.validate(p.proposal_id)
    svc.submit_for_approval(p.proposal_id)
    svc.approve(p.proposal_id, approver="human", approval_record="r2")
    result, _ = svc.promote(p.proposal_id, current_formal=base, sandbox_replay=lambda _c: False)
    assert result.state == "PROMOTION_FAILED"
    assert "sandbox" in result.failure_reason.lower()


def test_rollback_records_target_reason_operator() -> None:
    svc = RepairService()
    base, p = _valid_proposal(svc, "p-rollback")
    svc.validate(p.proposal_id)
    svc.submit_for_approval(p.proposal_id)
    svc.approve(p.proposal_id, approver="human", approval_record="r3")
    _, promoted = svc.promote(p.proposal_id, current_formal=base, sandbox_replay=lambda _c: True)
    with pytest.raises(ValidationError):
        svc.rollback(p.proposal_id, target_version=base, reason="", operator="human")
    rolled = svc.rollback(p.proposal_id, target_version=base, reason="regression", operator="ops")
    assert rolled.state == "ROLLED_BACK"
    assert rolled.promotion_record["rolled_back_to"] == 1
    assert rolled.promotion_record["rollback_reason"] == "regression"
    assert rolled.promotion_record["rollback_operator"] == "ops"
    assert svc.formal_version("TC-REPAIR").version == 1
