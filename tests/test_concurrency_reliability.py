"""R5-4.10 §7: bounded concurrency reliability.

Scope discipline
----------------
R5-7 proves the *serial* full chain over a long soak. It cannot prove anything
about interference, because nothing ever runs at the same time. These tests add
the smallest honest amount of concurrency — a handful of clients, runs and
writers inside one process — and assert the properties that only break under
overlap:

1. several SSE clients subscribe at once and every one sees every event;
2. several Runs execute at once and their events never cross;
3. one client disconnecting does not disturb the others;
4. concurrent submissions each reach a terminal state with their own steps;
5. cancelling one Run leaves the others' reports intact;
6. concurrent evidence writes never overwrite an existing record;
7. a failed transaction never leaves a run looking successful.

Deliberate limits: at most 4 clients / 4 runs / 8 writer threads, all in-process,
one isolated database and one isolated TeamFlow per module. No new
infrastructure (no Redis, no Celery, no multiprocessing) — the point is to find
interference bugs in the code that actually ships, not to build a load rig.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.main import build_app
from packages.core.config import AppEnv, Settings
from packages.evidence.manager import EvidenceBase, EvidenceManager
from packages.execution.tasks import RunService, RunStatus, TaskService
from tests.netutil import serve_uvicorn

CASE_DIR = Path("benchmark/cases")
EVAL_TOKEN = "local-dev-eval-token-change-me"
# Bounded on purpose: enough overlap to expose shared-state bugs, small enough
# that the suite stays fast on a developer machine.
CLIENTS = 4
RUNS = 3


# --------------------------------------------------------------- infrastructure


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


class _Recorder(threading.Thread):
    """Minimal SSE collector with a hard deadline.

    A stalled stream must fail the test, never hang the suite: the read timeout
    plus the overall deadline both bound this thread, and ``done`` is always set.
    """

    def __init__(
        self,
        url: str,
        *,
        stop_when: Callable[[list[dict[str, Any]]], bool] | None = None,
        max_seconds: float = 60.0,
    ) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.stop_when = stop_when or (lambda frames: False)
        self.max_seconds = max_seconds
        self.frames: list[dict[str, Any]] = []
        self.status: int | None = None
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self.timed_out = False

    def run(self) -> None:
        deadline = time.monotonic() + self.max_seconds
        try:
            timeout = httpx.Timeout(connect=10.0, read=5.0, write=10.0, pool=10.0)
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                with client.stream("GET", self.url) as resp:
                    self.status = resp.status_code
                    self.ready.set()
                    for line in resp.iter_lines():
                        if line.startswith("data:"):
                            self.frames.append(json.loads(line[5:].strip()))
                            if self.stop_when(self.frames):
                                break
                        if time.monotonic() > deadline:
                            self.timed_out = True
                            break
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            self.error = exc
        finally:
            self.done.set()


@pytest.fixture(scope="module")
def concurrent_platform(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """One isolated TeamFlow + one isolated platform API, both on OS-bound ports."""
    work = tmp_path_factory.mktemp("concurrency")
    with ExitStack() as stack:
        tf_base = stack.enter_context(serve_uvicorn("apps.teamflow.main:app"))
        tf_port = int(tf_base.rsplit(":", 1)[1])
        settings = Settings(
            app_env=AppEnv.TEST,
            database_url=f"sqlite:///{(work / 'platform.db').as_posix()}",
            teamflow_host="127.0.0.1",
            teamflow_port=tf_port,
            eval_control_token=EVAL_TOKEN,
            workbench_enabled=True,
            # Four workers so submissions genuinely overlap instead of queueing
            # behind a single thread and proving nothing.
            workbench_workers=CLIENTS,
            case_dir=str(CASE_DIR),
            exploration_data_dir=str(work / "explorations"),
            benchmark_data_dir=str(work / "benchmark"),
            chain_data_dir=str(work / "chains"),
            candidate_data_dir=str(work / "candidates"),
            blob_dir=str(work / "blobs"),
        )
        app = build_app(settings)
        api_base = stack.enter_context(serve_uvicorn(app))
        yield {"api": api_base, "app": app, "work": work}


def _client(api: str) -> httpx.Client:
    return httpx.Client(base_url=api, timeout=30, trust_env=False)


def _create_task(client: httpx.Client, task_id: str, case_id: str = "TC-CREATE-001") -> None:
    resp = client.post(
        "/api/v1/tasks",
        json={"task_id": task_id, "case_id": case_id, "title": f"concurrent {task_id}"},
    )
    assert resp.status_code == 201, resp.text


def _wait_terminal(client: httpx.Client, run_id: str, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/v1/runs/{run_id}").json()
        if data["status"] in RunStatus.TERMINAL:
            return data
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} never reached a terminal state")


# --------------------------------------------------------------------- 1 & 2 & 3


def test_concurrent_sse_clients_each_receive_every_event(
    concurrent_platform: dict[str, Any],
) -> None:
    """Fan-out under load: 4 concurrent subscribers, one publish set, no losses."""
    api = concurrent_platform["api"]
    bus = concurrent_platform["app"].state.orchestrator.bus
    run_id = "run_conc_fanout"
    tag = f"fanout-{time.time_ns()}"

    def recorder_stop(frames: list[dict[str, Any]]) -> bool:
        return len([f for f in frames if f["payload"].get("tag") == tag]) >= 8

    recorders = [
        _Recorder(f"{api}/api/v1/events/stream", stop_when=recorder_stop) for _ in range(CLIENTS)
    ]
    baseline = bus.subscriber_count
    for recorder in recorders:
        recorder.start()
    for recorder in recorders:
        assert recorder.ready.wait(20), "an SSE stream did not open"
        assert recorder.status == 200

    # Only publish once every client is really subscribed, otherwise a client
    # could "pass" on replay and prove nothing about live fan-out.
    _wait_until(
        lambda: bus.subscriber_count >= baseline + CLIENTS,
        20,
        "all clients subscribed server-side",
    )
    for index in range(8):
        bus.publish("conc.fanout", run_id, "task_conc", tag=tag, index=index)

    for recorder in recorders:
        assert recorder.done.wait(40), "a client did not receive the whole fan-out"
        assert recorder.error is None, recorder.error
        got = [f for f in recorder.frames if f["payload"].get("tag") == tag]
        assert [f["payload"]["index"] for f in got] == list(range(8))
        # Every client must observe the SAME event ids for the same publishes.
        assert len({f["event_id"] for f in got}) == 8
    signatures = [
        tuple(f["event_id"] for f in r.frames if f["payload"].get("tag") == tag) for r in recorders
    ]
    assert len(set(signatures)) == 1, "clients saw different event id sequences"


def test_concurrent_runs_keep_their_events_separate(
    concurrent_platform: dict[str, Any],
) -> None:
    """Overlapping Runs must not interleave payloads onto each other's events."""
    api = concurrent_platform["api"]

    with _client(api) as client:
        run_ids: list[str] = []
        for index in range(RUNS):
            task_id = f"T-CONC-{index}"
            _create_task(client, task_id)
            resp = client.post(f"/api/v1/tasks/{task_id}/runs", json={})
            assert resp.status_code == 202, resp.text
            run_ids.append(resp.json()["run_id"])

        recorder = _Recorder(
            f"{api}/api/v1/events/stream",
            stop_when=lambda frames: all(
                any(f["event_type"] == "run.finished" and f["run_id"] == rid for f in frames)
                for rid in run_ids
            ),
            max_seconds=120,
        )
        recorder.start()
        assert recorder.ready.wait(20)

        finals = [_wait_terminal(client, run_id) for run_id in run_ids]
        assert recorder.done.wait(120), "not every run's terminal event reached the stream"

    for final in finals:
        assert final["status"] == "COMPLETED", final
        assert final["test_verdict"] == "PASSED", final

    assert recorder.error is None, recorder.error
    # Each event belongs to exactly one of our runs and its sequence is strictly
    # increasing within that run: a crossed payload would break both invariants.
    by_run: dict[str, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
    for frame in recorder.frames:
        if frame["run_id"] in by_run:
            by_run[frame["run_id"]].append(frame)
    for run_id, frames in by_run.items():
        assert frames, f"no events observed for {run_id}"
        sequences = [f["sequence"] for f in frames]
        assert sequences == sorted(sequences), f"out-of-order events for {run_id}"
        assert len(set(sequences)) == len(sequences), f"duplicate sequence in {run_id}"
        # A run's terminal event must be last for that run.
        assert frames[-1]["event_type"] == "run.finished", [f["event_type"] for f in frames]
    # The three runs must not share a single event id.
    seen: set[str] = set()
    for frames in by_run.values():
        ids = {f["event_id"] for f in frames}
        assert not (seen & ids), "two runs shared an event id"
        seen |= ids


def test_one_client_disconnect_does_not_disturb_the_others(
    concurrent_platform: dict[str, Any],
) -> None:
    """Losing one subscriber must not stall or truncate the surviving stream."""
    api = concurrent_platform["api"]
    bus = concurrent_platform["app"].state.orchestrator.bus
    tag = f"disconnect-{time.time_ns()}"

    staying = _Recorder(
        f"{api}/api/v1/events/stream",
        stop_when=lambda frames: len([f for f in frames if f["payload"].get("tag") == tag]) >= 6,
        max_seconds=60,
    )
    leaving = _Recorder(
        f"{api}/api/v1/events/stream",
        stop_when=lambda frames: any(f["payload"].get("tag") == tag for f in frames),
        max_seconds=60,
    )
    baseline = bus.subscriber_count
    staying.start()
    leaving.start()
    assert staying.ready.wait(20) and leaving.ready.wait(20)
    _wait_until(lambda: bus.subscriber_count >= baseline + 2, 20, "both clients subscribed")

    bus.publish("conc.isolation", "run_conc_disconnect", "task_conc", tag=tag, index=0)
    # The leaving client stops reading as soon as it has the first event, which
    # closes its connection while the publisher keeps going.
    assert leaving.done.wait(30), "leaving client never received its first event"
    for index in range(1, 6):
        bus.publish("conc.isolation", "run_conc_disconnect", "task_conc", tag=tag, index=index)

    assert staying.done.wait(40), "the surviving client stopped receiving events"
    assert staying.error is None, staying.error
    got = [f["payload"]["index"] for f in staying.frames if f["payload"].get("tag") == tag]
    assert got == list(range(6)), got


# ------------------------------------------------------------------------- 4 & 5


def test_concurrent_submissions_all_finish_with_their_own_steps(
    concurrent_platform: dict[str, Any],
) -> None:
    """4 simultaneous submissions must each get a distinct, complete Run."""
    api = concurrent_platform["api"]
    submissions: dict[str, str] = {}
    errors: list[BaseException] = []

    with _client(api) as client:
        for index in range(CLIENTS):
            _create_task(client, f"T-PAR-{index}")

        def submit(index: int) -> None:
            try:
                resp = client.post(f"/api/v1/tasks/T-PAR-{index}/runs", json={})
                assert resp.status_code == 202, resp.text
                submissions[f"T-PAR-{index}"] = resp.json()["run_id"]
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(CLIENTS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not errors, errors
        assert len(submissions) == CLIENTS
        assert len(set(submissions.values())) == CLIENTS, "two submissions reused a run id"

        finals = {task: _wait_terminal(client, run_id) for task, run_id in submissions.items()}

    for task_id, final in finals.items():
        assert final["status"] == "COMPLETED", (task_id, final)
        assert final["test_verdict"] == "PASSED", (task_id, final)
        assert final["run_id"] == submissions[task_id]
        assert final["task_id"] == task_id, "a run was attributed to the wrong task"


def test_cancelling_one_run_leaves_the_others_intact(
    concurrent_platform: dict[str, Any],
) -> None:
    """Cancellation is per-run; it must never leak into a sibling execution."""
    api = concurrent_platform["api"]
    with _client(api) as client:
        for index in range(RUNS):
            _create_task(client, f"T-CANCEL-{index}")
        run_ids = []
        for index in range(RUNS):
            resp = client.post(f"/api/v1/tasks/T-CANCEL-{index}/runs", json={})
            assert resp.status_code == 202, resp.text
            run_ids.append(resp.json()["run_id"])

        victim = run_ids[1]
        cancelled = client.post(f"/api/v1/runs/{victim}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        # The response is authoritative about what cancel did; it must be a
        # coherent view of this run, never a 5xx or a foreign run id.
        assert cancelled.json()["run_id"] == victim, cancelled.text

        survivors = [run_id for run_id in run_ids if run_id != victim]
        finals = [_wait_terminal(client, run_id) for run_id in survivors]
        victim_final = _wait_terminal(client, victim)

    for final in finals:
        # The two untouched runs must be completely unaffected.
        assert final["status"] == "COMPLETED", final
        assert final["test_verdict"] == "PASSED", final
    assert victim_final["status"] in RunStatus.TERMINAL
    # A cancelled run must never be reported as a business PASS by accident.
    if victim_final["status"] == "CANCELLED":
        assert victim_final["test_verdict"] != "PASSED", victim_final


# --------------------------------------------------------------------- 6 & 7


def test_concurrent_evidence_writes_never_overwrite_history(tmp_path: Path) -> None:
    """Append-only evidence: 8 concurrent writers, 8 preserved records."""
    db = tmp_path / "evidence.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", connect_args={"check_same_thread": False})
    EvidenceBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    manager = EvidenceManager(factory)

    writers = 8
    started = threading.Barrier(writers)
    failures: list[BaseException] = []

    def write(index: int) -> None:
        try:
            started.wait(timeout=20)  # maximise real overlap
            manager.add_raw(
                evidence_id=f"ev-conc-{index}",
                evidence_type="screenshot",
                source=f"conc:{index}",
                data=f"payload-{index}".encode(),
                run_id=f"run_conc_{index}",
                summary=f"concurrent record {index}",
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, failures
    stored = [manager.get(f"ev-conc-{index}") for index in range(writers)]
    assert all(stored), f"lost evidence records: {[i for i, s in enumerate(stored) if not s]}"
    # Each record must still describe its own bytes — no cross-overwrite.
    for index, record in enumerate(stored):
        assert record is not None
        assert record["run_id"] == f"run_conc_{index}"
        assert record["payload"]["byte_length"] == len(f"payload-{index}".encode())
    ids = {record["evidence_id"] for record in stored if record}
    assert ids == {f"ev-conc-{index}" for index in range(writers)}
    engine.dispose()


def test_failed_transaction_never_leaves_a_successful_run(tmp_path: Path) -> None:
    """A commit failure must surface as an error, never as COMPLETED/PASSED."""
    db = tmp_path / "txn.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", connect_args={"check_same_thread": False})
    from packages.execution.tasks import create_task_tables

    create_task_tables(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    service = RunService(factory)
    TaskService(factory).create("T-TXN", "TC-CREATE-001", "transaction failure probe")
    run_id = "run_txn_failure"
    service.create_run(
        run_id=run_id,
        task_id="T-TXN",
        case_id="TC-CREATE-001",
        case_version=1,
        timeout_s=30,
        worker_id="worker-test",
    )
    before = service.get(run_id)
    assert before.status == RunStatus.QUEUED

    class _BoomSession(Session):
        def commit(self) -> None:  # type: ignore[override]
            raise RuntimeError("simulated commit failure")

    class _BoomFactory:
        def __call__(self) -> Session:
            return _BoomSession(bind=engine)

    broken = RunService(_BoomFactory())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        broken.finalize(
            run_id,
            status=RunStatus.COMPLETED,
            execution_status="COMPLETED",
            test_verdict="PASSED",
            report={"steps": []},
        )

    # Read back through a healthy session: the failed write must not have landed.
    after = service.get(run_id)
    assert after.status == RunStatus.QUEUED, f"failed transaction leaked status {after.status}"
    assert after.test_verdict != "PASSED", "a failed transaction produced a PASSED verdict"
    engine.dispose()
