"""Shared local self-approval guard for Candidate and Repair review routes.

This is an anti-mistake rule only. Free-text reviewer fields are not identity
authentication and do not make approvals trustworthy.
"""

from __future__ import annotations

SELF_APPROVER_IDENTIFIERS = frozenset(
    {
        "",
        "agent",
        "mock",
        "mock-agent",
        "mock_agent",
        "mock agent",
        "ai",
        "system",
        "auto",
        "traceguard",
        "anonymous",
        "unknown",
        "bot",
        "automation",
        "automated",
        "system-agent",
        "traceguard-agent",
    }
)


def is_self_approver(operator: str) -> bool:
    """Return whether a free-text operator is a known non-human/system label."""
    normalized = " ".join(str(operator).strip().casefold().split())
    return normalized in SELF_APPROVER_IDENTIFIERS
