"""Immutable decision records included only in Trace Hash v3."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    proposal: dict[str, Any]
    policy_decision: dict[str, Any]
    actual_action: dict[str, Any]
    result: dict[str, Any]
    observation_ref: str
    decision_source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_decision_record(
    *,
    proposal: dict[str, Any],
    policy_decision: dict[str, Any],
    actual_action: dict[str, Any],
    result: dict[str, Any],
    observation_ref: str,
    decision_source: str,
) -> DecisionRecord:
    body: dict[str, Any] = {
        "proposal": proposal,
        "policy_decision": policy_decision,
        "actual_action": actual_action,
        "result": result,
        "observation_ref": observation_ref,
        "decision_source": decision_source,
    }
    decision_id = (
        "dec_"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    return DecisionRecord(
        decision_id=decision_id,
        proposal=proposal,
        policy_decision=policy_decision,
        actual_action=actual_action,
        result=result,
        observation_ref=observation_ref,
        decision_source=decision_source,
    )
