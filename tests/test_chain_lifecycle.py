"""R5-4.2 acceptance: one Run lifecycle, observable and interruptible.

Before this round the full chain executed first and registered a Run afterwards:
the id was invented after the fact, nothing could be watched while it ran, and a
cancel had nothing to cancel. These tests pin the new contract with a chain that
blocks on purpose, so the Run row can be inspected mid-flight.

The browser itself is not involved here — execution semantics are covered by
``test_oracle_semantics.py`` and ``test_full_chain.py``. What is under test is
the *lifecycle*: ordering, one shared id, live stage progress, cancellation and
retry.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import build_app
from packages.core.config import Settings
from packages.execution.pipeline import ChainStage, FullChainReport

CASE: dict[str, Any] = {
    "case_id": "TC-LIFE-001",
    "version": 1,
    "title": "lifecycle-only case",
    "intent": {"intent_id": "i-life", "description": "d", "source_ref": ""},
    "steps": [{"step_id": "s1", "action": "navigate", "target": "/", "value": "", "expected": ""}],
    "assertions": [
        {"assertion_id": "a1", "description": "a", "required": True, "source_ref": "", "notes": []}
    ],
    "oracle_bindings": [
        {
            "assertion_id": "a1",
            "primary": {
                "oracle_id": "o1",
                "source_type": "ui",
                "source_ref": "ui.s1.passed",
                "expected": "PASSED",
                "version": "1.0.0",
                "source": "test",
                "approved_by": "human",
                "approved": True,
                "expected_value": True,
            },
            "supporting": [],
            "adjudication_rule": "frozen_fallback_inconclusive_on_conflict",
        }
    ],
    "preconditions": [],
    "postconditions": [],
    "data_refs": [],
    "cleanup": {"strategy": "none", "reset_endpoint": ""},
    "compiled_from": "manual",
    "approved_by": "human",
}


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    case_dir = tmp_path / "cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "TC-LIFE-001.json").write_text(json.dumps(CASE), encoding="utf-8")
    settings = Settings(
        app_env="test",
        # File-backed on purpose: the test reads the run row from another thread
        # while the chain request is still in flight.
        database_url=f"sqlite:///{(tmp_path / 'tg.db').as_posix()}",
        case_dir=str(case_dir),
        exploration_data_dir=str(tmp_path / "explorations"),
        benchmark_data_dir=str(tmp_path / "benchmark"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
        candidate_data_dir=str(tmp_path / "candidates"),
    )
    with TestClient(build_app(settings)) as c:
        yield c


class BlockingChain:
    """A chain that reports progress, then waits to be cancelled or released."""

    started = threading.Event()
    release = threading.Event()

    def __init__(self, **_kwargs: Any) -> None:
        self.progress = _kwargs.get("progress")

    def run(
        self,
        scenario_id: str,
        case: Any,
        *,
        run_id_override: str | None = None,
        cancel_check: Any = None,
        **_kwargs: Any,
    ) -> FullChainReport:
        report = FullChainReport(
            scenario_id=scenario_id, chain_id=f"chain_{run_id_override or 'life'}"
        )

        for name in ("testcase", "execution"):
            if self.progress:
                self.progress(name, "OK", f"running {name}")
        self.started.set()
        assert self.release.wait(15), "test did not release the blocked chain"
        cancelled = bool(cancel_check and cancel_check())
        report.cancelled = cancelled
        report.completed = not cancelled
        report.execution = {
            "run_id": run_id_override,
            "execution_status": "CANCELLED" if cancelled else "COMPLETED",
            "test_verdict": "NOT_EVALUATED" if cancelled else "PASSED",
        }
        if cancelled:
            report.stages.append(ChainStage("cancel", "CANCELLED", "cancel requested"))
        return report


@pytest.fixture()
def blocking_chain(monkeypatch: pytest.MonkeyPatch) -> type[BlockingChain]:
    BlockingChain.started.clear()
    BlockingChain.release.clear()
    monkeypatch.setattr("packages.execution.pipeline.FullChain", BlockingChain)
    return BlockingChain


def _runs(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    return client.get(f"/api/v1/tasks/{task_id}/runs").json()


def test_run_exists_before_execution_and_reports_live_progress(
    client: TestClient, blocking_chain: type[BlockingChain]
) -> None:
    result: dict[str, Any] = {}

    def call() -> None:
        result["resp"] = client.post("/api/v1/chains", json={"case_id": "TC-LIFE-001"})

    thread = threading.Thread(target=call)
    thread.start()
    assert blocking_chain.started.wait(15), "chain never started"

    # The Run row already exists and is RUNNING while the chain is blocked.
    runs = _runs(client, "task_TC-LIFE-001")
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "RUNNING"
    assert run["created_at"] and run["started_at"]

    # Stage progress is readable from the run itself, not just from SSE.
    partial = client.get(f"/api/v1/runs/{run['run_id']}/report").json()
    stages = [s["stage"] for s in partial.get("stages", [])]
    assert "testcase" in stages and "execution" in stages

    blocking_chain.release.set()
    thread.join(timeout=15)
    body = result["resp"].json()
    # One id everywhere: response, report, run row.
    assert body["run_id"] == run["run_id"]
    assert body["chain_id"]
    stored = client.get(f"/api/v1/chains/{body['chain_id']}").json()
    assert stored["execution"]["run_id"] == run["run_id"]
    assert stored["run_status"] == "COMPLETED"


def test_cancel_mid_flight_ends_in_cancelled_not_passed(
    client: TestClient, blocking_chain: type[BlockingChain]
) -> None:
    result: dict[str, Any] = {}

    def call() -> None:
        result["resp"] = client.post("/api/v1/chains", json={"case_id": "TC-LIFE-001"})

    thread = threading.Thread(target=call)
    thread.start()
    assert blocking_chain.started.wait(15)
    run_id = _runs(client, "task_TC-LIFE-001")[0]["run_id"]

    cancel = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancel_requested"] is True

    blocking_chain.release.set()
    thread.join(timeout=15)

    body = result["resp"].json()
    assert body["cancelled"] is True
    assert body["test_verdict"] != "PASSED"
    assert body["run_status"] == "CANCELLED"
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "CANCELLED"


def test_retry_creates_a_new_run_and_never_overwrites(
    client: TestClient, blocking_chain: type[BlockingChain]
) -> None:
    blocking_chain.release.set()  # do not block for this test
    first = client.post("/api/v1/chains", json={"case_id": "TC-LIFE-001"}).json()
    second = client.post("/api/v1/chains", json={"case_id": "TC-LIFE-001"}).json()

    assert first["run_id"] != second["run_id"]
    runs = _runs(client, "task_TC-LIFE-001")
    assert [r["attempt"] for r in runs] == [1, 2]
    # The earlier run keeps its own report; a retry never rewrites history.
    assert len(client.get("/api/v1/chains").json()) == 2
    assert client.get(f"/api/v1/runs/{first['run_id']}").json()["status"] == "COMPLETED"


def test_stage_failure_is_not_hidden(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingChain:
        def __init__(self, **kwargs: Any) -> None:
            self.progress = kwargs.get("progress")

        def run(self, scenario_id: str, case: Any, **kwargs: Any) -> FullChainReport:
            report = FullChainReport(scenario_id=scenario_id, chain_id="chain_fail_stage")
            if self.progress:
                self.progress("execution", "FAILED", "browser unavailable")
            report.execution = {"error": "RuntimeError: no browser", "execution_status": "FAILED"}
            report.completed = False
            report.stages.append(ChainStage("execution", "FAILED", "browser unavailable"))
            return report

    monkeypatch.setattr("packages.execution.pipeline.FullChain", FailingChain)
    body = client.post("/api/v1/chains", json={"case_id": "TC-LIFE-001"}).json()

    assert body["run_status"] == "FAILED"
    assert body["completed"] is False
    stored = client.get(f"/api/v1/chains/{body['chain_id']}").json()
    assert stored["execution"]["error"].startswith("RuntimeError")
    assert any(s["status"] == "FAILED" for s in stored["stages"])
    run = client.get(f"/api/v1/runs/{body['run_id']}").json()
    assert run["status"] == "FAILED"
    assert run["error"]


def test_cleanup_policy_is_planned_and_recorded(client: TestClient) -> None:
    """Cleanup is decided before a run and its outcome recorded separately."""
    from packages.execution.models import CleanupPolicy, TestCaseVersion, TestIntent
    from packages.execution.pipeline import FullChain

    case = TestCaseVersion(
        case_id="TC-LIFE-CLEAN",
        version=1,
        title="cleanup plan",
        intent=TestIntent("i", "d"),
        steps=(),
        assertions=(),
        oracle_bindings=(),
        cleanup=CleanupPolicy(strategy="eval_reset"),
    )
    # A non-local host must never be reset, whatever the policy says.
    chain = FullChain(base_url="http://example.com", graph=None)  # type: ignore[arg-type]
    plan = chain.plan_cleanup(case)
    assert plan["allowed"] is False
    assert "not an approved local test instance" in plan["reason"]

    report = FullChainReport(scenario_id="s", chain_id="chain_clean")
    chain.apply_cleanup(report, plan)
    assert report.cleanup["done"] is True
    assert report.stages[-1].name == "cleanup"
    assert report.stages[-1].status == "SKIPPED"


def test_chain_surface_requires_the_workbench(tmp_path: Path) -> None:
    """With the workbench disabled there is no orchestrator, so no chain route."""
    case_dir = tmp_path / "cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "TC-LIFE-001.json").write_text(json.dumps(CASE), encoding="utf-8")
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        case_dir=str(case_dir),
        workbench_enabled=False,
        exploration_data_dir=str(tmp_path / "explorations"),
    )
    with TestClient(build_app(settings)) as client:
        resp = client.post("/api/v1/chains", json={"case_id": "TC-LIFE-001"})
    assert resp.status_code in (404, 405)


def test_stages_are_ordered_as_documented() -> None:
    from packages.execution.pipeline import ChainProgress

    assert ChainProgress.ORDER[0] == "testcase"
    assert "event_persistence" in ChainProgress.ORDER
    assert "evidence_persistence" in ChainProgress.ORDER
    assert ChainProgress.ORDER[-1] == "cleanup"
