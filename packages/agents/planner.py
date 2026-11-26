"""Action proposal interfaces and deterministic observation-responsive mock."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from packages.agents.context_builder import ContextBuilder, DecisionContext
from packages.agents.exploration import Budget, Observation, ToolCall, ToolGateway
from packages.core.errors import ForbiddenError


@dataclass(frozen=True)
class ActionProposal:
    proposal_id: str
    action_type: str
    arguments: dict[str, Any]
    expected_outcome: str
    decision_reason: str
    observation_ref: str
    decision_source: str


class Planner(Protocol):
    is_real_model: bool

    def propose(self, context: DecisionContext) -> ActionProposal | None: ...


class MockProvider:
    """Deterministic rule provider. It never calls a language model or a tool."""

    is_real_model = False

    def __init__(
        self,
        rules: dict[str, dict[str, Any]] | None = None,
        *,
        steps: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self._rules = {str(key): dict(value) for key, value in (rules or {}).items()}
        self._steps = tuple(dict(step) for step in steps)

    def propose(self, context: DecisionContext) -> ActionProposal | None:
        if self._steps:
            index = len(context.history)
            if index >= len(self._steps):
                return None
            step = self._steps[index]
            observation = context.observation
            visible = set(str(item) for item in observation.get("visible_testids", []))
            selector = str((step.get("args") or {}).get("selector") or "")
            testid = selector.removeprefix("[data-testid=").removesuffix("]")
            if testid and testid != selector and testid not in visible:
                # Observation changed away from the expected control: stop the
                # proposal stream instead of blindly replaying a stale action.
                return None
            step_body: dict[str, Any] = {
                "action_type": str(step["tool"]),
                "arguments": dict(step.get("args") or {}),
                "expected_outcome": str(step.get("expected_outcome") or ""),
                "decision_reason": str(
                    step.get("rationale") or "mock planner matched current observation"
                ),
                "observation_ref": str(observation.get("observation_ref") or ""),
                "decision_source": "mock",
            }
            proposal_id = (
                "prop_"
                + hashlib.sha256(
                    json.dumps(
                        {"context_id": context.context_id, **step_body},
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:24]
            )
            return ActionProposal(proposal_id=proposal_id, **step_body)
        keys = (
            str(context.observation.get("state_summary") or ""),
            str(context.observation.get("url_path") or ""),
        )
        rule = next((self._rules[key] for key in keys if key in self._rules), None)
        if rule is None:
            return None
        body: dict[str, Any] = {
            "action_type": str(rule["action_type"]),
            "arguments": dict(rule.get("arguments") or {}),
            "expected_outcome": str(rule.get("expected_outcome") or ""),
            "decision_reason": str(rule.get("decision_reason") or "mock rule matched observation"),
            "observation_ref": str(context.observation.get("observation_ref") or ""),
            "decision_source": "mock",
        }
        proposal_id = (
            "prop_"
            + hashlib.sha256(
                json.dumps(
                    {"context_id": context.context_id, **body},
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:24]
        )
        return ActionProposal(
            proposal_id=proposal_id,
            action_type=body["action_type"],
            arguments=body["arguments"],
            expected_outcome=body["expected_outcome"],
            decision_reason=body["decision_reason"],
            observation_ref=body["observation_ref"],
            decision_source=body["decision_source"],
        )


class PlannerPolicy:
    """Explorer policy adapter; proposal is validated before it becomes a ToolCall."""

    is_real_model = False
    agent_version = "planner-policy-mock-1.0.0-NOT-REAL-MODEL"

    def __init__(
        self,
        *,
        planner: Planner,
        scenario: dict[str, Any],
        test_intent: dict[str, Any],
        budget: Budget,
        run_id: str,
    ) -> None:
        if not run_id:
            raise ValueError("PlannerPolicy requires a bound run_id")
        self.planner = planner
        self.scenario = dict(scenario)
        self.test_intent = dict(test_intent)
        self.budget = budget
        self.run_id = run_id
        self.context_builder = ContextBuilder()
        from packages.agents.policy_validator import PolicyValidator

        self.validator = PolicyValidator()
        self.gateway: ToolGateway | None = None
        self.history: list[dict[str, Any]] = []
        self.last_proposal: ActionProposal | None = None
        self.last_policy_decision: dict[str, Any] | None = None

    def bind_gateway(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    @property
    def remaining_actions(self) -> int:
        steps = getattr(self.planner, "_steps", ())
        return max(0, len(steps) - len(self.history)) if steps else 1

    def next_action(self, observation: Observation) -> ToolCall | None:
        self.last_proposal = None
        self.last_policy_decision = None
        if self.gateway is None:
            raise ForbiddenError("planner policy has no bound execution gateway")
        observed = observation.to_record()
        remaining = min(
            self.budget.max_tool_calls - self.gateway.tool_calls,
            self.budget.max_steps - self.gateway.steps,
        )
        context = self.context_builder.build(
            test_intent=self.test_intent,
            observation=observed,
            scenario=self.scenario,
            history=self.history,
            budget={"remaining": remaining, "elapsed_s": self.gateway.elapsed},
            goal_status=str(self.scenario.get("goal_status") or "NOT_VERIFIED"),
        )
        proposal = self.planner.propose(context)
        if proposal is None:
            return None
        if proposal.observation_ref and proposal.observation_ref != observed["observation_ref"]:
            raise ForbiddenError("planner proposal references a stale observation")
        decision = self.validator.validate(
            proposal,
            run_id=self.run_id,
            bound_run_id=self.run_id,
            allowed_actions=set(self.scenario.get("allowed_actions") or ()),
            denied_actions=set(self.scenario.get("denied_actions") or ()),
            gateway=self.gateway,
            remaining_budget=remaining,
        )
        if not decision.allowed:
            raise ForbiddenError("planner proposal rejected: " + "; ".join(decision.reasons))
        self.validator.record_execution(decision)
        self.last_proposal = proposal
        self.last_policy_decision = asdict(decision)
        self.history.append(
            {
                "proposal_id": proposal.proposal_id,
                "action_type": proposal.action_type,
                "arguments": proposal.arguments,
                "decision_source": proposal.decision_source,
            }
        )
        return ToolCall(
            tool=proposal.action_type,
            args=dict(proposal.arguments),
            rationale=proposal.decision_reason,
        )
