"""Stage-5 Explorer workflow: policy -> guarded tools -> Exploration Trace.

The browser adapter executes only whitelisted, gateway-authorized calls. The
Trace is real evidence of what the browser did; exploration success is NOT a
test verdict and never promotes a TestCase (v1.1 §阶段5 "明确不做").
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from packages.agents.decision_records import make_decision_record
from packages.agents.exploration import (
    Budget,
    ExplorationTrace,
    GoalStatus,
    Observation,
    Policy,
    ScriptedPolicy,
    StopReason,
    ToolCall,
    ToolGateway,
    evaluate_goal,
    intent_of,
    sanitize_action,
)
from packages.core.errors import ForbiddenError
from packages.core.logging import get_logger
from packages.execution.browser import browser_channel, launch_args
from packages.execution.netpolicy import OriginPolicy

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PlaywrightAdapter:
    """Executes authorized tool calls against a real browser, with snapshots.

    Defence in depth beyond the gateway (R5-4.3 §5.3): the browser context
    itself refuses to load subresources or follow redirects to an origin that is
    not approved, so a page cannot exfiltrate a request even if a policy call
    somehow slipped through. Every action is bounded by ``max_action_s``.
    """

    def __init__(
        self,
        trace: ExplorationTrace,
        budget: Budget | None = None,
        *,
        sensitive_selectors: tuple[str, ...] = (),
    ) -> None:
        self.trace = trace
        self.budget = budget or Budget()
        self.sensitive_selectors = sensitive_selectors
        # The route guard uses the SAME OriginPolicy implementation as the
        # formal BrowserManager (R5-4.7): one network boundary, two consumers.
        # A Budget without explicit origins stays hostname-scoped — inventing a
        # default-port origin here would block every real target.
        self.origin_policy = OriginPolicy(
            allowed_domains=tuple(self.budget.allowed_domains),
            allowed_origins=tuple(self.budget.allowed_origins),
            allowed_schemes=tuple(self.budget.allowed_schemes),
        )
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None
        self._last_observed_url_path = ""
        self.blocked_requests: list[str] = []

    def _approved(self, url: str) -> bool:
        return self.origin_policy.allows(url)

    def _guard_route(self, route: Any) -> None:
        url = str(route.request.url)
        if self._approved(url):
            route.continue_()
            return
        if len(self.blocked_requests) < 50:  # bounded against request spam
            self.blocked_requests.append(url)
        try:
            route.abort()
        except Exception:  # noqa: BLE001 - a dead page must not break the guard
            pass

    def __enter__(self) -> PlaywrightAdapter:
        self._pw = sync_playwright()
        pw = self._pw.__enter__()
        self._browser = pw.chromium.launch(
            channel=browser_channel(), headless=True, args=launch_args()
        )
        context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        # Every request the page makes is checked, not just the navigation:
        # an <img src="https://evil/..."> would otherwise be a silent egress.
        context.route("**/*", self._guard_route)
        context.set_default_timeout(min(self.budget.max_action_s * 1000, 15000))
        self._page = context.new_page()
        self._page.on("popup", lambda popup: popup.close())
        self._page.on("download", lambda download: download.cancel())
        return self

    def __exit__(self, *exc: object) -> None:
        if self._browser:
            self._browser.close()
        if self._pw is not None:
            self._pw.__exit__(*exc)  # type: ignore[arg-type]

    def observe(self, step_index: int, *, phase: str = "pre") -> Observation:
        assert self._page is not None
        page = self._page
        previous_url_path = self._last_observed_url_path
        text = ""
        testids: list[str] = []
        elements: list[dict[str, Any]] = []
        dom_counts: dict[str, int] = {}
        dom_relations: list[dict[str, Any]] = []
        attrs: list[dict[str, str]] = []
        title = ""
        url_path = ""
        try:
            url_path = urlparse(page.url).path or "/"
            title = page.title()[:120]
            text = page.locator("body").inner_text(timeout=3000)[:3000]
            testids = (
                page.eval_on_selector_all(
                    "[data-testid]", "els => els.map(e => e.getAttribute('data-testid'))"
                )
                or []
            )
            # A bounded structural view: what a reviewer needs to see when
            # judging whether an assertion could be anchored to real elements.
            # Input *values* are never read — a password field's value is a
            # credential, not an observation (found leaking during R5-4.1).
            elements = (
                page.eval_on_selector_all(
                    "a,button,input,select,textarea,[data-testid]",
                    """(els, sensitiveSelectors) => els.slice(0, 40).map(e => {
                        const tag = e.tagName.toLowerCase();
                        const isField = ['input', 'textarea', 'select'].includes(tag);
                        const autocomplete = (e.getAttribute('autocomplete') || '').toLowerCase();
                        const selectorSensitive = sensitiveSelectors.some(s => {
                            try { return e.matches(s); } catch (_) { return true; }
                        });
                        const sensitive = e.type === 'password'
                            || /password|one-time-code|cc-number|cc-csc/.test(autocomplete)
                            || selectorSensitive
                            || /password|secret|token|credential/i.test(
                                e.getAttribute('data-testid') || ''
                            )
                            || /password|secret|token|credential/i.test(
                                e.getAttribute('name') || ''
                            );
                        const label = isField
                            ? (e.getAttribute('name') || e.getAttribute('type') || '')
                            : (e.innerText || '');
                        return {
                            tag: tag,
                            testid: e.getAttribute('data-testid') || '',
                            name: sensitive ? '«redacted»' : label.trim().slice(0, 60),
                            type: e.getAttribute('type') || '',
                            role: e.getAttribute('role') || '',
                            sensitive: sensitive,
                            testid: e.getAttribute('data-testid') || '',
                            // R5-5.2: visibility / disabled are OBSERVATIONS used
                            // to decide element_visible and form_state. They grant
                            // nothing and never carry a value.
                            visible: !!(e.offsetWidth || e.offsetHeight
                                       || e.getClientRects().length),
                            disabled: !!e.disabled
                        };
                    })""",
                    arg=list(self.sensitive_selectors),
                )
                or []
            )
            dom_counts = page.evaluate("""() => ({
                table_row_count: Math.min(document.querySelectorAll('tr').length, 10000),
                list_item_count: Math.min(document.querySelectorAll('li').length, 10000),
                button_count: Math.min(document.querySelectorAll('button').length, 10000),
                input_count: Math.min(
                    document.querySelectorAll('input,textarea,select').length, 10000
                )
            })""")
            dom_relations = page.evaluate("""() => Array.from(document.querySelectorAll('tr,li'))
                .slice(0, 100).map((e, i) => ({
                    relation: e.tagName.toLowerCase() === 'tr' ? 'row_in_table' : 'item_in_list',
                    index: i,
                    testid: e.getAttribute('data-testid') || '',
                    parent_testid: e.closest('[data-testid]')?.getAttribute('data-testid') || ''
                }))""")
            attrs = page.eval_on_selector_all(
                "[data-testid]",
                """(els, sensitiveSelectors) => els.slice(0, 100).map(e => ({
                testid: e.getAttribute('data-testid') || '',
                role: e.getAttribute('role') || '',
                sensitive: sensitiveSelectors.some(s => {
                    try { return e.matches(s); } catch (_) { return true; }
                })
                    || /password|one-time-code|cc-number|cc-csc/i.test(
                        e.getAttribute('autocomplete') || ''
                    )
                    || /password|secret|token|credential/i.test(
                        e.getAttribute('data-testid') || ''
                    ),
                href: e.tagName.toLowerCase() === 'a'
                    ? (e.getAttribute('href') || '').slice(0, 200) : ''
            }))""",
                arg=list(self.sensitive_selectors),
            )
        except Exception:  # noqa: BLE001 - blank page before first navigate
            pass
        self._last_observed_url_path = url_path
        return Observation(
            url=page.url,
            page_text=text,  # UNTRUSTED: never used as a permission source
            visible_testids=tuple(t for t in testids if t),
            step_index=step_index,
            title=title,
            url_path=url_path,
            visible_elements=tuple(dict(e) for e in elements if isinstance(e, dict)),
            dom_counts={str(k): int(v) for k, v in dom_counts.items()},
            dom_relations=tuple(x for x in dom_relations if isinstance(x, dict))[:100],
            attrs=tuple(x for x in attrs if isinstance(x, dict))[:100],
            page_state="loaded" if title or url_path else "unavailable",
            previous_url_path=previous_url_path,
            phase=phase,
        )

    def execute(self, call: ToolCall) -> dict[str, Any]:
        assert self._page is not None
        page = self._page
        detail = ""
        deadline = time.monotonic() + self.budget.max_action_s
        if call.tool == "navigate":
            page.goto(
                str(call.args["url"]),
                wait_until="domcontentloaded",
                timeout=int(self.budget.max_action_s * 1000),
            )
            # A redirect may land somewhere unapproved: verify the *final* URL,
            # not just the requested one, and leave the page if it did.
            if not self._approved(page.url):
                target = page.url
                page.goto("about:blank")
                raise ForbiddenError(f"navigation ended on an unapproved origin: {target}")
            detail = page.url
        elif call.tool == "fill":
            page.locator(str(call.args["selector"])).fill(
                str(call.args["value"]), timeout=int(self.budget.max_action_s * 1000)
            )
            detail = "filled"
        elif call.tool == "click":
            page.locator(str(call.args["selector"])).click(
                timeout=int(self.budget.max_action_s * 1000)
            )
            detail = "clicked"
        elif call.tool == "wait":
            ms = int(call.args.get("ms", 200))
            page.wait_for_timeout(min(ms, self.budget.max_wait_ms))
            detail = f"waited {min(ms, self.budget.max_wait_ms)}ms"
        elif call.tool == "snapshot":
            detail = page.locator("body").inner_text(timeout=3000)[:500]
        elif call.tool == "assert_visible":
            page.locator(str(call.args["selector"])).wait_for(
                state="visible", timeout=int(self.budget.max_action_s * 1000)
            )
            detail = f"visible: {call.args['selector']}"
        elif call.tool == "assert_not_visible":
            page.locator(str(call.args["selector"])).wait_for(
                state="hidden", timeout=int(self.budget.max_action_s * 1000)
            )
            detail = f"not visible: {call.args['selector']}"
        elif call.tool == "assert_text":
            expected = str(call.args.get("text") or call.args.get("value") or "")
            actual = page.locator(str(call.args["selector"])).inner_text(timeout=10000)
            if expected not in actual:
                raise AssertionError(f"expected {expected!r} in {actual[:200]!r}")
            detail = f"text matched: {expected}"
        else:  # defence in depth — gateway should have rejected already
            raise ForbiddenError(f"unsupported tool: {call.tool}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"action {call.tool} exceeded {self.budget.max_action_s}s")
        return {"tool": call.tool, "args": call.args, "detail": detail}


class Explorer:
    """Drives a guarded exploration loop and produces an ExplorationTrace."""

    def __init__(
        self,
        *,
        policy: Policy | None = None,
        budget: Budget | None = None,
        application_version: str = "teamflow-0.1.0",
        browser_version: str = "system-chrome",
        sensitive_selectors: tuple[str, ...] = (),
        trace_hash_version: int = 2,
    ) -> None:
        self.policy = policy or ScriptedPolicy()
        self.budget = budget or Budget()
        self.application_version = application_version
        self.browser_version = browser_version
        self.sensitive_selectors = sensitive_selectors
        self.trace = ExplorationTrace(
            exploration_id=f"exp_{uuid.uuid4().hex[:12]}",
            test_intent_ref="",
            application_version=application_version,
            browser_version=browser_version,
            agent_version=getattr(self.policy, "agent_version", "custom-policy"),
            prompt_version="none-scripted"
            if isinstance(self.policy, ScriptedPolicy)
            else "external",
            tool_version="1.0.0",
        )
        self.trace.hash_version = trace_hash_version

    def explore(
        self,
        test_intent_ref: str,
        base_url: str = "http://127.0.0.1:8001",
        *,
        intent: object | None = None,
    ) -> ExplorationTrace:
        """Run the exploration. Budget/stop reasons are real, not cosmetic.

        Every iteration observes *before* asking the policy what to do, so the
        page state produced by the previous action is captured — including the
        final one, which is what the goal evaluator needs to judge whether the
        business goal was actually reached.

        R5-4.6: the full TestIntent is embedded in the trace (and covered by its
        hash), and the loop's *end* is never conflated with the *goal*: a policy
        that runs out of plan stops with ``PLAN_EXHAUSTED``; only
        :func:`evaluate_goal` decides whether the goal was verified.
        """
        self.trace.test_intent_ref = test_intent_ref
        self.trace.test_intent = intent_of(intent)
        gateway = ToolGateway(self.budget, tool_version=self.trace.tool_version)
        bind_gateway = getattr(self.policy, "bind_gateway", None)
        if callable(bind_gateway):
            bind_gateway(gateway)
        stop = StopReason.POLICY_STOP
        with PlaywrightAdapter(
            self.trace, self.budget, sensitive_selectors=self.sensitive_selectors
        ) as adapter:
            while True:
                reason = gateway.check_budget()
                if reason is not None:
                    stop = reason
                    break
                observation = adapter.observe(gateway.steps)
                if observation.url and not observation.url.startswith("about:"):
                    self.trace.page_state_ref = observation.url
                    # Structured, bounded, redacted-by-construction: page text
                    # is truncated data, never a permission source.
                    self.trace.record_observation(observation)
                try:
                    call = self.policy.next_action(observation)
                except Exception as exc:  # noqa: BLE001 - a policy error is trace data
                    # The decision itself failed (bad structured output, guard
                    # mismatch, provider exhaustion). It is recorded, then the
                    # loop stops: a broken decision loop must not look like a
                    # completed plan.
                    detail = f"{type(exc).__name__}: {exc}"[:300]
                    self.trace.agent_decisions.append(
                        {
                            "step_index": gateway.steps,
                            "observation": observation.state_summary,
                            "action": "(policy)",
                            "rationale": "",
                            "result": "ERROR",
                            "error": detail,
                            "decided_at": _now(),
                            "policy_version": self.trace.agent_version,
                            "prompt_version": self.trace.prompt_version,
                        }
                    )
                    stop = StopReason.POLICY_STOP
                    break
                if call is None:
                    # A script (or an agent's "finish") simply ran out of plan.
                    # That is NOT goal evidence; only the goal evaluator can
                    # say the business outcome was reached.
                    stop = StopReason.PLAN_EXHAUSTED
                    break
                try:
                    gateway.authorize(call)
                except ForbiddenError as exc:
                    logger.warning("explorer_tool_rejected: %s", exc)
                    self.trace.action_sequence.append(
                        {"tool": call.tool, "rejected": True, "reason": str(exc)}
                    )
                    self.trace.security_events.append(
                        {
                            "kind": "tool_rejected",
                            "step_index": gateway.steps,
                            "tool": call.tool,
                            "reason": str(exc),
                            "at": _now(),
                        }
                    )
                    self.trace.agent_decisions.append(
                        {
                            "step_index": gateway.steps,
                            "observation": observation.state_summary,
                            "action": call.tool,
                            "rationale": call.rationale,
                            "result": "REJECTED",
                            "rejection_reason": str(exc),
                            "decided_at": _now(),
                            "policy_version": self.trace.agent_version,
                            "prompt_version": self.trace.prompt_version,
                        }
                    )
                    stop = StopReason.POLICY_STOP
                    break
                try:
                    raw_result = adapter.execute(call)
                except Exception as exc:  # noqa: BLE001 - a failed action is trace data
                    # The action failed in the browser; record it (with the same
                    # redaction as a success) and let the policy see the new
                    # state, so an error can be recovered from instead of
                    # aborting the whole exploration.
                    detail = f"{type(exc).__name__}: {exc}"[:300]
                    safe, _ = sanitize_action({"tool": call.tool, "args": dict(call.args)})
                    self._record_decision(observation, safe, {"error": detail})
                    self.trace.action_sequence.append({**safe, "error": detail})
                    self.trace.agent_decisions.append(
                        {
                            "step_index": gateway.steps,
                            "observation": observation.state_summary,
                            "action": call.tool,
                            "rationale": call.rationale,
                            "result": "ERROR",
                            "error": detail,
                            "decided_at": _now(),
                            "policy_version": self.trace.agent_version,
                            "prompt_version": self.trace.prompt_version,
                        }
                    )
                    gateway.record(call)
                    continue
                gateway.record(call)
                # Redact before the action enters the trace: the in-memory trace
                # never holds a plaintext credential, not just the persisted one.
                safe_result, _ = sanitize_action(raw_result)
                self._record_decision(observation, safe_result, {"status": "OK"})
                self.trace.action_sequence.append(safe_result)
                self.trace.agent_decisions.append(
                    {
                        "step_index": gateway.steps,
                        "observation": observation.state_summary,
                        "action": call.tool,
                        "rationale": call.rationale,
                        "result": "OK",
                        "detail": str(raw_result.get("detail", ""))[:200],
                        "decided_at": _now(),
                        "policy_version": self.trace.agent_version,
                        "prompt_version": self.trace.prompt_version,
                    }
                )
        # Network-level refusals the page tried to make (subresource / redirect
        # egress) are part of the security record even though no tool call was
        # involved.
        for url in adapter.blocked_requests:
            self.trace.security_events.append(
                {"kind": "request_blocked", "url": url[:300], "at": _now()}
            )
        self.trace.stop_reason = stop
        # Goal evaluation happens AFTER execution, from observed evidence only.
        # PLAN_EXHAUSTED / budget stops never imply VERIFIED; a wrong password
        # that leaves the page on /login evaluates to NOT_VERIFIED here.
        evaluation = evaluate_goal(self.trace.test_intent, self.trace.observations)
        self.trace.goal_status = evaluation.status
        self.trace.goal_checks = [c.to_dict() for c in evaluation.checks]
        self.trace.goal_reason = evaluation.reason
        if evaluation.status == GoalStatus.VERIFIED and stop == StopReason.PLAN_EXHAUSTED:
            # The loop ended by plan *and* the goal checks passed: annotate the
            # stop reason so the semantics stay explicit and auditable.
            self.trace.stop_reason = StopReason.GOAL_VERIFIED
        self.trace.finalize()
        return self.trace

    def _record_decision(
        self, observation: Observation, actual_action: dict[str, Any], result: dict[str, Any]
    ) -> None:
        proposal = getattr(self.policy, "last_proposal", None)
        policy_decision = getattr(self.policy, "last_policy_decision", None)
        if proposal is None or not isinstance(policy_decision, dict):
            return
        self.trace.decision_records.append(
            make_decision_record(
                proposal={
                    "proposal_id": proposal.proposal_id,
                    "action_type": proposal.action_type,
                    "arguments": proposal.arguments,
                    "expected_outcome": proposal.expected_outcome,
                    "decision_reason": proposal.decision_reason,
                },
                policy_decision=policy_decision,
                actual_action=actual_action,
                result=result,
                observation_ref=observation.to_record()["observation_ref"],
                decision_source=proposal.decision_source,
            ).to_dict()
        )
