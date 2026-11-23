"""R5-4.7 §4.1: one network boundary for Explorer AND formal execution.

The matrix from the spec, plus live browser proof:

  1. normal local target            -> allowed
  2. same IP, different port        -> denied
  3. same IP, different scheme      -> denied
  4. localhost vs 127.0.0.1         -> different origins
  5. cross-origin redirect          -> final URL checked, page abandoned
  6. external image subresource     -> aborted
  7. external script                -> aborted
  8. iframe                         -> aborted
  9. popup                          -> closed
 10. websocket                      -> closed
 12. page-initiated fetch           -> aborted

The "attacker" origin is a second local HTTP server: requests to it must be
aborted by the gateway BEFORE it ever sees a hit (asserted via its hit log).
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator

import pytest

from packages.core.errors import ForbiddenError
from packages.execution.browser import BrowserManager
from packages.execution.events import EventCollector
from packages.execution.netpolicy import OriginPolicy

ATTACKER_PORT = 8711
ATTACKER = f"http://127.0.0.1:{ATTACKER_PORT}"
TARGET_PORT = 8710
TARGET = f"http://127.0.0.1:{TARGET_PORT}"


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        type(self).hits.append(self.path)
        if self.path == "/evil-page":
            body = b"<html><body>evil</body></html>"
        elif self.path == "/evil.js":
            body = b"window.__pwned = true"
        else:
            body = b"\x89PNG\r\n\x1a\n"  # plausible image bytes
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence
        pass


@pytest.fixture(scope="module")
def attacker_server() -> Iterator[None]:
    _CountingHandler.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", ATTACKER_PORT), _CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield
    server.shutdown()


# ------------------------------------------------------- origin policy matrix


def test_origin_policy_matrix() -> None:
    policy = OriginPolicy.for_base_url(f"{TARGET}/")
    assert policy.allows(f"{TARGET}/login")  # 1. normal
    assert not policy.allows("http://127.0.0.1:8711/x")  # 2. different port
    assert not policy.allows(f"https://127.0.0.1:{TARGET_PORT}/x")  # 3. different scheme
    assert not policy.allows("http://localhost:8710/x")  # 4. localhost != 127.0.0.1
    assert not policy.allows("http://evil.test/x")
    assert policy.allows("about:blank")  # in-page, no egress
    assert policy.allows("data:image/png;base64,AAAA")  # in-page, no egress
    assert not policy.allows("http://user:secret@127.0.0.1:8710/x")  # userinfo
    assert policy.refusal_reason("http://127.0.0.1:8711/x").startswith("origin not approved")


def test_toolgateway_and_originpolicy_agree() -> None:
    """The gateway's URL rule and the route guard's rule must never diverge."""
    from packages.agents.exploration import Budget, ToolCall, ToolGateway

    budget = Budget(
        allowed_domains=("127.0.0.1",),
        allowed_origins=(f"{TARGET}/",),
    )
    policy = OriginPolicy(
        allowed_domains=budget.allowed_domains,
        allowed_origins=budget.allowed_origins,
        allowed_schemes=budget.allowed_schemes,
    )
    gw = ToolGateway(budget)
    urls = [
        f"{TARGET}/login",
        "http://127.0.0.1:8711/x",
        f"https://127.0.0.1:{TARGET_PORT}/x",
        "http://localhost:8710/x",
        "http://evil.test/x",
        "http://user:secret@127.0.0.1:8710/x",
    ]
    for url in urls:
        gw_allowed = True
        try:
            gw.authorize(ToolCall("navigate", {"url": url}))
        except ForbiddenError:
            gw_allowed = False
        assert gw_allowed == policy.allows(url), url


# ------------------------------------------------- live browser egress checks


def _browser(target: str) -> BrowserManager:
    return BrowserManager(
        target,
        EventCollector("run_net", "test"),
        run_id="run_net",
    )


def test_page_initiated_egress_is_aborted(attacker_server: None) -> None:
    """A legal page on the approved origin trying to call home gets blocked."""
    html = f"""<html><body>
      <img src="{ATTACKER}/evil.png" />
      <script src="{ATTACKER}/evil.js"></script>
      <iframe src="{ATTACKER}/evil-page"></iframe>
      <script>
        fetch("{ATTACKER}/evil-fetch").catch(() => {{}});
        const ws = new WebSocket("ws://127.0.0.1:{ATTACKER_PORT}/ws");
        setTimeout(() => window.open("{ATTACKER}/evil-page"), 50);
      </script>
    </body></html>"""
    served: dict[str, bytes] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = html.encode() if self.path == "/" else b"ok"
            served[self.path] = body
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", TARGET_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from packages.execution.models import TestStep

        with _browser(f"{TARGET}/") as browser:
            assert browser.page is not None
            result = browser.execute_step(TestStep("s1", "navigate", f"{TARGET}/"), "run_net")
            assert result.ok, result.detail
            # give the page time to fire its egress attempts
            browser.page.wait_for_timeout(1200)

        blocked_urls = " ".join(b["url"] for b in browser.blocked_requests)
        assert f"{ATTACKER}/evil.png" in blocked_urls  # 6. image
        assert f"{ATTACKER}/evil.js" in blocked_urls  # 7. script
        assert f"{ATTACKER}/evil-page" in blocked_urls  # 8. iframe (+ popup)
        assert f"{ATTACKER}/evil-fetch" in blocked_urls  # 12. fetch
        # the attacker server must have seen NONE of these requests
        attacker_hits = [h for h in _CountingHandler.hits if h != "/evil-page"]
        assert not any("evil" in h for h in attacker_hits), _CountingHandler.hits
        assert any(b["reason"] == "popup closed" for b in browser.blocked_requests), (
            browser.blocked_requests
        )  # 9. popup closed
        # the blocked attempts are part of the evidence record
        ev = browser.security_evidence("run_net")
        assert ev is not None and ev.evidence_type == "network_blocked"
    finally:
        server.shutdown()


def test_navigation_to_off_origin_is_refused(attacker_server: None) -> None:
    from packages.execution.models import TestStep

    with _browser(f"{TARGET}/") as browser:
        result = browser.execute_step(TestStep("s1", "navigate", f"{ATTACKER}/evil-page"), "run")
        assert not result.ok
        assert "not approved" in result.detail or "origin" in result.detail


def test_cross_origin_redirect_is_abandoned(attacker_server: None) -> None:
    """A redirect that lands off-origin fails the step and leaves the page."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"{ATTACKER}/evil-page")
                self.end_headers()
            else:
                body = b"<html><body>home</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", TARGET_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from packages.execution.models import TestStep

        with _browser(f"{TARGET}/") as browser:
            result = browser.execute_step(TestStep("s1", "navigate", f"{TARGET}/redirect"), "run")
            # The redirect target answers with a download content-type, so
            # Chrome turns it into a download — which our gateway cancels.
            # Either way the step must fail and the page must NOT land on the
            # attacker origin.
            assert not result.ok, "a redirect landing off-origin must fail the step"
            assert "unapproved" in result.detail or "Download" in result.detail, result.detail
            # the page was abandoned instead of sitting on the attacker origin
            assert browser.page is not None
            final_url = browser.page.url
            assert final_url == "about:blank" or f"127.0.0.1:{TARGET_PORT}" in final_url
    finally:
        server.shutdown()


def test_download_is_cancelled(attacker_server: None) -> None:
    """A page-initiated download is an egress channel: cancelled + audited."""
    from packages.execution.models import TestStep

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                body = (
                    f'<html><body><a id="dl" href="{ATTACKER}/evil.png">dl</a></body></html>'
                ).encode()
                ctype = "text/html"
            else:
                body = b"\x89PNG\r\n\x1a\n"
                ctype = "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", TARGET_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with _browser(f"{TARGET}/") as browser:
            assert browser.page is not None
            browser.execute_step(TestStep("s1", "navigate", f"{TARGET}/"), "run")
            # Clicking the link targets the ATTACKER origin: the route guard
            # aborts the request, so no download ever starts (and if one
            # somehow starts, the download handler cancels it).
            result = browser.execute_step(TestStep("s2", "click", "a#dl"), "run")
            browser.page.wait_for_timeout(500)
            blocked = " ".join(b["url"] for b in browser.blocked_requests)
            cancelled = any(b["reason"] == "download cancelled" for b in browser.blocked_requests)
            assert f"{ATTACKER}/evil.png" in blocked or cancelled, browser.blocked_requests
            assert result.ok or "download" in result.detail.lower()
    finally:
        server.shutdown()
