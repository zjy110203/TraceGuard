"""Sensitive test-data binding: references in, plaintext never persisted.

R5-4.1 problem this module solves
---------------------------------
Explorer redacts a filled password before the trace is stored, so the trace
carries a placeholder instead of a credential. The compiler used to copy that
placeholder into ``TestStep.value`` and the runner typed it into the login form
— a case that could never pass, and worse, a case that silently executed with
the wrong secret.

The contract is now explicit:

  * a step that needs a secret carries ``value_ref`` and an EMPTY ``value``;
  * the plaintext lives in a deployment-managed provider (settings/env), never
    in the case file, the trace, the report, an event or a screenshot;
  * an unresolvable reference BLOCKS the run instead of guessing;
  * the redaction placeholder is never accepted as an input value.

Nothing here reads the network or the database: providers are pure lookups, so
this stays trivially testable and has no startup side effects.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

# The literal placeholder written into a persisted trace when a value is
# masked. It is a *marker*, not data: it must never be typed into a form.
REDACTED_MARKER = "«redacted»"

# A credential can hide in the arg *key* (token, api_key) or in the arg
# *context* — a plain ``value`` filled into a password field.
SENSITIVE_HINT = re.compile(r"pass(word)?|secret|token|api[-_]?key|credential", re.IGNORECASE)

_TESTID = re.compile(r"\[data-testid=[\"']?([^\"'\]]+)[\"']?\]")
_NAME_ATTR = re.compile(r"\[name=[\"']?([^\"'\]]+)[\"']?\]")


class UnresolvedDataRef(Exception):
    """A referenced secret could not be resolved. The run must not proceed."""

    def __init__(self, ref_id: str, reason: str = "not found in any provider") -> None:
        super().__init__(f"unresolved test data ref {ref_id!r}: {reason}")
        self.ref_id = ref_id
        self.reason = reason


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-") or "field"


def derive_value_ref(selector: str) -> str:
    """Derive a stable ref id from the selector a value was typed into.

    ``[data-testid=password]`` -> ``teamflow.password``. The namespace is the
    target application, so one provider can serve several suites without
    collisions. A selector with no testid/name still yields a deterministic id.
    """
    match = _TESTID.search(selector) or _NAME_ATTR.search(selector)
    field = _slug(match.group(1)) if match else _slug(selector.split(">")[-1])[:24]
    return f"teamflow.{field}"


def redacted_ref(rationale_source: Mapping[str, object]) -> str:
    """Ref id for an action arg masked before persistence (used by tracing)."""
    selector = str(rationale_source.get("selector") or rationale_source.get("target") or "")
    return derive_value_ref(selector)


class TestDataProvider(Protocol):
    """Resolves a ``value_ref`` to its deployment-managed plaintext."""

    @property
    def name(self) -> str: ...

    def resolve(self, ref_id: str) -> str | None: ...


@dataclass(frozen=True)
class MappingTestDataProvider:
    """Explicit mapping — the shape every other provider reduces to."""

    values: Mapping[str, str]
    name: str = "mapping"

    def resolve(self, ref_id: str) -> str | None:
        value = self.values.get(ref_id)
        return value or None


@dataclass(frozen=True)
class NullTestDataProvider:
    """Resolves nothing. The safe default: an unbound ref blocks the run."""

    name: str = "null"

    def resolve(self, ref_id: str) -> str | None:  # noqa: ARG002 - protocol shape
        return None


@dataclass(frozen=True)
class EnvTestDataProvider:
    """Reads ``TG_TEST_DATA_<REF_ID>`` from the process environment.

    ``teamflow.password`` -> ``TG_TEST_DATA_TEAMFLOW_PASSWORD``. Keeps secrets
    out of files entirely and out of every persisted artifact.
    """

    prefix: str = "TG_TEST_DATA_"
    name: str = "env"

    def resolve(self, ref_id: str) -> str | None:
        key = self.prefix + ref_id.upper().replace(".", "_").replace("-", "_")
        value = os.environ.get(key)
        return value or None


@dataclass(frozen=True)
class ChainTestDataProvider:
    """First provider that resolves wins (settings -> env -> file)."""

    providers: tuple[TestDataProvider, ...] = ()
    name: str = "chain"

    def resolve(self, ref_id: str) -> str | None:
        for provider in self.providers:
            value = provider.resolve(ref_id)
            if value:
                return value
        return None


def build_provider(
    *, settings_values: Mapping[str, str] | None = None, extra: Iterable[TestDataProvider] = ()
) -> TestDataProvider:
    """Provider used by the product: settings first, then env, then extras."""
    providers: list[TestDataProvider] = []
    if settings_values:
        providers.append(MappingTestDataProvider(dict(settings_values), name="settings"))
    providers.append(EnvTestDataProvider())
    providers.extend(extra)
    return ChainTestDataProvider(tuple(providers))


@dataclass(frozen=True)
class ResolvedValue:
    """Outcome of binding a step: either a usable value or a hard block."""

    ok: bool
    value: str = ""
    ref_id: str = ""
    reason: str = ""


def is_placeholder(value: str) -> bool:
    """True when a value is the redaction marker (or an empty marker-ish echo)."""
    stripped = value.strip()
    return stripped == REDACTED_MARKER or stripped == "«redacted»" or stripped == "[REDACTED]"


def resolve_step_value(
    *,
    step_id: str,
    value: str,
    value_ref: str,
    provider: TestDataProvider,
) -> ResolvedValue:
    """Bind one step's input, refusing placeholders and unresolved refs."""
    if value_ref:
        if value:
            return ResolvedValue(
                ok=False,
                ref_id=value_ref,
                reason=f"{step_id}: carries both value_ref {value_ref!r} and plaintext value",
            )
        resolved = provider.resolve(value_ref)
        if not resolved:
            return ResolvedValue(
                ok=False,
                ref_id=value_ref,
                reason=(
                    f"{step_id}: value_ref {value_ref!r} unresolved by provider {provider.name!r}"
                ),
            )
        return ResolvedValue(ok=True, value=resolved, ref_id=value_ref)
    if is_placeholder(value):
        return ResolvedValue(
            ok=False,
            reason=(
                f"{step_id}: input is the redaction placeholder {REDACTED_MARKER!r}; "
                "it is not a real value and must be bound to a value_ref"
            ),
        )
    return ResolvedValue(ok=True, value=value)
