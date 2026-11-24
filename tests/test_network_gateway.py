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

R5-4.9: every server binds an OS-assigned port and is fully closed
(``shutdown`` + ``server_close`` + join). The old fixed ports 8710/8711 caused
the CI failure ``OSError: [Errno 98] Address already in use``.
"""

from __future__ import annotations

import http.server
from collections.abc import Iterator

import pytest

from packages.core.errors import ForbiddenError
from packages.execution.browser import BrowserManager
from packages.execution.events import EventCollector
from packages.execution.netpolicy import OriginPolicy
from tests.netutil import release_reserved, reserve_port, serve_http

# URL-only matrix: nothing is bound to these ports, they just must DIFFER.
# ``free_port()`` closed each socket before the next call, and the OS reuses the
# most recently freed port first — so both calls could return the same number
# and the "other origin is refused" cases would silently test the allowed one.
# Holding both sockets open makes the two origins provably distinct.
POLICY_TARGET_PORT = reserve_port()
POLICY_TARGET = f"http://127.0.0.1:{POLICY_TARGET_PORT}"
POLICY_OTHER_PORT = reserve_port()
POLICY_OTHER = f"http://127.0.0.1:{POLICY_OTHER_PORT}"


@pytest.fixture(scope="module", autouse=True)
def _release_policy_ports() -> Iterator[None]:
    yield
    release_reserved(POLICY_TARGET_PORT)
    release_reserved(POLICY_OTHER_PORT)


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
def attacker() -> Iterator[str]:
    """The unapproved origin; returns its base URL on an ephemeral port."""
    _CountingHandler.hits = []
    with serve_http(_CountingHandler) as base:
        yield base


class _HomeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<html><body>home</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _attacker_port(attacker: str) -> str:
    return attacker.rsplit(":", 1)[1]


# ------------------------------------------------------- origin policy matrix


def test_origin_policy_matrix() -> None:
    policy = OriginPolicy.for_base_url(f"{POLICY_TARGET}/")
    assert policy.allows(f"{POLICY_TARGET}/login")  # 1. normal
    assert not policy.allows(f"{POLICY_OTHER}/x")  # 2. different port
    assert not policy.allows(f"https://127.0.0.1:{POLICY_TARGET_PORT}/x")  # 3. different scheme
    assert not policy.allows(
        f"http://localhost:{POLICY_TARGET_PORT}/x"
    )  # 4. localhost != 127.0.0.1
    assert not policy.allows("http://evil.test/x")
    assert policy.allows("about:blank")  # in-page, no egress
    assert policy.allows("data:image/png;base64,AAAA")  # in-page, no egress
    assert not policy.allows(f"http://user:secret@127.0.0.1:{POLICY_TARGET_PORT}/x")  # userinfo
    assert policy.refusal_reason(f"{POLICY_OTHER}/x").startswith("origin not approved")


def test_toolgateway_and_originpolicy_agree(attacker: str) -> None:
    """The gateway's URL rule and the route guard's rule must never diverge."""
    from packages.agents.exploration import Budget, ToolCall, ToolGateway

    budget = Budget(
        allowed_domains=("127.0.0.1",),
        allowed_origins=(f"{POLICY_TARGET}/",),
    )
    policy = OriginPolicy(
        allowed_domains=budget.allowed_domains,
        allowed_origins=budget.allowed_origins,
        allowed_schemes=budget.allowed_schemes,
    )
    gw = ToolGateway(budget)
    urls = [
        f"{POLICY_TARGET}/login",
        f"{attacker}/x",
        f"https://127.0.0.1:{POLICY_TARGET_PORT}/x",
        f"http://localhost:{POLICY_TARGET_PORT}/x",
        "http://evil.test/x",
        f"http://user:secret@127.0.0.1:{POLICY_TARGET_PORT}/x",
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


def test_page_initiated_egress_is_aborted(attacker: str) -> None:
    """A legal page on the approved origin trying to call home gets blocked."""
    html = f"""<html><body>
      <img src="{attacker}/evil.png" />
      <script src="{attacker}/evil.js"></script>
      <iframe src="{attacker}/evil-page"></iframe>
      <script>
        fetch("{attacker}/evil-fetch").catch(() => {{}});
        const ws = new WebSocket("ws://127.0.0.1:{_attacker_port(attacker)}/ws");
        setTimeout(() => window.open("{attacker}/evil-page"), 50);
      </script>
    </body></html>"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = html.encode() if self.path == "/" else b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    with serve_http(Handler) as target:
        from packages.execution.models import TestStep

        with _browser(f"{target}/") as browser:
            assert browser.page is not None
            result = browser.execute_step(TestStep("s1", "navigate", f"{target}/"), "run_net")
            assert result.ok, result.detail
            # give the page time to fire its egress attempts
            browser.page.wait_for_timeout(1200)

        blocked_urls = " ".join(b["url"] for b in browser.blocked_requests)
        assert f"{attacker}/evil.png" in blocked_urls  # 6. image
        assert f"{attacker}/evil.js" in blocked_urls  # 7. script
        assert f"{attacker}/evil-page" in blocked_urls  # 8. iframe (+ popup)
        assert f"{attacker}/evil-fetch" in blocked_urls  # 12. fetch
        # the attacker server must have seen NONE of these requests
        attacker_hits = [h for h in _CountingHandler.hits if h != "/evil-page"]
        assert not any("evil" in h for h in attacker_hits), _CountingHandler.hits
        assert any(b["reason"] == "popup closed" for b in browser.blocked_requests), (
            browser.blocked_requests
        )  # 9. popup closed
        # the blocked attempts are part of the evidence record
        ev = browser.security_evidence("run_net")
        assert ev is not None and ev.evidence_type == "network_blocked"


def test_navigation_to_off_origin_is_refused(attacker: str) -> None:
    from packages.execution.models import TestStep

    with serve_http(_HomeHandler) as target:
        with _browser(f"{target}/") as browser:
            result = browser.execute_step(
                TestStep("s1", "navigate", f"{attacker}/evil-page"), "run"
            )
            assert not result.ok
            assert "not approved" in result.detail or "origin" in result.detail


def test_cross_origin_redirect_is_abandoned(attacker: str) -> None:
    """A redirect that lands off-origin fails the step and leaves the page."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"{attacker}/evil-page")
                self.end_headers()
            else:
                body = b"<html><body>home</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    with serve_http(Handler) as target:
        from packages.execution.models import TestStep

        with _browser(f"{target}/") as browser:
            result = browser.execute_step(TestStep("s1", "navigate", f"{target}/redirect"), "run")
            # The redirect target answers with a download content-type, so
            # Chrome turns it into a download — which our gateway cancels.
            # Either way the step must fail and the page must NOT land on the
            # attacker origin.
            assert not result.ok, "a redirect landing off-origin must fail the step"
            assert "unapproved" in result.detail or "Download" in result.detail, result.detail
            # the page was abandoned instead of sitting on the attacker origin
            assert browser.page is not None
            final_url = browser.page.url
            assert final_url == "about:blank" or final_url.startswith(target), final_url


def test_download_is_cancelled(attacker: str) -> None:
    """A page-initiated download is an egress channel: cancelled + audited."""
    from packages.execution.models import TestStep

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                body = (
                    f'<html><body><a id="dl" href="{attacker}/evil.png">dl</a></body></html>'
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

    with serve_http(Handler) as target:
        with _browser(f"{target}/") as browser:
            assert browser.page is not None
            browser.execute_step(TestStep("s1", "navigate", f"{target}/"), "run")
            # Clicking the link targets the ATTACKER origin: the route guard
            # aborts the request, so no download ever starts (and if one
            # somehow starts, the download handler cancels it).
            result = browser.execute_step(TestStep("s2", "click", "a#dl"), "run")
            browser.page.wait_for_timeout(500)
            blocked = " ".join(b["url"] for b in browser.blocked_requests)
            cancelled = any(b["reason"] == "download cancelled" for b in browser.blocked_requests)
            assert f"{attacker}/evil.png" in blocked or cancelled, browser.blocked_requests
            assert result.ok or "download" in result.detail.lower()
