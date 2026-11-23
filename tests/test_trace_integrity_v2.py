"""R5-4.6 trace hash v2: tamper evidence over the fields that decide compile.

v1 (legacy) scope is preserved for historical traces; v2 adds the TestIntent,
every agent decision and the version fields, so editing any of them after the
fact breaks verification. Derived state (goal evaluation, status, counters) is
deliberately outside the hash — hashing a derivation of hashed inputs would be
self-reference.
"""

from __future__ import annotations

import copy
import json

from packages.agents.exploration import (
    HASH_VERSION,
    ExplorationTrace,
    compute_trace_hash,
    verify_trace_record,
)

INTENT = {
    "intent_id": "i-integ",
    "description": "login",
    "goal": "login",
    "target": "/projects",
    "expected_outcomes": [],
    "required_assertions": [],
    "constraints": [],
}


def _record() -> dict:
    trace = ExplorationTrace(
        exploration_id="exp_tamper",
        test_intent_ref="i-integ",
        application_version="app-1",
        browser_version="chrome-1",
        agent_version="agent-1",
        prompt_version="prompt-1",
        tool_version="tool-1",
        test_intent=dict(INTENT),
        action_sequence=[{"tool": "navigate", "args": {"url": "http://127.0.0.1:8001/"}}],
        observations=[
            {
                "url": "http://127.0.0.1:8001/projects",
                "url_path": "/projects",
                "text_excerpt": "projects",
                "step_index": 1,
            }
        ],
        agent_decisions=[{"step_index": 0, "action": "navigate", "result": "OK"}],
        security_events=[],
        stop_reason="GOAL_VERIFIED",
        goal_status="VERIFIED",
    )
    trace.finalize()
    return json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))


def test_v2_record_verifies_after_json_round_trip() -> None:
    record = _record()
    assert record["hash_version"] == HASH_VERSION == 2
    assert verify_trace_record(record) is True


def test_editing_test_intent_is_detected() -> None:
    tampered = _record()
    tampered["test_intent"]["target"] = "/settings"
    assert verify_trace_record(tampered) is False


def test_editing_agent_decision_is_detected() -> None:
    tampered = _record()
    tampered["agent_decisions"][0]["result"] = "FORGED"
    assert verify_trace_record(tampered) is False


def test_editing_versions_is_detected() -> None:
    for field in (
        "agent_version",
        "prompt_version",
        "tool_version",
        "application_version",
        "browser_version",
    ):
        tampered = _record()
        tampered[field] = f"evil-{field}"
        assert verify_trace_record(tampered) is False, field


def test_editing_actions_or_observations_is_detected() -> None:
    tampered = _record()
    tampered["action_sequence"][0]["args"]["url"] = "http://127.0.0.1:9/evil"
    assert verify_trace_record(tampered) is False
    tampered2 = _record()
    tampered2["observations"][0]["url_path"] = "/admin"
    assert verify_trace_record(tampered2) is False


def test_old_hash_cannot_be_reused_for_new_content() -> None:
    """Replaying a valid hash onto different content must fail everywhere."""
    record = _record()
    valid_hash = record["source_trace_hash"]
    forged = _record()
    forged["stop_reason"] = "PLAN_EXHAUSTED"
    forged["source_trace_hash"] = valid_hash  # stale hash, new content
    assert verify_trace_record(forged) is False


def test_derived_fields_are_outside_the_hash() -> None:
    record = _record()
    for field, value in (
        ("status", "FAILED"),
        ("goal_status", "NOT_VERIFIED"),
        ("goal_reason", "changed"),
        ("step_count", 99),
        ("trace_integrity_verified", False),
    ):
        derived = copy.deepcopy(record)
        derived[field] = value
        assert verify_trace_record(derived) is True, field


def test_legacy_records_still_verify_with_v1_scope() -> None:
    """Historical traces (no hash_version) keep validating — no mass tamper."""
    legacy = {
        "exploration_id": "exp_legacy",
        "action_sequence": [{"tool": "navigate", "args": {"url": "u"}}],
        "observations": [{"url_path": "/"}],
        "security_events": [],
        "stop_reason": "GOAL_REACHED",
        "source_trace_hash": compute_trace_hash(
            exploration_id="exp_legacy",
            action_sequence=[{"tool": "navigate", "args": {"url": "u"}}],
            observations=[{"url_path": "/"}],
            security_events=[],
            stop_reason="GOAL_REACHED",
            hash_version=1,
        ),
    }
    assert verify_trace_record(legacy) is True
    legacy["stop_reason"] = "FORGED"
    assert verify_trace_record(legacy) is False
