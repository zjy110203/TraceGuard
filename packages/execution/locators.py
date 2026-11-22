"""Locator management for stage 2 (roadmap §5, stage 2 task 13).

The v1.1 roadmap fixes a locator priority order that a Compiler/Explorer should
follow.  This module makes that order an explicit, testable policy instead of a
convention scattered across test cases:

    role (with accessible name) -> label -> placeholder -> test id
    -> verified CSS -> limited XPath fallback

A test step may reference a *logical* locator (``loc:<name>``).  The runner
resolves it through a :class:`LocatorRegistry` to a concrete Playwright
selector.  Raw CSS selectors keep working unchanged, so existing cases remain
valid.  XPath is registered last and is never chosen while a higher-priority
candidate exists — it is a limited fallback, not a default.

No Agent may grant itself new locator permissions here: the registry is a
static, reviewable artifact loaded from ``benchmark/cases/locators.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Lower number = higher priority. Mirrors the frozen roadmap order.
PRIORITY: dict[str, int] = {
    "role": 10,
    "label": 20,
    "placeholder": 30,
    "testid": 40,
    "css": 50,
    "xpath": 60,
}

LOGICAL_PREFIX = "loc:"


@dataclass(frozen=True)
class LocatorCandidate:
    """One concrete way to find an element, with its strategy and priority."""

    strategy: str
    value: str
    name: str = ""

    def __post_init__(self) -> None:
        if self.strategy not in PRIORITY:
            raise ValueError(f"unknown locator strategy: {self.strategy}")

    @property
    def priority(self) -> int:
        return PRIORITY[self.strategy]

    def to_selector(self) -> str:
        """Convert to a Playwright selector string."""
        if self.strategy == "role":
            # role=button[name="Login"] uses Playwright's role engine; an
            # accessible name makes the selector resilient to markup changes.
            return f'role={self.value}[name="{self.name}"]' if self.name else f"role={self.value}"
        if self.strategy == "label":
            # Playwright CSS extension: match the <label> text, then its input.
            # The bare ``label="..."`` selector engine is not available in all
            # Playwright builds (verified against 1.65.2), so use CSS instead.
            return f'label:has-text("{self.value}") input, label:has-text("{self.value}") textarea'
        if self.strategy == "placeholder":
            # ``placeholder=`` is not a registered selector engine in Playwright
            # 1.65.2; the equivalent CSS attribute selector is portable.
            return f'[placeholder="{self.value}"]'
        if self.strategy == "testid":
            return f'[data-testid="{self.value}"]'
        if self.strategy == "xpath":
            return f"xpath={self.value}"
        return self.value  # verified CSS

    def to_dict(self) -> dict[str, Any]:
        return {"strategy": self.strategy, "value": self.value, "name": self.name}


class LocatorRegistry:
    """Immutable map of logical locator name -> ordered candidates."""

    def __init__(self, entries: dict[str, tuple[LocatorCandidate, ...]]) -> None:
        self._entries = dict(entries)

    @classmethod
    def from_file(cls, path: Path) -> LocatorRegistry:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries: dict[str, tuple[LocatorCandidate, ...]] = {}
        for name, candidates in raw.items():
            parsed = tuple(
                LocatorCandidate(strategy=c["strategy"], value=c["value"], name=c.get("name", ""))
                for c in candidates
            )
            if not parsed:
                raise ValueError(f"locator '{name}' has no candidates")
            entries[name] = parsed
        return cls(entries)

    def resolve(self, name: str) -> LocatorCandidate:
        """Return the highest-priority registered candidate for ``name``."""
        candidates = self._entries.get(name)
        if not candidates:
            raise KeyError(f"unknown logical locator: {name}")
        return min(candidates, key=lambda c: c.priority)

    def selector(self, name: str) -> str:
        return self.resolve(name).to_selector()

    def resolve_target(self, target: str) -> str:
        """Resolve ``loc:<name>``; return raw selectors unchanged."""
        if not target.startswith(LOGICAL_PREFIX):
            return target
        return self.selector(target[len(LOGICAL_PREFIX) :])

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def to_dict(self) -> dict[str, Any]:
        return {
            name: [c.to_dict() for c in candidates]
            for name, candidates in sorted(self._entries.items())
        }
