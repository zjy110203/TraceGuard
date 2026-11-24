"""Browser Manager, Action Executor and evidence collection (stage 2).

Uses Playwright with ``channel="chrome"`` to reuse the system-installed Chrome
(no browser download). All actions are deterministic and synchronous.
Screenshots + DOM snapshots + network responses are captured as C06 evidence.

R5-4.7 network gateway: the formal execution path now has the SAME network
boundary as the Explorer — every request the page makes (subresource, redirect,
iframe, websocket) passes an OriginPolicy derived from the approved target
origin. A legal click that triggers an illegal request gets that request
aborted and recorded; it never silently egresses.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from packages.core.errors import ForbiddenError
from packages.execution.events import EventCollector, EvidenceRecord
from packages.execution.locators import LocatorRegistry
from packages.execution.models import TestStep
from packages.execution.netpolicy import OriginPolicy


def browser_channel() -> str | None:
    """The Chromium channel every test/production launch must use.

    Default is the system Chrome (``channel="chrome"``) — the product contract.
    CI has no system Chrome, so it sets ``TRACEGUARD_BROWSER_CHANNEL=""`` to
    run against Playwright's bundled Chromium instead. The override is an
    environment decision, never a per-call-site decision, so the channel cannot
    drift between the Explorer and the formal runner.
    """
    raw = os.environ.get("TRACEGUARD_BROWSER_CHANNEL")
    if raw is None:
        return "chrome"
    return raw or None


def launch_args() -> list[str]:
    """Chromium flags shared by every launch (Explorer, runner, acceptance).

    GitHub's ubuntu runner mounts only 64 MB at ``/dev/shm``. A headless Chrome
    that renders a page and then screenshots it can have its renderer killed
    there, which is what the historical CI failure
    ``Page.screenshot: Protocol error`` looked like. ``--disable-dev-shm-usage``
    moves shared memory to ``/tmp`` and is a no-op on Windows/macOS, so it is
    unconditional rather than an environment special case that could drift.
    """
    return ["--disable-dev-shm-usage"]


@dataclass(frozen=True)
class StepExecution:
    step_id: str
    action: str
    ok: bool
    detail: str = ""
    evidence_ids: tuple[str, ...] = ()
    # Set when the step never ran because the page/browser disappeared. The
    # runner must NOT publish an observation for a blocked step: "not observed"
    # is not "observed false" (a crashed browser must not become a business FAIL).
    blocked: str = ""


class BrowserManager:
    """Manages Playwright lifecycle and executes test steps against TeamFlow."""

    def __init__(
        self,
        base_url: str,
        collector: EventCollector,
        *,
        locators: LocatorRegistry | None = None,
        trace_dir: Path | None = None,
        run_id: str = "",
        origin_policy: OriginPolicy | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.collector = collector
        self.locators = locators
        self.trace_dir = trace_dir
        self.run_id = run_id
        # Default permission: exactly the approved target origin. Callers may
        # narrow (never widen) it by passing an explicit policy.
        self.origin_policy = origin_policy or OriginPolicy.for_base_url(base_url)
        self._pw: Any | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._evidence: list[EvidenceRecord] = []
        self._network: list[dict[str, Any]] = []
        self.blocked_requests: list[dict[str, str]] = []
        self._trace_path: Path | None = None
        self._network_guard = threading.Lock()
        # A renderer crash / vanished page is an execution fact, not a business
        # result: it is recorded so the runner can refuse to publish a verdict
        # built on steps that never observed anything.
        self.page_crashed = False
        self.crash_detail = ""

    def _on_crash(self, _page: Any) -> None:
        self._mark_page_lost("page crashed (renderer process gone)")

    def _mark_page_lost(self, detail: str) -> None:
        if self.page_crashed:
            return
        self.page_crashed = True
        self.crash_detail = detail[:300]
        self.collector.emit("browser.page.crashed", payload={"detail": self.crash_detail})

    def _page_available(self) -> bool:
        """True only when Playwright calls on the page can be expected to work."""
        page = self.page
        if page is None or self.page_crashed:
            return False
        try:
            if page.is_closed():
                self._mark_page_lost("page closed before the step could run")
                return False
            return True
        except Exception:  # noqa: BLE001 - transport already gone
            self._mark_page_lost("page closed (browser transport unavailable)")
            return False

    @staticmethod
    def _is_target_gone(exc: BaseException) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("has been closed", "target closed", "target page", "crash")
        )

    def _guard_route(self, route: Any) -> None:
        """The network security gateway: default-deny for every request."""
        url = str(route.request.url)
        if self.origin_policy.allows(url):
            route.continue_()
            return
        reason = self.origin_policy.refusal_reason(url)
        with self._network_guard:
            if len(self.blocked_requests) < 50:  # bounded: a page spamming
                # blocked requests must not balloon the trace
                self.blocked_requests.append({"url": url[:300], "reason": reason})
        try:
            route.abort()
        except Exception:  # noqa: BLE001 - a dead page must not break the guard
            pass

    def _on_popup(self, popup: Any) -> None:
        """Popups are an egress channel: close before they load anything."""
        try:
            popup.close()
        except Exception:  # noqa: BLE001
            pass
        with self._network_guard:
            if len(self.blocked_requests) < 50:
                self.blocked_requests.append(
                    {"url": str(getattr(popup, "url", ""))[:300], "reason": "popup closed"}
                )

    def _on_download(self, download: Any) -> None:
        try:
            download.cancel()
        except Exception:  # noqa: BLE001
            pass
        with self._network_guard:
            if len(self.blocked_requests) < 50:
                self.blocked_requests.append(
                    {
                        "url": str(getattr(download, "url", ""))[:300],
                        "reason": "download cancelled",
                    }
                )

    def _on_websocket(self, ws: Any) -> None:
        url = str(getattr(ws, "url", ""))
        if not self.origin_policy.allows(url):
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            with self._network_guard:
                if len(self.blocked_requests) < 50:
                    self.blocked_requests.append({"url": url[:300], "reason": "websocket closed"})

    def __enter__(self) -> BrowserManager:
        self._pw = sync_playwright()
        playwright = self._pw.__enter__()
        self.browser = playwright.chromium.launch(
            channel=browser_channel(), headless=True, args=launch_args()
        )
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        # EVERY request the page makes is checked against the approved origin —
        # navigation, subresources, iframes, fetch/XHR, websockets. A legal
        # click that triggers an off-origin request gets that request aborted.
        self.context.route("**/*", self._guard_route)
        if self.trace_dir is not None:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            self._trace_path = self.trace_dir / f"trace_{self.run_id or 'session'}.zip"
            # Stage 2 task 16: Trace evidence, alongside screenshot/network.
            self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.page = self.context.new_page()
        self.page.on("crash", self._on_crash)
        self.page.on("response", self._on_response)
        self.page.on("popup", self._on_popup)
        self.page.on("download", self._on_download)
        self.page.on("websocket", self._on_websocket)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.context is not None and self._trace_path is not None:
            try:
                self.context.tracing.stop(path=str(self._trace_path))
                import hashlib

                # Full SHA-256, not a 16-char prefix: a truncated digest cannot
                # be verified against the bytes it claims to describe.
                digest = hashlib.sha256(self._trace_path.read_bytes()).hexdigest()
                self._evidence.append(
                    EvidenceRecord.create(
                        evidence_type="playwright_trace",
                        source="browser:context",
                        run_id=self.run_id,
                        step_id=None,
                        summary=f"Playwright trace ({self._trace_path.stat().st_size} bytes)",
                        attachment_ref=str(self._trace_path),
                        integrity_check=f"sha256:{digest}",
                        source_trust="observed",
                        payload={"path": str(self._trace_path), "digest": digest},
                    )
                )
            except Exception as exc:  # noqa: BLE001 - trace must not break a run
                self.collector.emit(
                    "evidence.trace.failed",
                    payload={"error": f"{type(exc).__name__}: {exc}"[:300]},
                )
        if self.browser:
            try:
                self.browser.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                self.collector.emit(
                    "browser.close.failed",
                    payload={"error": f"{type(exc).__name__}: {exc}"[:300]},
                )
        if self._pw is not None:
            self._pw.__exit__(*exc)  # type: ignore[arg-type]

    def _on_response(self, response: Any) -> None:
        if "/api/" in response.url:
            self._network.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "method": response.request.method,
                }
            )

    def security_evidence(self, run_id: str) -> EvidenceRecord | None:
        """One evidence record for every blocked network attempt (if any)."""
        if not self.blocked_requests:
            return None
        ev = EvidenceRecord.create(
            evidence_type="network_blocked",
            source="browser:network-gateway",
            run_id=run_id,
            step_id=None,
            summary=f"{len(self.blocked_requests)} request(s) blocked by the origin policy",
            source_trust="observed",
            payload={"blocked": list(self.blocked_requests)},
        )
        self._evidence.append(ev)
        self.collector.emit(
            "security.request_blocked",
            payload={"count": len(self.blocked_requests)},
        )
        return ev

    def evidence(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._evidence)

    def _target(self, raw: str) -> str:
        """Resolve a logical ``loc:`` target via the registry; raw CSS unchanged."""
        if self.locators is None:
            return raw
        return self.locators.resolve_target(raw)

    def _capture_screenshot(self, run_id: str, step_id: str, label: str) -> str:
        """Capture a screenshot as evidence, or record WHY it is missing.

        Evidence capture must never fabricate a business failure: the action has
        already succeeded when this runs, so a Chromium ``Page.screenshot``
        protocol error (crashed renderer / closed target — the historical CI
        failure) is recorded as a missing-evidence event, not turned into a
        failed step. The run-level crash flag keeps the verdict honest.
        """
        if not self._page_available():
            self.collector.emit(
                "evidence.screenshot.skipped",
                step_id=step_id,
                payload={"reason": self.crash_detail or "page unavailable"},
            )
            return ""
        assert self.page is not None
        try:
            shot = self.page.screenshot(full_page=False, timeout=15000)
        except Exception as exc:  # noqa: BLE001 - evidence gap, not a step failure
            if self._is_target_gone(exc):
                self._mark_page_lost(f"screenshot failed: {exc}")
            self.collector.emit(
                "evidence.screenshot.failed",
                step_id=step_id,
                payload={
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "target_gone": self.page_crashed,
                },
            )
            return ""
        import hashlib

        # Content-addressed integrity: the digest must describe exactly these
        # bytes, at full length (a 16-hex prefix is not verifiable evidence).
        digest = hashlib.sha256(shot).hexdigest()
        ref = f"screenshots/{run_id}_{step_id}_{label}.png"
        ev = EvidenceRecord.create(
            evidence_type="screenshot",
            source=f"browser:{step_id}",
            run_id=run_id,
            step_id=step_id,
            summary=f"screenshot {label} ({len(shot)} bytes)",
            attachment_ref=ref,
            integrity_check=f"sha256:{digest}",
            source_trust="observed",
            payload={"bytes": len(shot), "digest": digest, "image": shot.hex()},
        )
        self._evidence.append(ev)
        self.collector.emit(
            "evidence.screenshot.captured",
            step_id=step_id,
            payload={"evidence_id": ev.evidence_id, "integrity_check": ev.integrity_check},
        )
        return ev.evidence_id

    def _capture_dom(self, run_id: str, step_id: str) -> str:
        if not self._page_available():
            self.collector.emit(
                "evidence.dom.skipped",
                step_id=step_id,
                payload={"reason": self.crash_detail or "page unavailable"},
            )
            return ""
        assert self.page is not None
        try:
            body = self.page.locator("body").inner_text(timeout=10000)[:2000]
        except Exception as exc:  # noqa: BLE001 - evidence gap, not a step failure
            if self._is_target_gone(exc):
                self._mark_page_lost(f"dom snapshot failed: {exc}")
            self.collector.emit(
                "evidence.dom.failed",
                step_id=step_id,
                payload={"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
            return ""
        ev = EvidenceRecord.create(
            evidence_type="dom_snapshot",
            source=f"browser:{step_id}",
            run_id=run_id,
            step_id=step_id,
            summary=f"DOM text snapshot ({len(body)} chars)",
            source_trust="observed",
            payload={"text": body},
        )
        self._evidence.append(ev)
        return ev.evidence_id

    def execute_step(self, step: TestStep, run_id: str) -> StepExecution:
        assert self.page is not None, "BrowserManager must be used as context manager"
        if not self._page_available():
            # Fail fast and honestly: a dead page cannot observe anything, so
            # the step is BLOCKED (not "failed") and later steps stop burning
            # their per-action timeouts on a closed target.
            detail = self.crash_detail or "page unavailable (closed or crashed)"
            self.collector.emit(
                "step.blocked",
                step_id=step.step_id,
                payload={"action": step.action, "reason": detail},
            )
            return StepExecution(step.step_id, step.action, False, detail, blocked=detail)
        page = self.page
        self.collector.emit(
            "step.started",
            step_id=step.step_id,
            payload={"action": step.action, "target": step.target},
        )
        try:
            if step.action == "navigate":
                if step.target.startswith("http"):
                    url = step.target
                else:
                    url = f"{self.base_url}{step.target}"
                # The requested URL must be approved BEFORE the browser moves.
                if not self.origin_policy.allows(url):
                    raise ForbiddenError(self.origin_policy.refusal_reason(url))
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # A redirect may land somewhere unapproved: verify the FINAL
                # url, not just the requested one (same rule as the Explorer).
                if not self.origin_policy.allows(page.url):
                    final = page.url
                    try:
                        page.goto("about:blank")
                    except Exception:  # noqa: BLE001
                        pass
                    raise ForbiddenError(f"navigation ended on an unapproved origin: {final}")
                detail = f"navigated to {page.url}"
            elif step.action == "fill":
                selector = self._target(step.target)
                page.locator(selector).fill(step.value, timeout=10000)
                detail = f"filled {selector}"
            elif step.action == "click":
                selector = self._target(step.target)
                page.locator(selector).click(timeout=10000)
                detail = f"clicked {selector}"
            elif step.action == "wait":
                page.wait_for_timeout(int(step.value or "300"))
                detail = f"waited {step.value}ms"
            elif step.action == "assert_visible":
                selector = self._target(step.target)
                page.locator(selector).wait_for(state="visible", timeout=10000)
                detail = f"visible: {selector}"
            elif step.action == "assert_text":
                selector = self._target(step.target)
                actual = page.locator(selector).inner_text(timeout=10000)
                if step.value not in actual:
                    detail = f"expected '{step.value}' in '{actual}'"
                    self.collector.emit(
                        "step.assertion.failed", step_id=step.step_id, payload={"detail": detail}
                    )
                    return StepExecution(step.step_id, step.action, False, detail)
                detail = f"text matched: {step.value}"
            elif step.action == "assert_not_visible":
                selector = self._target(step.target)
                page.locator(selector).wait_for(state="hidden", timeout=10000)
                detail = f"not visible: {selector}"
            else:
                detail = f"unsupported action: {step.action}"
                return StepExecution(step.step_id, step.action, False, detail)

            evidence_ids = tuple(
                evidence_id
                for evidence_id in (
                    self._capture_screenshot(run_id, step.step_id, step.action),
                    self._capture_dom(run_id, step.step_id),
                )
                if evidence_id
            )
            self.collector.emit(
                "step.completed",
                step_id=step.step_id,
                payload={"action": step.action, "evidence_ids": list(evidence_ids)},
            )
            return StepExecution(step.step_id, step.action, True, detail, evidence_ids)
        except Exception as exc:  # noqa: BLE001 - runner records any failure
            detail = f"{type(exc).__name__}: {exc}"[:500]
            target_gone = self._is_target_gone(exc)
            if target_gone:
                self._mark_page_lost(detail)
            self.collector.emit(
                "step.error",
                step_id=step.step_id,
                payload={"action": step.action, "error": detail, "target_gone": target_gone},
            )
            return StepExecution(
                step.step_id,
                step.action,
                False,
                detail,
                blocked=detail if target_gone else "",
            )

    def network_evidence(self, run_id: str) -> EvidenceRecord:
        ev = EvidenceRecord.create(
            evidence_type="network_response",
            source="browser:network",
            run_id=run_id,
            step_id=None,
            summary=f"{len(self._network)} API responses observed",
            source_trust="observed",
            payload={"responses": list(self._network)},
        )
        self._evidence.append(ev)
        return ev
