"""Single-process, atomic, one-use execution grants bound to one Run/action."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from packages.core.errors import ForbiddenError


def _digest(action: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"action": action, "arguments": arguments},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionGrant:
    grant_id: str
    run_id: str
    action_digest: str
    budget_units: int


@dataclass
class _GrantState:
    grant: ExecutionGrant
    expires_at: float
    consumed: bool = False


class ExecutionAuthorizationManager:
    """O2 process-local lock; not a distributed or multi-worker lock."""

    def __init__(self, *, run_budgets: dict[str, int], max_ttl_s: int = 60) -> None:
        self._lock = threading.Lock()
        self._max_ttl_s = max_ttl_s
        self._budgets = dict(run_budgets)
        self._reserved: dict[str, int] = {}
        self._spent: dict[str, int] = {}
        self._grants: dict[str, _GrantState] = {}
        self._active_runs: set[str] = set()

    def issue(
        self,
        *,
        run_id: str,
        action: str,
        arguments: dict[str, Any],
        budget_units: int = 1,
        ttl_s: int = 30,
    ) -> str:
        if not run_id or budget_units < 1 or not 1 <= ttl_s <= self._max_ttl_s:
            raise ForbiddenError("invalid run, budget reservation or authorization lifetime")
        with self._lock:
            self._expire_locked()
            total = self._budgets.get(run_id)
            if total is None:
                raise ForbiddenError("run is not registered for execution authorization")
            reserved = self._reserved.get(run_id, 0)
            spent = self._spent.get(run_id, 0)
            if spent + reserved + budget_units > total:
                raise ForbiddenError("execution budget cannot be reserved")
            grant_id = secrets.token_urlsafe(32)
            grant = ExecutionGrant(grant_id, run_id, _digest(action, arguments), budget_units)
            self._reserved[run_id] = reserved + budget_units
            self._grants[grant_id] = _GrantState(grant, time.monotonic() + ttl_s)
            return grant_id

    def _expire_locked(self) -> None:
        now = time.monotonic()
        expired = [key for key, state in self._grants.items() if now > state.expires_at]
        for key in expired:
            state = self._grants.pop(key)
            run_id = state.grant.run_id
            self._reserved[run_id] = max(
                0, self._reserved.get(run_id, 0) - state.grant.budget_units
            )

    @contextmanager
    def consume(
        self,
        grant_id: str,
        *,
        run_id: str,
        action: str,
        arguments: dict[str, Any],
        required_budget_units: int = 1,
    ) -> Iterator[ExecutionGrant]:
        with self._lock:
            state = self._grants.get(grant_id)
            if state is None or state.consumed:
                raise ForbiddenError("authorization is missing, expired or already consumed")
            if time.monotonic() > state.expires_at:
                self._reserved[state.grant.run_id] -= state.grant.budget_units
                self._grants.pop(grant_id, None)
                raise ForbiddenError("authorization has expired")
            grant = state.grant
            if grant.run_id != run_id or grant.action_digest != _digest(action, arguments):
                raise ForbiddenError("authorization run or parameters do not match")
            if required_budget_units > grant.budget_units:
                raise ForbiddenError("authorization budget reservation is insufficient")
            if run_id in self._active_runs:
                raise ForbiddenError("another action is already executing for this Run")
            state.consumed = True
            self._reserved[run_id] -= grant.budget_units
            self._spent[run_id] = self._spent.get(run_id, 0) + required_budget_units
            self._active_runs.add(run_id)
        try:
            yield grant
        finally:
            with self._lock:
                self._active_runs.discard(run_id)
                self._grants.pop(grant_id, None)

    def remaining_budget(self, run_id: str) -> int:
        with self._lock:
            return (
                self._budgets.get(run_id, 0)
                - self._spent.get(run_id, 0)
                - self._reserved.get(run_id, 0)
            )
