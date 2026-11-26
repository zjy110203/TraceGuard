from __future__ import annotations

import json
import threading

import pytest

from packages.agents.data_lifecycle import DataLeaseManager


def test_lease_isolated_and_cleanup_is_idempotent(tmp_path) -> None:
    manager = DataLeaseManager(tmp_path)
    first = manager.setup("run-one", {"seed": "one"}, now=100)
    second = manager.setup("run-two", {"seed": "two"}, now=100)

    assert json.loads((tmp_path / "run-one" / "seed.json").read_text()) == {"seed": "one"}
    assert manager.cleanup("run-one", first, now=101)
    assert not (tmp_path / "run-one").exists()
    assert (tmp_path / "run-two" / "lease.json").exists()
    assert manager.cleanup("run-one", first, now=102)
    assert manager.cleanup("run-two", second, now=102)


def test_active_run_is_not_orphaned_and_stale_run_recovers(tmp_path) -> None:
    manager = DataLeaseManager(tmp_path)
    active = manager.setup("active-run", now=100)
    manager.setup("stale-run", now=100)
    manager.heartbeat("active-run", active, now=150)

    assert manager.expired_runs(now=159) == ()
    assert manager.expired_runs(now=160.01) == ("stale-run",)
    assert manager.recover_orphans(now=160.01) == ("stale-run",)
    assert (tmp_path / "active-run" / "lease.json").exists()
    assert not (tmp_path / "stale-run").exists()


def test_cleanup_failed_is_retained_and_retried_idempotently(tmp_path, monkeypatch) -> None:
    manager = DataLeaseManager(tmp_path)
    token = manager.setup("retry-run", now=100)
    manager.add_resource("retry-run", token, {"kind": "teamflow-project", "id": "p-1"})
    manager.mark_cleanup_failed("retry-run", token, "database unavailable")
    assert manager.expired_runs(now=101) == ("retry-run",)
    assert manager.recover_orphans(now=101) == ("retry-run",)
    assert not (tmp_path / "retry-run").exists()


def test_cleanup_rejects_foreign_token_without_touching_run(tmp_path) -> None:
    manager = DataLeaseManager(tmp_path)
    token = manager.setup("protected-run", {"data": [1]})
    with pytest.raises(ValueError, match="token mismatch"):
        manager.cleanup("protected-run", "wrong-token")
    assert (tmp_path / "protected-run" / "seed.json").exists()
    assert manager.cleanup("protected-run", token)


def test_concurrent_run_leases_never_cross_cleanup(tmp_path) -> None:
    manager = DataLeaseManager(tmp_path)
    leases = {f"run-{i}": manager.setup(f"run-{i}") for i in range(8)}
    errors: list[Exception] = []

    def clean(run_id: str, token: str) -> None:
        try:
            assert manager.cleanup(run_id, token)
        except Exception as exc:  # pragma: no cover - diagnostic on thread failure
            errors.append(exc)

    threads = [threading.Thread(target=clean, args=item) for item in leases.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("heartbeat", "timeout"), [(0, 60), (21, 60), (20, 61), (20, 20)])
def test_lease_parameters_are_bounded(heartbeat: int, timeout: int, tmp_path) -> None:
    with pytest.raises(ValueError):
        DataLeaseManager(tmp_path, heartbeat_seconds=heartbeat, timeout_seconds=timeout)
