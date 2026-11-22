"""SSRF / DNS-rebinding / redirect guard for outbound HTTP (R4-3).

The platform makes outbound calls to exactly two kinds of hosts: the local
TeamFlow target and the configured LLM provider. Neither URL comes from page
content, but defence in depth still applies: any future feature that accepts a
URL from a request body, a page, or an LLM response MUST go through this guard
before an HTTP client touches it.

Design:
* scheme allowlist (http/https only — no file:, gopher:, ftp:);
* host denylist: loopback, private ranges, link-local, unspecified, and
  IPv4-mapped IPv6 — configurable for the local-dev case where 127.0.0.1 IS
  the intended target;
* every resolved address (A/AAAA) is checked, not just the first — a mixed
  answer with one private IP is rejected;
* the resolver is injectable: tests simulate DNS rebinding without touching
  real DNS;
* ``validate_redirect`` re-checks each hop target so a redirect cannot smuggle
  the client to a forbidden host.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from packages.core.errors import ValidationError

_ALLOWED_SCHEMES = {"http", "https"}


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    loops = (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    )
    if loops:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_forbidden_ip(ip.ipv4_mapped)
    return False


@dataclass(frozen=True)
class GuardPolicy:
    allow_private: bool = False  # local dev flips this for 127.0.0.1 targets


class UrlGuard:
    """Validate an outbound URL against the SSRF policy."""

    def __init__(
        self,
        policy: GuardPolicy | None = None,
        *,
        resolver: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.policy = policy or GuardPolicy()
        # Default resolver performs real DNS; tests inject a fake one.
        self._resolver = resolver or (lambda host: self._system_resolve(host))

    @staticmethod
    def _system_resolve(host: str) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise ValidationError(f"cannot resolve host: {host}") from exc
        return [str(info[4][0]) for info in infos]

    def validate(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in _ALLOWED_SCHEMES:
            raise ValidationError(f"forbidden URL scheme: {parts.scheme or '<empty>'}")
        if not parts.hostname:
            raise ValidationError("URL has no host")
        if parts.username is not None or parts.password is not None:
            raise ValidationError("userinfo in URL is not allowed")
        for ip_text in self._resolver(parts.hostname):
            ip = ipaddress.ip_address(ip_text)
            if _is_forbidden_ip(ip) and not (self.policy.allow_private and ip.is_loopback):
                raise ValidationError(
                    f"URL resolves to a forbidden address: {ip} (host={parts.hostname})"
                )
        return url

    def validate_redirect(self, from_url: str, to_url: str) -> str:
        """Every redirect hop must itself pass validation (same policy)."""
        return self.validate(to_url)
