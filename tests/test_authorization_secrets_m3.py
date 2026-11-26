from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from packages.agents.authorization import ExecutionAuthorizationManager
from packages.agents.secret_resolver import SecretResolver
from packages.core.errors import ForbiddenError


def test_execution_grant_is_single_use_and_parameter_bound() -> None:
    manager = ExecutionAuthorizationManager(run_budgets={"run-1": 2})
    token = manager.issue(run_id="run-1", action="click", arguments={"selector": "#save"})
    with pytest.raises(ForbiddenError, match="parameters"):
        with manager.consume(
            token, run_id="run-1", action="click", arguments={"selector": "#delete"}
        ):
            pass
    with manager.consume(token, run_id="run-1", action="click", arguments={"selector": "#save"}):
        assert manager.remaining_budget("run-1") == 1
    with pytest.raises(ForbiddenError, match="consumed"):
        with manager.consume(
            token, run_id="run-1", action="click", arguments={"selector": "#save"}
        ):
            pass


def test_execution_grant_binds_run_reserves_budget_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ExecutionAuthorizationManager(run_budgets={"run-1": 1}, max_ttl_s=2)
    with pytest.raises(ForbiddenError, match="not registered"):
        manager.issue(run_id="run-2", action="snapshot", arguments={}, ttl_s=1)
    with pytest.raises(ForbiddenError, match="budget"):
        manager.issue(run_id="run-1", action="snapshot", arguments={}, budget_units=2)
    token = manager.issue(run_id="run-1", action="snapshot", arguments={}, ttl_s=1)
    state = manager._grants[token]
    state.expires_at = time.monotonic() - 1
    with pytest.raises(ForbiddenError, match="expired"):
        with manager.consume(token, run_id="run-1", action="snapshot", arguments={}):
            pass


def test_execution_grants_are_atomic_and_same_run_is_serialized() -> None:
    manager = ExecutionAuthorizationManager(run_budgets={"run-1": 3})
    token = manager.issue(run_id="run-1", action="snapshot", arguments={})
    entered = threading.Event()
    release = threading.Event()

    def consume_once() -> str:
        try:
            with manager.consume(token, run_id="run-1", action="snapshot", arguments={}):
                entered.set()
                release.wait(3)
            return "ok"
        except ForbiddenError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(consume_once)
        assert entered.wait(2)
        second = pool.submit(consume_once)
        assert second.result(timeout=2) == "rejected"
        release.set()
        assert first.result(timeout=3) == "ok"

    first_token = manager.issue(run_id="run-1", action="snapshot", arguments={})
    second_token = manager.issue(run_id="run-1", action="snapshot", arguments={})
    with manager.consume(first_token, run_id="run-1", action="snapshot", arguments={}):
        with pytest.raises(ForbiddenError, match="already executing"):
            with manager.consume(second_token, run_id="run-1", action="snapshot", arguments={}):
                pass


def test_secret_resolver_is_ledger_scoped_server_only_and_never_echoes_missing() -> None:
    resolver = SecretResolver(
        approved_refs={"teamflow.password": "TG_SECRET_TEAMFLOW_PASSWORD"},
        environ={"TG_SECRET_TEAMFLOW_PASSWORD": "test-secret-value"},
    )
    assert (
        resolver.resolve("teamflow.password", principal="server", permission=True)
        == "test-secret-value"
    )
    for principal, permission, ref in [
        ("agent", True, "teamflow.password"),
        ("server", False, "teamflow.password"),
        ("server", True, "../../password"),
        ("server", True, "unregistered.secret"),
    ]:
        with pytest.raises(ForbiddenError):
            resolver.resolve(ref, principal=principal, permission=permission)
    missing = SecretResolver(
        approved_refs={"teamflow.password": "TG_SECRET_TEAMFLOW_PASSWORD"}, environ={}
    )
    with pytest.raises(ForbiddenError, match="missing") as exc:
        missing.resolve("teamflow.password", principal="server", permission=True)
    assert "test-secret-value" not in str(exc.value)
