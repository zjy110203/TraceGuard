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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from packages.core.errors import ForbiddenError
from packages.core.logging import get_logger
from packages.execution.events import EventCollector, EvidenceRecord
from packages.execution.locators import LocatorRegistry
from packages.execution.models import TestStep
from packages.execution.netpolicy import OriginPolicy

logger = get_logger(__name__)


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


@dataclass
class CleanupStatus:
    """Structured outcome of tearing a ``BrowserManager`` down.

    Cleanup is a reported dimension of its own, separate from the business
    verdict: a browser that refuses to close must be visible in the RunReport
    without turning a PASSED result into FAILED. Each field is filled by the
    code path that owns that resource, so "closed" can never be confused with
    "never opened".
    """

    browser_closed: bool = False
    browser_close_error: str = ""
    trace_stopped: bool = False  # trace finalized AND registered as evidence
    trace_stop_error: str = ""
    evidence_errors: list[str] = field(default_factory=list)
    page_crashed: bool = False
    page_crash_detail: str = ""
    execution_cancelled: bool = False
    cancellation_reason: str = ""
    playwright_stopped: bool = False
    playwright_stop_error: str = ""

    def problems(self) -> list[str]:
        """Every recorded problem as readable text, in a stable order."""
        items: list[str] = []
        if self.browser_close_error:
            items.append(f"browser_close: {self.browser_close_error}")
        if self.trace_stop_error:
            items.append(f"trace_stop: {self.trace_stop_error}")
        if self.playwright_stop_error:
            items.append(f"playwright_stop: {self.playwright_stop_error}")
        items.extend(f"evidence: {error}" for error in self.evidence_errors)
        if self.page_crashed:
            items.append(f"page_crashed: {self.page_crash_detail or 'page lost'}")
        if self.execution_cancelled:
            items.append(f"execution_cancelled: {self.cancellation_reason or 'cancelled'}")
        return items

    @property
    def is_clean(self) -> bool:
        """Did every resource get released without a lifecycle error?

        Deliberately narrow. An evidence gap (a capture that failed on a
        healthy page) and a renderer crash are execution/evidence facts,
        reported through ``evidence_errors`` / ``page_crashed`` and the run's
        execution_status — neither means a resource was left dangling, so
        neither makes an otherwise clean teardown "unclean". An operator
        cancellation is a deliberate outcome for the same reason.
        """
        return (
            self.browser_closed
            and self.trace_stopped
            and self.playwright_stopped
            and not self.browser_close_error
            and not self.trace_stop_error
            and not self.playwright_stop_error
        )

    def mark_cancelled(self, reason: str) -> None:
        """Record why a run stopped early; the first reason wins.

        Cancellation is not a cleanup defect — it explains an early stop — so
        it is reported beside the resource flags without affecting
        ``is_clean``.
        """
        if self.execution_cancelled:
            return
        self.execution_cancelled = True
        self.cancellation_reason = (reason or "cancelled without a reason")[:300]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = asdict(self)
        data["is_clean"] = self.is_clean
        data["problems"] = self.problems()
        return data


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
        # Filled by the teardown paths below; never by the business verdict.
        self.cleanup = CleanupStatus()

    def _on_crash(self, _page: Any) -> None:
        self._mark_page_lost("page crashed (renderer process gone)")

    def _mark_page_lost(self, detail: str) -> None:
        if self.page_crashed:
            return
        self.page_crashed = True
        self.crash_detail = detail[:300]
        self.cleanup.page_crashed = True
        self.cleanup.page_crash_detail = self.crash_detail
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

    # -- teardown steps ---------------------------------------------------
    # Each raw call is isolated in its own method so ``__exit__`` can record a
    # failure per step (and so tests can inject one without a real crash).

    def _stop_trace(self) -> None:
        assert self.context is not None and self._trace_path is not None
        self.context.tracing.stop(path=str(self._trace_path))

    def _close_browser(self) -> None:
        assert self.browser is not None
        self.browser.close()

    def _stop_playwright(self, exc: tuple[object, ...]) -> None:
        assert self._pw is not None
        self._pw.__exit__(*exc)  # type: ignore[arg-type]

    def _record_trace_evidence(self) -> None:
        """Register the stopped trace file as C06 evidence."""
        assert self._trace_path is not None
        import hashlib

        # Full SHA-256, not a 16-char prefix: a truncated digest cannot be
        # verified against the bytes it claims to describe.
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

    def _emit_cleanup_failure(self, event_type: str, detail: str) -> None:
        try:
            self.collector.emit(event_type, payload={"error": detail})
        except Exception:  # noqa: BLE001 - reporting must not abort teardown
            logger.exception("cleanup_event_emit_failed: %s", event_type)

    def _record_evidence_error(self, detail: str) -> None:
        """An evidence gap is recorded, never promoted to a business failure."""
        self.cleanup.evidence_errors.append(detail[:300])

    def __exit__(self, *exc: object) -> None:
        """Tear every resource down without ever raising or staying silent.

        Contract:
          * a cleanup failure is recorded in ``self.cleanup``, emitted as an
            event and logged, so the caller can always see it;
          * it never replaces the business outcome of the ``with`` body;
          * every step is isolated, so a dead trace context cannot keep the
            browser alive, and a failing ``playwright.stop()`` cannot escape
            into the runner and destroy an otherwise valid report.
        """
        if self.context is not None and self._trace_path is not None:
            try:
                self._stop_trace()
                self._record_trace_evidence()
            except Exception as err:  # noqa: BLE001 - trace must not break a run
                self.cleanup.trace_stopped = False
                self.cleanup.trace_stop_error = f"{type(err).__name__}: {err}"[:300]
                logger.error("trace_stop_failed: %s", self.cleanup.trace_stop_error)
                self._emit_cleanup_failure("evidence.trace.failed", self.cleanup.trace_stop_error)
            else:
                self.cleanup.trace_stopped = True
        else:
            # No trace was requested; nothing is left running.
            self.cleanup.trace_stopped = True

        if self.browser is not None:
            try:
                self._close_browser()
                self.cleanup.browser_closed = True
            except Exception as err:  # noqa: BLE001 - cleanup must not mask the result
                self.cleanup.browser_closed = False
                self.cleanup.browser_close_error = f"{type(err).__name__}: {err}"[:300]
                logger.error("browser_close_failed: %s", self.cleanup.browser_close_error)
                self._emit_cleanup_failure("browser.close.failed", self.cleanup.browser_close_error)
            finally:
                # Never keep a dead handle: reusing it raises "Event loop is
                # closed" and hides the first, real failure behind a second one.
                self.browser = None
        else:
            self.cleanup.browser_closed = True

        if self._pw is not None:
            try:
                self._stop_playwright(exc)
                self.cleanup.playwright_stopped = True
            except Exception as err:  # noqa: BLE001 - must never escape this frame
                self.cleanup.playwright_stopped = False
                self.cleanup.playwright_stop_error = f"{type(err).__name__}: {err}"[:300]
                logger.error("playwright_stop_failed: %s", self.cleanup.playwright_stop_error)
                self._emit_cleanup_failure(
                    "browser.playwright.failed", self.cleanup.playwright_stop_error
                )
            finally:
                self._pw = None
        else:
            self.cleanup.playwright_stopped = True

        # The page/context belonged to the browser: they are dead now too.
        self.page = None
        self.context = None

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
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self._record_evidence_error(f"screenshot[{step_id}]: {error}")
            self.collector.emit(
                "evidence.screenshot.failed",
                step_id=step_id,
                payload={"error": error, "target_gone": self.page_crashed},
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
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self._record_evidence_error(f"dom_snapshot[{step_id}]: {error}")
            self.collector.emit(
                "evidence.dom.failed",
                step_id=step_id,
                payload={"error": error},
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
            failure_evidence: list[str] = []
            if (
                not target_gone
                and type(exc).__name__ == "TimeoutError"
                and step.action in {"click", "fill", "assert_visible", "assert_text"}
            ):
                # Read the live DOM after the timeout. A timeout alone is not
                # enough to blame a locator (the node may exist but be disabled).
                try:
                    selector = self._target(step.target)
                    count = page.locator(selector).count()
                    ev = EvidenceRecord.create(
                        evidence_type="locator_failure",
                        source=f"browser:{step.step_id}",
                        run_id=run_id,
                        step_id=step.step_id,
                        summary=f"locator timeout: {count} matching nodes",
                        source_trust="observed",
                        payload={
                            "selector": selector,
                            "match_count": count,
                            "error_type": "TimeoutError",
                            "error": detail,
                        },
                    )
                    self._evidence.append(ev)
                    failure_evidence.append(ev.evidence_id)
                    for eid in (
                        self._capture_screenshot(run_id, step.step_id, "failure"),
                        self._capture_dom(run_id, step.step_id),
                    ):
                        if eid:
                            failure_evidence.append(eid)
                except Exception as capture_exc:
                    self._record_evidence_error(f"locator failure capture: {capture_exc}")
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
                tuple(failure_evidence),
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
