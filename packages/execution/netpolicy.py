"""Shared origin policy for every browser egress check (R5-4.7 §4.1).

One implementation, two consumers: the Explorer's PlaywrightAdapter and the
formal execution's BrowserManager. Before this module the two guards were
written independently — the Explorer checked scheme+host, the formal runner
checked nothing at the network layer — so "which origins may this browser
touch" had two different answers depending on the code path.

The permission is ALWAYS an exact ``scheme://host:port`` origin (default ports
made explicit), optionally narrowed by a hostname whitelist. The checks cover
every egress channel a page can use:

  * navigation + redirect targets (final URL, not just the requested one);
  * subresources (img / script / css / fetch / XHR) via context routing;
  * iframes (routed like any other request);
  * popups (closed before they can load anything);
  * downloads (cancelled);
  * websockets (routed / closed).

In-page schemes (``data:`` ``blob:`` ``about:``) make no network egress and are
always allowed. Userinfo in a URL (``https://user:pass@host``) is refused: it
is never a legitimate test target and hides the real host from naive checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

IN_PAGE_SCHEMES = frozenset({"data", "blob", "about"})


def normalize_origin(url: str) -> str:
    """Canonical ``scheme://host:port`` with default ports made explicit.

    ``http://127.0.0.1:8001/x`` and ``http://127.0.0.1/x`` are DIFFERENT
    origins; ``http://localhost:8001`` and ``http://127.0.0.1:8001`` are
    different origins too — localhost is a different host from the loopback
    address as far as a permission is concerned.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}"


@dataclass(frozen=True)
class OriginPolicy:
    """The network permission one browser session is granted. Immutable."""

    allowed_domains: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    allowed_schemes: tuple[str, ...] = ("http", "https")

    def __post_init__(self) -> None:
        if not self.allowed_domains:
            raise ValueError("at least one allowed domain is required")
        if not self.allowed_schemes:
            raise ValueError("at least one allowed scheme is required")
        object.__setattr__(
            self,
            "allowed_origins",
            tuple(normalize_origin(o) for o in self.allowed_origins),
        )
        object.__setattr__(
            self,
            "allowed_domains",
            tuple(d.lower() for d in self.allowed_domains),
        )

    @classmethod
    def for_base_url(
        cls,
        base_url: str,
        *,
        extra_domains: tuple[str, ...] = (),
    ) -> OriginPolicy:
        """The default product permission: exactly the target's origin."""
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        domains = {host, "127.0.0.1", "localhost", *extra_domains} - {""}
        return cls(
            allowed_domains=tuple(sorted(domains)),
            allowed_origins=(normalize_origin(base_url),),
        )

    def allows(self, url: str) -> bool:
        """True when this URL may touch the network. No page input involved."""
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme in IN_PAGE_SCHEMES:
            return True  # no network egress
        if scheme not in self.allowed_schemes:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = (parsed.hostname or "").lower()
        if host not in self.allowed_domains:
            return False
        if self.allowed_origins and normalize_origin(url) not in set(self.allowed_origins):
            # Same host, different port or scheme: a different origin, and
            # this session's permission does not cover it.
            return False
        return True

    def refusal_reason(self, url: str) -> str:
        """Why ``allows`` said no — for security events and tests."""
        if self.allows(url):
            return ""
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if parsed.username is not None or parsed.password is not None:
            return f"userinfo in url is not permitted: {url[:200]}"
        if scheme in IN_PAGE_SCHEMES:
            return ""
        if scheme not in self.allowed_schemes:
            return f"scheme not allowed: {scheme or '<empty>'} ({url[:200]})"
        host = (parsed.hostname or "").lower()
        if host not in self.allowed_domains:
            return f"domain not approved: {url[:200]}"
        return (
            f"origin not approved: {normalize_origin(url)} "
            f"(allowed: {sorted(set(self.allowed_origins))})"
        )
