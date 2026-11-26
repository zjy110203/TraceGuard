from __future__ import annotations

import pytest

from packages.agents.decision_records import make_decision_record
from packages.agents.exploration import ExplorationTrace, verify_trace_record


def _trace(version: int) -> ExplorationTrace:
    return ExplorationTrace(
        exploration_id=f"exp-v{version}",
        test_intent_ref="intent-1",
        application_version="test",
        browser_version="chromium",
        agent_version="mock",
        prompt_version="none",
        tool_version="1",
        hash_version=version,
    )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_trace_hash_versions_remain_readable(version: int) -> None:
    trace = _trace(version)
    if version == 3:
        decision = make_decision_record(
            proposal={"action_type": "snapshot"},
            policy_decision={"allowed": True},
            actual_action={"tool": "snapshot"},
            result={"status": "ok"},
            observation_ref="obs-1",
            decision_source="mock",
        )
        trace.decision_records.append(decision.to_dict())
    trace.finalize()
    record = trace.to_dict()
    assert verify_trace_record(record)
    if version < 3:
        assert "decision_records" not in record


def test_trace_hash_v3_detects_decision_record_tampering() -> None:
    trace = _trace(3)
    decision = make_decision_record(
        proposal={"action_type": "snapshot"},
        policy_decision={"allowed": True},
        actual_action={"tool": "snapshot"},
        result={"status": "ok"},
        observation_ref="obs-1",
        decision_source="mock",
    )
    trace.decision_records.append(decision.to_dict())
    trace.finalize()
    record = trace.to_dict()
    record["decision_records"][0]["policy_decision"]["allowed"] = False
    assert not verify_trace_record(record)
