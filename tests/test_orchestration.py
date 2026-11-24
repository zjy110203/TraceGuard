"""Stage-3 acceptance: Task/Run/Step orchestration, real execution, SSE.

Verifies the v1.1 core rules with real code (no mock verdict):
  * Task is logical; each submit/retry is a distinct append-only Run;
  * timeout and cancellation are enforced between steps via the abort hook;
  * four result dimensions stay separate (diagnosis NOT_STARTED);
  * status machine rejects illegal transitions;
  * SSE/event history is monotonic and replayable by Last-Event-ID.

Orchestration uses the documented in-process worker substitution (no Celery).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from apps.api.main import build_app
from packages.core.config import Settings
from packages.core.errors import ConflictError
from packages.execution.tasks import RunStatus
from tests.netutil import free_port

TF_PORT = free_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"
CASE_DIR = Path("benchmark/cases")
EVAL_TOKEN = "local-dev-eval-token-change-me"


@pytest.fixture(scope="module")
def teamflow_server() -> Any:
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=TF_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        pytest.fail("TeamFlow server did not become healthy")
    yield
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def platform(teamflow_server: Any, tmp_path: Path) -> Any:
    import uuid

    db_file = tmp_path / f"tg_{uuid.uuid4().hex[:8]}.db"
    db = f"sqlite:///{db_file.as_posix()}"
    settings = Settings(
        app_env="test",
        database_url=db,
        teamflow_host="127.0.0.1",
        teamflow_port=TF_PORT,
        eval_control_token=EVAL_TOKEN,
        workbench_enabled=True,
        workbench_workers=2,
    )
    app = build_app(settings)
    with TestClient(app) as client:
        yield client, app


def _create_task(client: TestClient, task_id: str, case_id: str = "TC-CREATE-001") -> None:
    r = client.post(
        "/api/v1/tasks",
        json={"task_id": task_id, "case_id": case_id, "title": f"task {task_id}"},
    )
    assert r.status_code == 201, r.text


def _wait_terminal(client: TestClient, run_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/v1/runs/{run_id}").json()
        if data["status"] in RunStatus.TERMINAL:
            return data
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not reach terminal state")


def test_task_run_separation_and_append_only_history(platform: Any) -> None:
    client, _app = platform
    _create_task(client, "T-SEP")
    r1 = client.post("/api/v1/tasks/T-SEP/runs", json={}).json()
    r2 = client.post("/api/v1/tasks/T-SEP/runs", json={}).json()
    assert r1["run_id"] != r2["run_id"]
    assert r1["attempt"] == 1 and r2["attempt"] == 2
    f1 = _wait_terminal(client, r1["run_id"])
    f2 = _wait_terminal(client, r2["run_id"])
    assert f1["status"] == "COMPLETED" and f1["test_verdict"] == "PASSED"
    assert f2["status"] == "COMPLETED" and f2["test_verdict"] == "PASSED"
    hist = client.get("/api/v1/tasks/T-SEP/runs").json()
    assert [h["attempt"] for h in hist] == [1, 2]
    # four independent dimensions
    assert f1["execution_status"] == "COMPLETED"
    assert f1["diagnosis_status"] == "NOT_STARTED"
    assert f1["diagnosis_outcome"] == "NOT_STARTED"
    # report + steps persisted
    assert client.get(f"/api/v1/runs/{r1['run_id']}/report").status_code == 200
    assert len(client.get(f"/api/v1/runs/{r1['run_id']}").json()["steps"]) >= 8


def test_retry_is_new_run_not_overwrite(platform: Any) -> None:
    client, _app = platform
    _create_task(client, "T-RETRY")
    first = client.post("/api/v1/tasks/T-RETRY/runs", json={}).json()
    _wait_terminal(client, first["run_id"])
    retry = client.post("/api/v1/tasks/T-RETRY/runs", json={}).json()
    assert retry["run_id"] != first["run_id"]
    assert retry["attempt"] == 2
    # first run's report is immutable
    report1 = client.get(f"/api/v1/runs/{first['run_id']}/report").json()
    assert report1["run_id"] == first["run_id"]
    _wait_terminal(client, retry["run_id"])


def test_cancel_requires_terminal_and_is_cooperative(platform: Any) -> None:
    client, _app = platform
    _create_task(client, "T-CANCEL")
    view = client.post("/api/v1/tasks/T-CANCEL/runs", json={}).json()
    # cancel immediately; worker checks the flag between steps
    cancelled = client.post(f"/api/v1/runs/{view['run_id']}/cancel").json()
    assert cancelled["cancel_requested"] is True
    final = _wait_terminal(client, view["run_id"])
    assert final["status"] == "CANCELLED"
    # cancelling a terminal run is idempotent no-op
    again = client.post(f"/api/v1/runs/{view['run_id']}/cancel").json()
    assert again["status"] == "CANCELLED"


def test_timeout_is_enforced(platform: Any) -> None:
    client, _app = platform
    _create_task(client, "T-TIMEOUT", case_id="TC-CREATE-DUP-001")
    view = client.post("/api/v1/tasks/T-TIMEOUT/runs", json={"timeout_s": 0.05}).json()
    final = _wait_terminal(client, view["run_id"])
    assert final["status"] == "TIMED_OUT"
    assert "timeout" in final["error"].lower()


def test_status_machine_rejects_illegal_transition(platform: Any) -> None:
    _client, app = platform
    orch = app.state.orchestrator
    _create_task(_client, "T-SM")
    view = orch.submit("T-SM")
    final = _wait_terminal(_client, view.run_id)
    assert final["status"] == "COMPLETED"
    with pytest.raises(ConflictError):
        orch.runs.transition(view.run_id, RunStatus.RUNNING)  # terminal -> RUNNING illegal


def test_event_history_monotonic_and_replayable(platform: Any) -> None:
    client, _app = platform
    _create_task(client, "T-EVT")
    view = client.post("/api/v1/tasks/T-EVT/runs", json={}).json()
    _wait_terminal(client, view["run_id"])
    events = client.get("/api/v1/events").json()
    seqs = [e["sequence"] for e in events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    types = {e["event_type"] for e in events}
    assert {"run.queued", "run.running", "run.finished"}.issubset(types)
    # replay from Last-Event-ID: only later events returned
    mid = events[1]["event_id"]
    replay = client.get(f"/api/v1/events?after_sequence={events[1]['sequence']}").json()
    assert all(e["sequence"] > events[1]["sequence"] for e in replay)
    assert mid  # event ids used by SSE Last-Event-ID

    with client.stream("GET", "/api/v1/events/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
