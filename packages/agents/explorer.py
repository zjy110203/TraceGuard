"""Stage-5 Explorer workflow: policy -> guarded tools -> Exploration Trace.

The browser adapter executes only whitelisted, gateway-authorized calls. The
Trace is real evidence of what the browser did; exploration success is NOT a
test verdict and never promotes a TestCase (v1.1 §阶段5 "明确不做").
"""

from __future__ import annotations

import uuid
from typing import Any

from playwright.sync_api import sync_playwright

from packages.agents.exploration import (
    Budget,
    ExplorationTrace,
    Observation,
    Policy,
    ScriptedPolicy,
    StopReason,
    ToolCall,
    ToolGateway,
)
from packages.core.errors import ForbiddenError
from packages.core.logging import get_logger

logger = get_logger(__name__)


class PlaywrightAdapter:
    """Executes authorized tool calls against a real browser, with snapshots."""

    def __init__(self, trace: ExplorationTrace) -> None:
        self.trace = trace
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None

    def __enter__(self) -> PlaywrightAdapter:
        self._pw = sync_playwright()
        pw = self._pw.__enter__()
        self._browser = pw.chromium.launch(channel="chrome", headless=True)
        context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        self._page = context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._browser:
            self._browser.close()
        if self._pw is not None:
            self._pw.__exit__(*exc)  # type: ignore[arg-type]

    def observe(self, step_index: int) -> Observation:
        assert self._page is not None
        text = ""
        testids: list[str] = []
        try:
            text = self._page.locator("body").inner_text(timeout=3000)[:3000]
            testids = (
                self._page.eval_on_selector_all(
                    "[data-testid]", "els => els.map(e => e.getAttribute('data-testid'))"
                )
                or []
            )
        except Exception:  # noqa: BLE001 - blank page before first navigate
            pass
        return Observation(
            url=self._page.url,
            page_text=text,  # UNTRUSTED: never used as a permission source
            visible_testids=tuple(t for t in testids if t),
            step_index=step_index,
        )

    def execute(self, call: ToolCall) -> dict[str, Any]:
        assert self._page is not None
        page = self._page
        detail = ""
        if call.tool == "navigate":
            page.goto(str(call.args["url"]), wait_until="domcontentloaded", timeout=15000)
            detail = page.url
        elif call.tool == "fill":
            page.locator(str(call.args["selector"])).fill(str(call.args["value"]), timeout=10000)
            detail = "filled"
        elif call.tool == "click":
            page.locator(str(call.args["selector"])).click(timeout=10000)
            detail = "clicked"
        elif call.tool == "wait":
            page.wait_for_timeout(int(call.args.get("ms", 200)))
            detail = "waited"
        elif call.tool == "snapshot":
            detail = page.locator("body").inner_text(timeout=3000)[:500]
        else:  # defence in depth — gateway should have rejected already
            raise ForbiddenError(f"unsupported tool: {call.tool}")
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
    ) -> None:
        self.policy = policy or ScriptedPolicy()
        self.budget = budget or Budget()
        self.application_version = application_version
        self.browser_version = browser_version
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

    def explore(
        self, test_intent_ref: str, base_url: str = "http://127.0.0.1:8001"
    ) -> ExplorationTrace:
        """Run the exploration. Budget/stop reasons are real, not cosmetic."""
        self.trace.test_intent_ref = test_intent_ref
        gateway = ToolGateway(self.budget, tool_version=self.trace.tool_version)
        stop = StopReason.POLICY_STOP
        with PlaywrightAdapter(self.trace) as adapter:
            while True:
                reason = gateway.check_budget()
                if reason is not None:
                    stop = reason
                    break
                observation = adapter.observe(gateway.steps)
                self.trace.page_state_ref = observation.url
                call = self.policy.next_action(observation)
                if call is None:
                    stop = StopReason.GOAL_REACHED
                    break
                try:
                    gateway.authorize(call)
                except ForbiddenError as exc:
                    logger.warning("explorer_tool_rejected: %s", exc)
                    self.trace.action_sequence.append(
                        {"tool": call.tool, "rejected": True, "reason": str(exc)}
                    )
                    stop = StopReason.POLICY_STOP
                    break
                result = adapter.execute(call)
                gateway.record(call)
                self.trace.action_sequence.append(result)
        self.trace.stop_reason = stop
        self.trace.finalize()
        return self.trace
