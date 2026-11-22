"""Stage-5 Explorer primitives: Exploration Trace, Tool Gateway, budgets, policy.

HONESTY: no LLM credential is configured in this environment (verified by
checking env var NAMES only). The decision loop is therefore an INJECTABLE
``Policy`` interface. The built-in ``ScriptedPolicy`` is a deterministic
navigation script for the approved TeamFlow scenarios — it is NOT a language
model and never claims to be. An LLM-backed policy can be plugged in later
without changing the guarded execution layer.

Security invariants (v1.1 §阶段5), enforced here, not by the policy:
  * only approved domains are reachable;
  * page content is UNTRUSTED DATA — text from the page can never grant a tool
    permission or widen a budget (no instruction parsing from DOM);
  * no arbitrary shell / file / network access through tools;
  * bounded steps, wall-clock time and tool calls; explicit stop_reason.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from packages.core.errors import ForbiddenError, ValidationError


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StopReason:
    GOAL_REACHED = "GOAL_REACHED"
    STEP_BUDGET = "STEP_BUDGET"
    TIME_BUDGET = "TIME_BUDGET"
    TOOL_CALL_BUDGET = "TOOL_CALL_BUDGET"
    POLICY_STOP = "POLICY_STOP"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Budget:
    """Hard bounds; enforced by ToolGateway regardless of the policy."""

    max_steps: int = 30
    max_tool_calls: int = 60
    max_duration_s: float = 60.0
    allowed_domains: tuple[str, ...] = ("127.0.0.1", "localhost")

    def validate(self) -> None:
        if self.max_steps < 1 or self.max_tool_calls < 1 or self.max_duration_s <= 0:
            raise ValidationError("budget bounds must be positive")
        if not self.allowed_domains:
            raise ValidationError("at least one approved domain is required")


@dataclass(frozen=True)
class ToolCall:
    """A requested action from the policy. Never trusted until gateway checks."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""  # metadata only; never a permission source


@dataclass(frozen=True)
class Observation:
    """What the adapter observed. ``page_text`` is UNTRUSTED input data."""

    url: str
    page_text: str
    visible_testids: tuple[str, ...] = ()
    step_index: int = 0


class Policy(Protocol):
    """Decision interface. Implementations decide the next tool call.

    The policy is NOT trusted for permissions or budgets — ToolGateway is the
    single enforcement point. Implementations may be an LLM, a rule engine, or
    a deterministic script; that choice is recorded in the trace's agent_version.
    """

    def next_action(self, observation: Observation) -> ToolCall | None: ...


ALLOWED_TOOLS = frozenset({"navigate", "fill", "click", "wait", "snapshot"})


class ToolGateway:
    """Validates every tool call against whitelist, domain, params and budget."""

    def __init__(self, budget: Budget, *, tool_version: str = "1.0.0") -> None:
        budget.validate()
        self.budget = budget
        self.tool_version = tool_version
        self.tool_calls = 0
        self.steps = 0
        self._started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def check_budget(self) -> str | None:
        """Return a stop_reason if any budget is exhausted, else None."""
        if self.steps >= self.budget.max_steps:
            return StopReason.STEP_BUDGET
        if self.tool_calls >= self.budget.max_tool_calls:
            return StopReason.TOOL_CALL_BUDGET
        if self.elapsed >= self.budget.max_duration_s:
            return StopReason.TIME_BUDGET
        return None

    def authorize(self, call: ToolCall) -> None:
        """Raise if the call is not allowed. Page text is never consulted."""
        if call.tool not in ALLOWED_TOOLS:
            raise ForbiddenError(f"tool not allowed: {call.tool}")
        if call.tool == "navigate":
            target = str(call.args.get("url", ""))
            parsed = urlparse(target)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or host not in self.budget.allowed_domains:
                raise ForbiddenError(f"domain not approved: {target}")
        for key in ("path", "command", "shell", "file"):
            if key in call.args:
                raise ForbiddenError(f"arbitrary {key} access is not permitted")
        if self.tool_calls >= self.budget.max_tool_calls:
            raise ForbiddenError("tool call budget exhausted")

    def record(self, call: ToolCall) -> None:
        self.tool_calls += 1
        if call.tool != "snapshot":
            self.steps += 1


@dataclass
class ExplorationTrace:
    """Full exploration trace (v1.1 §阶段5 required fields)."""

    exploration_id: str
    test_intent_ref: str
    application_version: str
    browser_version: str
    agent_version: str
    prompt_version: str
    tool_version: str
    action_sequence: list[dict[str, Any]] = field(default_factory=list)
    page_state_ref: str = ""
    event_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    stop_reason: str = ""
    source_trace_hash: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    def finalize(self) -> None:
        self.finished_at = _now()
        canonical = json.dumps(
            {
                "exploration_id": self.exploration_id,
                "action_sequence": self.action_sequence,
                "stop_reason": self.stop_reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.source_trace_hash = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScriptedPolicy:
    """Deterministic TeamFlow navigation script — NOT a language model.

    Used to prove the guarded exploration loop, trace capture and budget
    enforcement without an LLM credential. It emits only whitelisted tools and
    never reads page text to decide permissions (it patterns visible testids,
    which are untrusted data used only to choose a step, never to grant access).
    """

    def __init__(self, script: tuple[ToolCall, ...] | None = None) -> None:
        self._script = script or _DEFAULT_SCRIPT
        self._index = 0

    @property
    def agent_version(self) -> str:
        return "scripted-policy-1.0.0-NOT-LLM"

    def next_action(self, observation: Observation) -> ToolCall | None:
        if self._index >= len(self._script):
            return None
        call = self._script[self._index]
        self._index += 1
        return call


_DEFAULT_SCRIPT: tuple[ToolCall, ...] = (
    ToolCall("navigate", {"url": "http://127.0.0.1:8001/"}, "open login"),
    ToolCall("fill", {"selector": "[data-testid=username]", "value": "tester"}, "fill username"),
    ToolCall(
        "fill", {"selector": "[data-testid=password]", "value": "test-pass-123"}, "fill password"
    ),
    ToolCall("click", {"selector": "[data-testid=login-submit]"}, "submit login"),
    ToolCall("snapshot", {}, "observe projects page"),
)
