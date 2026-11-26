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
        trace.scenario_id = "login"
        trace.scenario_version = "2.0.0"
        trace.scenario_digest = "a" * 64
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision_id", "dec_tampered"),
        ("proposed_action", {"action_type": "navigate", "arguments": {"url": "/evil"}}),
        ("policy_decision", {"allowed": False}),
        ("decision_source", "forged"),
    ],
)
def test_trace_hash_v3_detects_each_decision_field(field: str, value: object) -> None:
    trace = _trace(3)
    trace.scenario_id, trace.scenario_version, trace.scenario_digest = "login", "2.0.0", "a" * 64
    decision = make_decision_record(
        proposal={"action_type": "snapshot"},
        policy_decision={"allowed": True},
        actual_action={"tool": "snapshot"},
        result={"status": "ok"},
        observation_ref="obs-1",
        decision_source="mock",
    ).to_dict()
    decision["proposed_action"] = dict(decision["proposal"])
    trace.decision_records.append(decision)
    trace.finalize()
    record = trace.to_dict()
    record["decision_records"][0][field] = value
    assert not verify_trace_record(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [("scenario_id", "other"), ("scenario_version", "9.9.9"), ("scenario_digest", "b" * 64)],
)
def test_trace_hash_v3_detects_scenario_identity_tampering(field: str, value: str) -> None:
    trace = _trace(3)
    trace.scenario_id, trace.scenario_version, trace.scenario_digest = "login", "2.0.0", "a" * 64
    trace.finalize()
    record = trace.to_dict()
    record[field] = value
    assert not verify_trace_record(record)
