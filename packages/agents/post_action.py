"""Structured post-action checks and stop-policy semantics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AgentStopReason(StrEnum):
    GOAL_VERIFIED = "GOAL_VERIFIED"
    PLAN_EXHAUSTED = "PLAN_EXHAUSTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    POLICY_STOP = "POLICY_STOP"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class PostActionResult:
    actual_result: str
    expected_match: bool | None
    page_changed: bool | None
    exception: str
    needs_replan: bool
    stop_reason: AgentStopReason


def evaluate_post_action(
    *,
    actual_result: dict[str, Any],
    expected_outcome: str,
    before_observation: dict[str, Any],
    after_observation: dict[str, Any],
    goal_status: str = "NOT_VERIFIED",
    execution_error: str = "",
    policy_allowed: bool = True,
    budget_remaining: int = 1,
    plan_remaining: int = 1,
) -> PostActionResult:
    before_ref = str(before_observation.get("observation_ref") or "")
    after_ref = str(after_observation.get("observation_ref") or "")
    page_changed = (before_ref != after_ref) if before_ref and after_ref else None
    detail = str(actual_result.get("detail") or actual_result.get("status") or "")
    expected_match = (expected_outcome in detail) if expected_outcome else None
    if not policy_allowed:
        stop = AgentStopReason.POLICY_STOP
    elif execution_error:
        stop = AgentStopReason.EXECUTION_FAILED
    elif goal_status == "VERIFIED":
        stop = AgentStopReason.GOAL_VERIFIED
    elif budget_remaining <= 0:
        stop = AgentStopReason.BUDGET_EXHAUSTED
    elif plan_remaining <= 0:
        stop = AgentStopReason.PLAN_EXHAUSTED
    elif goal_status in {"INCONCLUSIVE", "NOT_EVALUATED"}:
        stop = AgentStopReason.INCONCLUSIVE
    else:
        stop = AgentStopReason.PLAN_EXHAUSTED
    return PostActionResult(
        actual_result=detail,
        expected_match=expected_match,
        page_changed=page_changed,
        exception=execution_error,
        needs_replan=stop in {AgentStopReason.PLAN_EXHAUSTED, AgentStopReason.INCONCLUSIVE},
        stop_reason=stop,
    )
