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
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from packages.core.errors import ForbiddenError, ValidationError
from packages.execution.dataref import (
    REDACTED_MARKER,
    SENSITIVE_HINT,
    derive_value_ref,
    is_placeholder,
)

# Selector syntax allowed through the gateway: attribute/id/class/tag/child
# combinators only. Anything that looks like code, a URL scheme or a shadow-DOM
# pierce is rejected before Playwright ever sees it.
_SELECTOR_SAFE = re.compile(r"^[A-Za-z0-9\s\[\]=\"'#._:\->~*+,()]+$")
_SELECTOR_DENY = re.compile(r"(javascript|vbscript|data)\s*:", re.IGNORECASE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StopReason:
    """Why the execution loop stopped — NEVER a business-goal verdict.

    R5-4.6 separates "the loop ended" from "the goal was verified":

    * ``PLAN_EXHAUSTED`` means a scripted plan ran out of steps. It says
      nothing about the business goal. Only the goal evaluator
      (:func:`evaluate_goal`) may return ``GOAL_VERIFIED`` evidence.
    """

    GOAL_VERIFIED = "GOAL_VERIFIED"
    PLAN_EXHAUSTED = "PLAN_EXHAUSTED"
    STEP_BUDGET = "STEP_BUDGET"
    TIME_BUDGET = "TIME_BUDGET"
    TOOL_CALL_BUDGET = "TOOL_CALL_BUDGET"
    POLICY_STOP = "POLICY_STOP"
    ERROR = "ERROR"


class ExplorationStatus:
    """Execution-level status of the exploration process itself."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class GoalStatus:
    """Business-goal verification outcome, independent of execution status."""

    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EVALUATED = "NOT_EVALUATED"


# --------------------------------------------------------------- goal model


def _normalize_path(raw: str) -> str:
    path = raw.strip()
    if not path:
        return "/"
    if "://" in path:  # a full URL: keep only its path component
        path = urlparse(path).path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def intent_of(raw: Any) -> dict[str, Any]:
    """Canonical intent dict from a ``TestIntent`` or a persisted mapping.

    Accepts both shapes so the live path (dataclass) and the reload path (JSON)
    share one goal-evaluation implementation.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    return {
        "intent_id": getattr(raw, "intent_id", ""),
        "description": getattr(raw, "description", ""),
        "source_ref": getattr(raw, "source_ref", ""),
        "goal": getattr(raw, "goal", ""),
        "expected_outcomes": list(getattr(raw, "expected_outcomes", ()) or ()),
        "target": getattr(raw, "target", ""),
        "required_assertions": list(getattr(raw, "required_assertions", ()) or ()),
        "constraints": list(getattr(raw, "constraints", ()) or ()),
    }


@dataclass(frozen=True)
class GoalCheck:
    """One goal condition with its evidence. ``ok`` is only meaningful when
    ``evaluated`` is True; otherwise the check could not be judged here."""

    name: str
    expected: str
    observed: str
    ok: bool = False
    evaluated: bool = True
    deferred: bool = False  # verified at formal-run time, not during exploration
    evidence: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected": self.expected,
            "observed": self.observed,
            "ok": self.ok,
            "evaluated": self.evaluated,
            "deferred": self.deferred,
            "evidence": self.evidence,
            "note": self.note,
        }


@dataclass(frozen=True)
class GoalEvaluation:
    status: str
    checks: tuple[GoalCheck, ...]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "checks": [c.to_dict() for c in self.checks],
        }


_STATE_ASSERTION_FORM = re.compile(r"^state\.([A-Za-z0-9_.]+)\s*=")


def evaluate_goal(intent: Any, observations: Any) -> GoalEvaluation:
    """Judge the business goal against *observed evidence only*.

    Rules (deterministic, no invention):

    * No declared goal at all -> ``NOT_EVALUATED`` (case D: refuse to compile).
    * A declared target or expected outcome that contradicts the final
      observation -> ``NOT_VERIFIED`` (cases B and E).
    * ``state.*`` required assertions cannot be read from the page; they are
      marked ``deferred`` (evaluated by the independent Oracle at formal-run
      time) and never count as failure, but at least one *observable* check
      must pass for VERIFIED.
    * No usable observation -> ``INCONCLUSIVE``.
    """
    data = intent_of(intent)
    checks: list[GoalCheck] = []

    target = str(data.get("target") or "").strip()
    outcomes = [str(o).strip() for o in (data.get("expected_outcomes") or ()) if str(o).strip()]
    required = [str(a).strip() for a in (data.get("required_assertions") or ()) if str(a).strip()]
    goal = str(data.get("goal") or "").strip()

    if not (target or outcomes or required or goal):
        return GoalEvaluation(
            GoalStatus.NOT_EVALUATED,
            (),
            "no explicit business goal declared (goal/target/expected_outcomes all empty)",
        )

    usable = [
        o
        for o in observations or ()
        if str((o or {}).get("url") or "")
        and not str((o or {}).get("url") or "").startswith("about:")
    ]
    if not usable:
        return GoalEvaluation(
            GoalStatus.INCONCLUSIVE,
            (),
            "no usable page observation to verify the goal against",
        )
    final = dict(usable[-1])
    final_path = _normalize_path(str(final.get("url_path") or "/"))
    final_text = str(final.get("text_excerpt") or "")

    if target:
        expected_path = _normalize_path(target)
        checks.append(
            GoalCheck(
                name="target_path",
                expected=expected_path,
                observed=final_path,
                ok=expected_path == final_path,
                evidence=f"observations[{len(usable) - 1}].url_path",
            )
        )
    else:
        checks.append(
            GoalCheck(
                name="target_path",
                expected="",
                observed=final_path,
                evaluated=False,
                note="intent declares no target page; goal cannot be anchored to a route",
            )
        )

    for text in outcomes:
        checks.append(
            GoalCheck(
                name="expected_outcome",
                expected=text,
                observed=final_text[:200],
                ok=text in final_text,
                evidence=f"observations[{len(usable) - 1}].text_excerpt",
            )
        )

    for requirement in required:
        if _STATE_ASSERTION_FORM.match(requirement):
            checks.append(
                GoalCheck(
                    name="required_assertion",
                    expected=requirement,
                    observed="",
                    evaluated=False,
                    deferred=True,
                    note="independent business-state assertion; evaluated by the formal Oracle",
                )
            )
        else:
            checks.append(
                GoalCheck(
                    name="required_assertion",
                    expected=requirement,
                    observed="",
                    evaluated=False,
                    note="non state.<key>=<json> form; cannot be machine-verified in exploration",
                )
            )

    evaluated = [c for c in checks if c.evaluated]
    observable = [c for c in evaluated if not c.deferred]
    failed = [c for c in evaluated if not c.ok]
    if failed:
        reasons = "; ".join(
            f"{c.name}: expected {c.expected!r}, observed {c.observed!r}" for c in failed
        )
        return GoalEvaluation(GoalStatus.NOT_VERIFIED, tuple(checks), reasons)
    if not any(c.ok for c in observable):
        return GoalEvaluation(
            GoalStatus.INCONCLUSIVE,
            tuple(checks),
            "no goal condition could be verified against a page observation",
        )
    return GoalEvaluation(GoalStatus.VERIFIED, tuple(checks))


@dataclass(frozen=True)
class Budget:
    """Hard bounds; enforced by ToolGateway regardless of the policy."""

    max_steps: int = 30
    max_tool_calls: int = 60
    max_duration_s: float = 60.0
    max_action_s: float = 15.0
    allowed_domains: tuple[str, ...] = ("127.0.0.1", "localhost")
    # Exact scheme://host:port origins. When set, they are authoritative: a
    # hostname match alone is not enough (R5-4.3 §5.3).
    allowed_origins: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = ()
    allowed_schemes: tuple[str, ...] = ("http", "https")
    max_selector_chars: int = 200
    max_value_chars: int = 500
    max_wait_ms: int = 5000

    def validate(self) -> None:
        if self.max_steps < 1 or self.max_tool_calls < 1 or self.max_duration_s <= 0:
            raise ValidationError("budget bounds must be positive")
        if self.max_action_s <= 0 or self.max_action_s > self.max_duration_s:
            raise ValidationError("max_action_s must be positive and within the total budget")
        if not self.allowed_domains:
            raise ValidationError("at least one approved domain is required")
        if not self.allowed_schemes:
            raise ValidationError("at least one allowed scheme is required")


def origin_of(url: str) -> str:
    """Canonical ``scheme://host:port`` (default ports made explicit)."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}"


@dataclass(frozen=True)
class ToolSpec:
    """Strict parameter contract for one tool (R5-4.3 §5.2).

    The policy may be an LLM; the argument shape is not negotiable. Unknown
    keys are rejected rather than ignored, so a prompt injection cannot smuggle
    extra behaviour through an unused argument.
    """

    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    @property
    def allowed(self) -> frozenset[str]:
        return frozenset((*self.required, *self.optional))


TOOL_SPECS: dict[str, ToolSpec] = {
    "navigate": ToolSpec(("url",)),
    "fill": ToolSpec(("selector", "value"), ("value_ref",)),
    "click": ToolSpec(("selector",)),
    "wait": ToolSpec((), ("ms",)),
    "snapshot": ToolSpec(),
    "assert_visible": ToolSpec(("selector",)),
    "assert_not_visible": ToolSpec(("selector",)),
    "assert_text": ToolSpec(("selector", "text"), ("value",)),
}

# One definition for both the policy schema and the gateway. Before R5-4.3 the
# LLM schema advertised assert_text/assert_visible while the gateway rejected
# them, so a compliant model produced calls the runner could never allow.
ALLOWED_TOOLS = frozenset(TOOL_SPECS)


class ToolGateway:
    """Validates every tool call against whitelist, origin, params and budget."""

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

    # ------------------------------------------------------------- validation

    def _check_url(self, raw: Any) -> str:
        target = str(raw or "")
        if not target:
            raise ForbiddenError("navigate requires a non-empty url")
        parsed = urlparse(target)
        scheme = (parsed.scheme or "").lower()
        if scheme not in self.budget.allowed_schemes:
            raise ForbiddenError(f"scheme not allowed: {scheme or '<empty>'}")
        if parsed.username is not None or parsed.password is not None:
            raise ForbiddenError("userinfo in a navigation url is not permitted")
        host = (parsed.hostname or "").lower()
        if host not in self.budget.allowed_domains:
            raise ForbiddenError(f"domain not approved: {target}")
        port = parsed.port or (443 if scheme == "https" else 80)
        if self.budget.allowed_ports and port not in self.budget.allowed_ports:
            raise ForbiddenError(f"port not approved: {port}")
        if self.budget.allowed_origins and origin_of(target) not in {
            origin_of(o) for o in self.budget.allowed_origins
        }:
            raise ForbiddenError(f"origin not approved: {origin_of(target)}")
        if len(target) > 2048:
            raise ForbiddenError("url is too long")
        return target

    def _check_selector(self, raw: Any) -> str:
        selector = str(raw or "")
        if not selector:
            raise ForbiddenError("selector is required")
        if len(selector) > self.budget.max_selector_chars:
            raise ForbiddenError("selector is too long")
        if not _SELECTOR_SAFE.fullmatch(selector):
            raise ForbiddenError(f"selector contains unsupported syntax: {selector[:60]!r}")
        if _SELECTOR_DENY.search(selector):
            raise ForbiddenError("selector must not contain a URL scheme")
        return selector

    def _check_value(self, raw: Any) -> str:
        value = str(raw if raw is not None else "")
        if len(value) > self.budget.max_value_chars:
            raise ForbiddenError("value exceeds the allowed length")
        return value

    def authorize(self, call: ToolCall) -> None:
        """Raise if the call is not allowed. Page text is never consulted."""
        spec = TOOL_SPECS.get(call.tool)
        if spec is None:
            raise ForbiddenError(f"tool not allowed: {call.tool}")
        args = call.args if isinstance(call.args, dict) else {}
        unknown = set(args) - spec.allowed
        if unknown:
            raise ForbiddenError(f"unexpected argument(s) for {call.tool}: {sorted(unknown)}")
        missing = [key for key in spec.required if key not in args]
        if missing:
            raise ForbiddenError(f"missing required argument(s) for {call.tool}: {missing}")

        if call.tool == "navigate":
            self._check_url(args.get("url"))
        if "selector" in spec.allowed:
            self._check_selector(args.get("selector"))
        if call.tool == "fill":
            value = self._check_value(args.get("value"))
            ref = str(args.get("value_ref") or "")
            if is_placeholder(value) and not ref:
                # A redacted marker is evidence of masking, not an input.
                raise ForbiddenError("fill received a redaction placeholder without a value_ref")
        if call.tool in {"wait"}:
            ms = args.get("ms", 200)
            if not isinstance(ms, int) or isinstance(ms, bool) or ms < 0:
                raise ForbiddenError("wait ms must be a non-negative integer")
            if ms > self.budget.max_wait_ms:
                raise ForbiddenError(f"wait exceeds the {self.budget.max_wait_ms}ms cap")
        if call.tool == "assert_text":
            text = args.get("text", args.get("value"))
            if not str(text or "").strip():
                raise ForbiddenError("assert_text requires a non-empty text")
            self._check_value(text)

        for key in ("path", "command", "shell", "file"):
            if key in args:
                raise ForbiddenError(f"arbitrary {key} access is not permitted")
        if self.tool_calls >= self.budget.max_tool_calls:
            raise ForbiddenError("tool call budget exhausted")

    def record(self, call: ToolCall) -> None:
        self.tool_calls += 1
        if call.tool != "snapshot":
            self.steps += 1


@dataclass(frozen=True)
class ToolCall:
    """A requested action from the policy. Never trusted until gateway checks."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""  # metadata only; never a permission source


@dataclass(frozen=True)
class Observation:
    """What the adapter observed. ``page_text`` is UNTRUSTED input data.

    R5-4.1 made this structured: a page-text blob alone cannot support an
    assertion, because a compiler cannot tell *where* it was or *what* changed.
    The extra fields are observations, not verdicts — nothing here grants
    permission, and page content never widens a budget.
    """

    url: str
    page_text: str
    visible_testids: tuple[str, ...] = ()
    step_index: int = 0
    title: str = ""
    url_path: str = ""
    visible_elements: tuple[dict[str, str], ...] = ()
    observed_at: str = field(default_factory=_now)
    evidence_ref: str = ""
    phase: str = "pre"  # pre = before the next action, post = after one

    @property
    def state_summary(self) -> str:
        """One-line digest: enough to compare two page states at a glance."""
        return (
            f"{self.title or '(untitled)'} @ {self.url_path or self.url} | "
            f"{len(self.visible_testids)} testids | {len(self.visible_elements)} elements"
        )

    def to_record(self, *, max_text: int = 400) -> dict[str, Any]:
        """Persistable observation: bounded text, no unbounded DOM dump."""
        return {
            "url": self.url,
            "url_path": self.url_path,
            "title": self.title,
            "visible_testids": list(self.visible_testids),
            "visible_elements": [_safe_element(e) for e in self.visible_elements],
            "text_excerpt": self.page_text[:max_text],
            "state_summary": self.state_summary,
            "observed_at": self.observed_at,
            "step_index": self.step_index,
            "phase": self.phase,
            "evidence_ref": self.evidence_ref,
        }


def _safe_element(element: dict[str, str]) -> dict[str, str]:
    """Second line of defence: a field that *looks* sensitive carries no text.

    The adapter already refuses to read input values; this catches any other
    adapter (or a page that renders a secret as visible text) before it is
    persisted.
    """
    out = dict(element)
    testid = str(out.get("testid") or "")
    if SENSITIVE_HINT.search(testid) or SENSITIVE_HINT.search(str(out.get("name") or "")):
        out["name"] = REDACTED_MARKER
    return out


class Policy(Protocol):
    """Decision interface. Implementations decide the next tool call.

    The policy is NOT trusted for permissions or budgets — ToolGateway is the
    single enforcement point. Implementations may be an LLM, a rule engine, or
    a deterministic script; that choice is recorded in the trace's agent_version.
    """

    def next_action(self, observation: Observation) -> ToolCall | None: ...


def sanitize_action(action: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Mask sensitive args in one captured action; return (action, masked).

    Two detection paths, because a credential can hide in either place:
    the arg *key* looks sensitive (``token``/``api_key``), or the arg is a plain
    ``value`` whose context names a password field. A masked input keeps a
    ``value_ref`` so the compiler can bind it to a real provider later instead
    of replaying the placeholder.
    """
    args = action.get("args")
    if not isinstance(args, dict):
        return action, 0
    context = " ".join(
        str(v) for k, v in args.items() if k != "value" and isinstance(v, (str, int, float))
    )
    context_sensitive = bool(SENSITIVE_HINT.search(context))
    clean: dict[str, Any] = {}
    masked = 0
    for key, value in args.items():
        key_sensitive = bool(SENSITIVE_HINT.search(str(key)))
        value_sensitive = key == "value" and context_sensitive
        if (key_sensitive or value_sensitive) and value not in (None, ""):
            clean[key] = REDACTED_MARKER
            masked += 1
        else:
            clean[key] = value
    if masked:
        selector = str(args.get("selector") or args.get("target") or "")
        clean["value_ref"] = derive_value_ref(selector)
        clean["redacted"] = True
    return {**action, "args": clean}, masked


HASH_VERSION_LEGACY = 1
HASH_VERSION = 2

# Fields hashed in v2: everything that influences compilation and review.
# Deliberately EXCLUDED (derived state — hashing them would be self-reference):
# status, step_count, goal evaluation output, trace_integrity_verified,
# redacted_fields, requested_at, hash_version itself.
_HASH_VERSION_FIELDS = (
    "exploration_id",
    "test_intent",
    "action_sequence",
    "observations",
    "agent_decisions",
    "security_events",
    "stop_reason",
    "agent_version",
    "prompt_version",
    "tool_version",
    "application_version",
    "browser_version",
)


def compute_trace_hash(
    *,
    exploration_id: str,
    action_sequence: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    security_events: list[dict[str, Any]],
    stop_reason: str,
    test_intent: dict[str, Any] | None = None,
    agent_decisions: list[dict[str, Any]] | None = None,
    versions: dict[str, Any] | None = None,
    hash_version: int = HASH_VERSION,
) -> str:
    """Canonical hash over what was actually captured (already redacted).

    Hashing the post-redaction trace is the point: a hash of the plaintext form
    would describe a document that is deliberately not the one on disk.

    v2 (R5-4.6) extends the canonical scope to the fields that decide whether a
    trace can be compiled and reviewed: the TestIntent itself, every agent
    decision, and the agent/prompt/tool/application/browser versions. A trace
    whose intent or decisions are edited no longer validates against its hash.
    """
    if hash_version == HASH_VERSION_LEGACY:
        canonical = json.dumps(
            {
                "exploration_id": exploration_id,
                "action_sequence": action_sequence,
                "observations": observations,
                "security_events": security_events,
                "stop_reason": stop_reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    v = dict(versions or {})
    payload: dict[str, Any] = {
        "exploration_id": exploration_id,
        "test_intent": dict(test_intent or {}),
        "action_sequence": action_sequence,
        "observations": observations,
        "agent_decisions": list(agent_decisions or []),
        "security_events": security_events,
        "stop_reason": stop_reason,
        "agent_version": v.get("agent_version", ""),
        "prompt_version": v.get("prompt_version", ""),
        "tool_version": v.get("tool_version", ""),
        "application_version": v.get("application_version", ""),
        "browser_version": v.get("browser_version", ""),
        "hash_scope": list(_HASH_VERSION_FIELDS),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def verify_trace_record(record: dict[str, Any]) -> bool:
    """Recompute the hash from a *persisted* record and compare it.

    This is the check that makes the hash meaningful: it is verified against the
    bytes on disk (after a JSON round-trip), not against the in-memory object
    that produced them.

    Versioning: records written before R5-4.6 carry no ``hash_version`` and are
    verified with the legacy (v1) scope so historical traces keep validating —
    tightening the scope silently would mark every old trace as tampered.
    """
    declared = str(record.get("source_trace_hash") or "")
    if not declared.startswith("sha256:"):
        return False
    version = int(record.get("hash_version") or HASH_VERSION_LEGACY)
    recomputed = compute_trace_hash(
        exploration_id=str(record.get("exploration_id") or ""),
        action_sequence=list(record.get("action_sequence") or []),
        observations=list(record.get("observations") or []),
        security_events=list(record.get("security_events") or []),
        stop_reason=str(record.get("stop_reason") or ""),
        test_intent=dict(record.get("test_intent") or {}),
        agent_decisions=list(record.get("agent_decisions") or []),
        versions={
            "agent_version": str(record.get("agent_version") or ""),
            "prompt_version": str(record.get("prompt_version") or ""),
            "tool_version": str(record.get("tool_version") or ""),
            "application_version": str(record.get("application_version") or ""),
            "browser_version": str(record.get("browser_version") or ""),
        },
        hash_version=version,
    )
    return recomputed == declared


@dataclass
class ExplorationTrace:
    """Full exploration trace (v1.1 §阶段5 required fields).

    ``observations`` is the structured page-state sequence (R5-4.1): the
    compiler reads it to decide what the goal actually looked like, instead of
    assuming the last click meant success. ``agent_decisions`` is the auditable
    policy log — observation digest, action, rationale, tool result, versions.
    """

    exploration_id: str
    test_intent_ref: str
    application_version: str
    browser_version: str
    agent_version: str
    prompt_version: str
    tool_version: str
    # The full TestIntent travels WITH the trace (R5-4.6): the compiler and the
    # reviewer read the same intent the Explorer was given, and the v2 hash
    # covers it so a post-hoc edit cannot pass unnoticed.
    test_intent: dict[str, Any] = field(default_factory=dict)
    action_sequence: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    agent_decisions: list[dict[str, Any]] = field(default_factory=list)
    # Every security refusal, with its reason: a reviewer must be able to see
    # that a call was blocked and why, not just that the trace ended early.
    security_events: list[dict[str, Any]] = field(default_factory=list)
    page_state_ref: str = ""
    event_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    stop_reason: str = ""
    source_trace_hash: str = ""
    hash_version: int = HASH_VERSION
    # Goal evaluation is DERIVED from (test_intent, observations); it is stored
    # for the UI and the compile gate but deliberately excluded from the hash
    # (hashing a derivation of hashed inputs is self-reference).
    goal_status: str = ""
    goal_checks: list[dict[str, Any]] = field(default_factory=list)
    goal_reason: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    def record_observation(self, observation: Observation) -> None:
        """Append a structured observation (redaction already applied upstream)."""
        self.observations.append(observation.to_record())

    def finalize(self) -> None:
        self.finished_at = _now()
        # The hash covers what was observed and decided, not only the actions:
        # two explorations that typed the same keys but saw different pages are
        # different evidence. It is computed on the already-redacted trace.
        self.source_trace_hash = compute_trace_hash(
            exploration_id=self.exploration_id,
            action_sequence=self.action_sequence,
            observations=self.observations,
            security_events=self.security_events,
            stop_reason=self.stop_reason,
            test_intent=self.test_intent,
            agent_decisions=self.agent_decisions,
            versions={
                "agent_version": self.agent_version,
                "prompt_version": self.prompt_version,
                "tool_version": self.tool_version,
                "application_version": self.application_version,
                "browser_version": self.browser_version,
            },
            hash_version=self.hash_version,
        )

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
    # Credential-free on purpose: a default script must never embed a password.
    # Product flows build their script from the deployment's test-data
    # configuration (see ``apps/api/explorer_api.py``); traces still redact any
    # sensitive arg that passes through.
    ToolCall("navigate", {"url": "http://127.0.0.1:8001/"}, "open entry page"),
    ToolCall("snapshot", {}, "observe entry page"),
)
