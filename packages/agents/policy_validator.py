"""Independent fail-closed checks for proposed actions (never executes them)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from packages.agents.exploration import _DESTRUCTIVE_INTENT, TOOL_SPECS, ToolGateway
from packages.agents.planner import ActionProposal
from packages.agents.secret_resolver import SECRET_VALUE_MARKER


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]
    action_fingerprint: str


class PolicyValidator:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def validate(
        self,
        proposal: ActionProposal,
        *,
        run_id: str,
        bound_run_id: str,
        allowed_actions: set[str],
        denied_actions: set[str],
        gateway: ToolGateway,
        remaining_budget: int,
    ) -> PolicyDecision:
        reasons: list[str] = []
        action = proposal.action_type
        spec = TOOL_SPECS.get(action)
        if _DESTRUCTIVE_INTENT.search(action):
            reasons.append("destructive actions are disabled by policy")
        if not run_id or run_id != bound_run_id:
            reasons.append("run permission mismatch")
        if spec is None or action not in allowed_actions:
            reasons.append("action is outside the allowlist")
        if action in denied_actions:
            reasons.append("action is explicitly denied")
        if spec is not None:
            args = proposal.arguments
            if set(args) - set(spec.allowed) or set(spec.required) - set(args):
                reasons.append("action parameters do not match the strict schema")
            selector = str(args.get("selector") or "").casefold()
            value_ref = str(args.get("value_ref") or "")
            if "password" in selector and (
                str(args.get("value") or "") != SECRET_VALUE_MARKER or not value_ref
            ):
                reasons.append("password values must use an approved secret_ref")
        else:
            args = proposal.arguments
        if _contains_destructive_intent(args):
            reasons.append("destructive actions are disabled by policy")
        if remaining_budget <= 0 or gateway.check_budget() is not None:
            reasons.append("execution budget is exhausted")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"run_id": run_id, "action": action, "arguments": args},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if fingerprint in self._seen:
            reasons.append("duplicate action proposal")
        return PolicyDecision(
            allowed=not reasons, reasons=tuple(reasons), action_fingerprint=fingerprint
        )

    def record_execution(self, decision: PolicyDecision) -> None:
        if not decision.allowed:
            raise ValueError("cannot consume a rejected policy decision")
        self._seen.add(decision.action_fingerprint)


def _contains_destructive_intent(value: Any) -> bool:
    if isinstance(value, str):
        return _DESTRUCTIVE_INTENT.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_destructive_intent(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_destructive_intent(item) for item in value)
    return False
