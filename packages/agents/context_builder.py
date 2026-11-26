"""Deterministic, bounded context construction for controlled planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DecisionContext:
    context_id: str
    test_intent: dict[str, Any]
    observation: dict[str, Any]
    scenario: dict[str, Any]
    history: tuple[dict[str, Any], ...]
    budget: dict[str, Any]
    goal_status: str
    allowed_actions: tuple[str, ...]
    denied_actions: tuple[str, ...]
    is_real_model: bool = False


class ContextBuilder:
    """Build a reproducible context while reserving an untruncated safety block."""

    def __init__(self, *, max_text_chars: int = 3000, max_history: int = 30) -> None:
        if not 100 <= max_text_chars <= 3000 or not 1 <= max_history <= 100:
            raise ValueError("context bounds are outside the supported range")
        self.max_text_chars = max_text_chars
        self.max_history = max_history

    def build(
        self,
        *,
        test_intent: dict[str, Any],
        observation: dict[str, Any],
        scenario: dict[str, Any],
        history: list[dict[str, Any]],
        budget: dict[str, Any],
        goal_status: str,
    ) -> DecisionContext:
        safe_observation = dict(observation)
        safe_observation["text_excerpt"] = str(observation.get("text_excerpt") or "")[
            : self.max_text_chars
        ]
        safe_history = tuple(dict(row) for row in history[-self.max_history :])
        allowed = tuple(str(action) for action in scenario.get("allowed_actions", ()))
        denied = tuple(str(action) for action in scenario.get("denied_actions", ()))
        # Keep policy/security constraints outside the truncatable text block.
        safe_scenario = {
            "scenario_id": scenario.get("scenario_id"),
            "version": scenario.get("version"),
            "constraints": list(scenario.get("constraints", ())),
            "allowed_actions": list(allowed),
            "denied_actions": list(denied),
        }
        payload = {
            "test_intent": test_intent,
            "observation": safe_observation,
            "scenario": safe_scenario,
            "history": safe_history,
            "budget": budget,
            "goal_status": goal_status,
            "allowed_actions": allowed,
            "denied_actions": denied,
        }
        context_id = (
            "ctx_"
            + hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()[:24]
        )
        return DecisionContext(
            context_id=context_id,
            test_intent=dict(test_intent),
            observation=safe_observation,
            scenario=safe_scenario,
            history=safe_history,
            budget=dict(budget),
            goal_status=goal_status,
            allowed_actions=allowed,
            denied_actions=denied,
        )
