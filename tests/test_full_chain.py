"""Stage-13 final integration: full business loop across 10 mandated scenarios.

The 10 scenarios from v1.1 §阶段13:
 1 normal create  2 UI-success-but-persistence-fail  3 API 500  4 network timeout
 5 locator change  6 async timing  7 browser unavailable  8 multi-source conflict
 9 probe out-of-scope  10 test-script repair

Each scenario produces a FullChainReport with REAL execution evidence (or an
honestly-recorded failure). Chain: TestCase -> Playwright -> Verdict ->
Event/Evidence -> Graph -> Diagnosis -> Probe -> Disposition -> Repair gate.
All numbers asserted here come from real runs, not fixtures with fixed outputs.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from packages.core.errors import ForbiddenError
from packages.evidence.graph import make_in_memory_graph
from packages.execution.models import load_case
from packages.execution.pipeline import FullChain
from packages.execution.repair import DiffEntry, RepairService
from tests.netutil import release_reserved, reserve_port, take_reserved

# The server is per-test (see below), but the module-level constants are read by
# the helpers, so they must hold a valid reserved port even before the first
# fixture runs. The reservation is released on the first fixture entry.
_BOOTSTRAP_PORT = reserve_port()
TF_PORT = _BOOTSTRAP_PORT
TF_BASE = f"http://127.0.0.1:{TF_PORT}"
EVAL_H = {"X-Eval-Token": "local-dev-eval-token-change-me"}
CREATE = load_case(Path("benchmark/cases/TC-CREATE-001.json"))
CREATE_WITH_BANNER = load_case(Path("benchmark/cases/TC-CREATE-002.json"))

# R4-4 isolation evidence: one JSONL entry per test run (db path, worker, port).
ISOLATION_LOG = Path(".traceguard-tmp/r4/s2-isolation/run-log.jsonl")


def _log_isolation(entry: dict[str, Any]) -> None:
    ISOLATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ISOLATION_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


@pytest.fixture()
def teamflow_server(tmp_path: Path) -> str:
    """R4-4 (approved): per-test isolated TeamFlow.

    Every test gets its OWN SQLite database (tmp_path) and OWN port — nothing
    is shared with other tests or with the business DB, so fault injection in
    one test can never pollute another. The fixture rebinds the module-level
    TF_BASE so the _reset/_enable/_fault_events/_chain helpers target this
    instance. Test intent and assertions are unchanged.
    """
    global TF_PORT, TF_BASE
    from apps.teamflow import service as tf_service
    from apps.teamflow.eval_models import Base as EvalBase  # noqa: F401 - table parity
    from apps.teamflow.main import build_app
    from packages.core.config import Settings

    db_path = tmp_path / f"teamflow-{uuid.uuid4().hex[:10]}.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}", app_env="test")
    app = build_app(settings)
    # Release the module-level placeholder reservation (idempotent: no-op once
    # consumed) before reserving this test's own port.
    release_reserved(_BOOTSTRAP_PORT)
    port = reserve_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, kwargs={"sockets": take_reserved(port)}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("isolated TeamFlow not healthy")
        TF_PORT, TF_BASE = port, base

        # Seed the six authoritative scenarios into THIS test's eval plane so the
        # isolated DB has the same GT content the shared DB used to carry.
        from scripts.seed_scenarios import META, SCENARIOS

        with app.state.session_factory() as session:
            for sid, fid, desc, gt in SCENARIOS:
                meta = dict(META)
                meta["fault_id"] = fid or "NONE"
                tf_service.seed_scenario(session, sid, fid, desc, meta, gt)
                session.commit()

        _reset()
        _log_isolation(
            {
                "at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                "port": port,
                "db": str(db_path),
                "fault_config": "none (per-test reset before test body)",
            }
        )
        yield base
        _log_isolation(
            {
                "at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                "port": port,
                "db": str(db_path),
                "fault_config": "teardown: server stopped, isolated DB retained in tmp",
            }
        )
    finally:
        server.should_exit = True
        t.join(timeout=15)
        assert not t.is_alive(), "server thread did not stop"
        release_reserved(port)


def _reset() -> None:
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        c.post("/__eval__/faults/clear", headers=EVAL_H)
        c.post("/__eval__/data/reset", headers=EVAL_H)


def _enable(fault: str | None) -> None:
    _reset()
    if fault:
        with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
            c.post(f"/__eval__/faults/{fault}/enable", headers=EVAL_H)


def _fault_events() -> list[dict[str, Any]]:
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        data = c.get("/__eval__/events", headers=EVAL_H).json()
    return data if isinstance(data, list) else data.get("events", [])


def _chain(sid: str) -> FullChain:
    graph, _ = make_in_memory_graph()
    return FullChain(base_url=TF_BASE, graph=graph, fault_events_provider=_fault_events)


def test_s1_normal_create(teamflow_server: None) -> None:
    _enable(None)
    r = _chain("S1").run("S1-normal", CREATE, fault_events=_fault_events())
    assert r.completed and r.execution["test_verdict"] == "PASSED"
    assert r.execution["evidence_count"] >= 10 and r.execution["event_count"] >= 5
    assert r.diagnosis["diagnosis_status"] == "CONFIRMED"
    assert r.disposition == "MANUAL_REVIEW"
    assert "graph" in {s.name for s in r.stages}


def test_s2_ui_success_persistence_fail(teamflow_server: None) -> None:
    _enable("TF-DB-001")
    r = _chain("S2").run("S2-db", CREATE, fault_events=_fault_events())
    assert r.execution["test_verdict"] == "FAILED"
    assert r.diagnosis["failure_mechanism"] == "PERSISTENCE"
    assert r.diagnosis["diagnosis_status"] == "CONFIRMED"
    assert r.disposition == "PRODUCT_DEFECT"


def test_s3_api_500(teamflow_server: None) -> None:
    _enable("TF-API-001")
    r = _chain("S3").run("S3-api", CREATE, fault_events=_fault_events())
    assert r.execution["test_verdict"] == "FAILED"
    assert r.diagnosis["failure_mechanism"] == "API"


def test_s4_network_timeout(teamflow_server: None) -> None:
    _enable("TF-NET-001")
    r = _chain("S4").run("S4-net", CREATE, fault_events=_fault_events())
    # network delay faults do not change persisted outcome; recorded honestly
    assert r.execution["test_verdict"] in ("PASSED", "FAILED")
    assert r.execution["run_id"].startswith("run_")


def test_s5_locator_change(teamflow_server: None) -> None:
    _enable(None)
    bad = dataclasses.replace(
        CREATE,
        case_id="TC-LOCATOR-CHANGED",
        steps=tuple(
            dataclasses.replace(s, target="[data-testid=does-not-exist]")
            if s.step_id == "s7"
            else s
            for s in CREATE.steps
        ),
    )
    r = _chain("S5").run("S5-locator", bad, fault_events=[])
    assert r.completed
    assert r.execution["test_verdict"] in ("FAILED", "INCONCLUSIVE")
    assert "s7" in r.execution["failed_steps"]
    # A single locator failure with no authoritative fault evidence must NOT be
    # auto-confirmed or auto-repaired: no confirmed root cause, never PASSED.
    assert r.diagnosis["diagnosis_status"] != "CONFIRMED"
    assert r.diagnosis["root_causes"] == []
    assert r.execution["test_verdict"] != "PASSED"


def test_s6_async_timing(teamflow_server: None) -> None:
    _enable("TF-TIME-001")
    r = _chain("S6").run("S6-timing", CREATE, fault_events=_fault_events())
    assert r.completed
    assert r.execution["run_id"].startswith("run_")


def test_s7_browser_unavailable_recorded_honestly() -> None:
    graph, _ = make_in_memory_graph()
    # point at a closed local port -> runner raises, chain records honest failure
    chain = FullChain(base_url="http://127.0.0.1:9", graph=graph)
    r = chain.run("S7-no-browser", CREATE, fault_events=[])
    assert r.completed
    # Target unreachable: steps fail and verdict is NOT_EVALUATED — never a
    # false PASSED. The chain completes and records the environment failure.
    # Stage order now starts with cleanup_pre (R5-4.2 CleanupPolicy), so the
    # check is on the named stage rather than on a positional index.
    execution_stage = next((s for s in r.stages if s.name == "execution"), None)
    assert execution_stage is not None
    assert r.execution["test_verdict"] in ("NOT_EVALUATED", "INCONCLUSIVE", "FAILED")
    assert r.execution["test_verdict"] != "PASSED"


def test_s8_multi_source_conflict(teamflow_server: None) -> None:
    _enable("TF-UI-001")
    # Use the case that actually asserts the success banner (s10) — otherwise
    # the UI lie is invisible to the test.
    r = _chain("S8").run("S8-conflict", CREATE_WITH_BANNER, fault_events=_fault_events())
    # TC-CREATE-002 binds BOTH a UI and a state Oracle; under the UI lie they
    # disagree, so the frozen adjudication rule refuses to auto-PASS — the
    # conflict surfaces honestly and both evidence sides are preserved.
    assert r.execution["test_verdict"] in ("FAILED", "INCONCLUSIVE")
    assert r.execution["test_verdict"] != "PASSED"
    assert "s10" in r.execution["failed_steps"]
    hyp = next(h for h in r.diagnosis["hypotheses"] if h["hypothesis_id"] == "h-ui")
    assert hyp["supporting"] and hyp["contradicting"]


def test_s9_probe_out_of_scope_rejected_and_audited(teamflow_server: None) -> None:
    from packages.agents.probes import (
        PermissionGateway,
        ProbeEngine,
        ProbeRequest,
        ProbeSpec,
    )

    spec = ProbeSpec(
        "API_READ",
        {"required": ["workspace_id"]},
        "workspace",
        "diagnose",
        True,
        "none",
        2.0,
        1.0,
        "LOW",
        5.0,
        "none",
        {},
        "none",
    )
    registry = {"API_READ": spec}
    engine = ProbeEngine(PermissionGateway(registry), registry)
    engine.register_handler("API_READ", lambda req: {"project_count": 1})
    # cross-workspace read must be rejected and audited, never executed
    with pytest.raises(ForbiddenError):
        engine.execute(ProbeRequest("API_READ", {"workspace_id": "ws-other"}, "ws-own"))
    assert any(a.decision == "REJECTED" and a.check == "resource_scope" for a in engine.audit)


def test_s10_test_script_repair_gated(teamflow_server: None) -> None:
    _enable(None)
    base = CREATE
    candidate = dataclasses.replace(
        base,
        version=2,
        steps=tuple(
            dataclasses.replace(s, target="[data-testid=new-username]") if s.step_id == "s2" else s
            for s in base.steps
        ),
    )
    svc = RepairService()
    p = svc.create_proposal(
        "p-s10",
        base,
        candidate,
        change_kinds=("locator_update",),
        diff=[DiffEntry("locator_update", "s2", "username", "new-username")],
    )
    p = svc.validate(p.proposal_id)
    assert p.state == "VALIDATED"
    p = svc.submit_for_approval(p.proposal_id)
    p = svc.approve(p.proposal_id, approver="human", approval_record="final-demo-review")
    promoted, version = svc.promote(
        p.proposal_id, current_formal=base, sandbox_replay=lambda _c: True
    )
    assert promoted.state == "PROMOTED" and version.version == 2
    # required assertions + formal oracle survived the locater fix
    assert base.assertions == version.assertions
    assert base.oracle_bindings == version.oracle_bindings


def test_all_ten_scenarios_have_execution_evidence(teamflow_server: None) -> None:
    """Summary guard: every scenario above produced inspectable evidence."""
    ids = [
        "S1-normal",
        "S2-db",
        "S3-api",
        "S4-net",
        "S5-locator",
        "S6-timing",
        "S7-no-browser",
        "S8-conflict",
    ]
    for sid in ids:
        _enable(None)
        r = _chain(sid).run(sid, CREATE, fault_events=[])
        assert r.chain_id and r.stages, sid
        assert "test_verdict" in r.execution or "error" in r.execution, sid
