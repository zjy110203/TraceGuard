"""R5-4.4 acceptance: evidence integrity that survives a real round-trip.

Before this round a screenshot's ``integrity_check`` was a 16-character prefix
of the SHA-256, so "verify" compared a prefix against a full digest and the
comparison could never be meaningful. Here the digest is checked against the
actual bytes at every hop: capture -> blob store -> evidence manager -> retrieval
-> recompute.

The trace hash is checked the same way, and — because that is the point of a
hash — after a JSON round-trip of the *persisted* record, not against memory.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.main import build_app
from apps.teamflow.main import build_app as build_teamflow_app
from packages.agents.exploration import (
    ExplorationTrace,
    ScriptedPolicy,
    ToolCall,
    compute_trace_hash,
    verify_trace_record,
)
from packages.agents.explorer import Explorer
from packages.core.config import Settings
from packages.core.persistence import (
    OutcomeCounter,
    PersistenceOutcome,
    classify_error,
)
from packages.evidence.manager import EvidenceBase, EvidenceManager
from packages.evidence.storage import LocalBlobStore
from packages.execution.models import (
    AssertionSpec,
    OracleBinding,
    OracleSpec,
    TestCaseVersion,
    TestIntent,
    TestStep,
)
from packages.execution.runner import LocalRunner
from tests.netutil import free_port

for _m in (TestCaseVersion, TestStep, TestIntent):
    _m.__test__ = False

TF_PORT = free_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"


@pytest.fixture(scope="module")
def teamflow(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db = tmp_path_factory.mktemp("tf4") / "teamflow.db"
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{db.as_posix()}",
        eval_control_token="test-eval-token",
        teamflow_test_user="tester",
        teamflow_test_password="test-pass-123",
    )
    app = build_teamflow_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=TF_PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(60):
        try:
            if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:  # pragma: no cover
        pytest.fail("TeamFlow did not become healthy")
    yield TF_BASE
    server.should_exit = True
    thread.join(timeout=10)


def _manager(tmp_path: Path) -> EvidenceManager:
    engine = create_engine(f"sqlite:///{(tmp_path / 'evd.db').as_posix()}")
    EvidenceBase.metadata.create_all(engine)
    return EvidenceManager(sessionmaker(bind=engine, expire_on_commit=False))


def _case() -> TestCaseVersion:
    case = TestCaseVersion(
        case_id="TC-EVD-001",
        version=1,
        title="one visible page, one assertion",
        intent=TestIntent("i-evd", "navigate and assert"),
        steps=(
            TestStep("s1", "navigate", "/"),
            TestStep("s2", "assert_visible", "[data-testid=login-submit]"),
        ),
        assertions=(AssertionSpec("a1", "login form visible", required=True),),
        oracle_bindings=(
            OracleBinding(
                "a1",
                primary=OracleSpec(
                    oracle_id="o1",
                    source_type="ui",
                    source_ref="ui.s2.passed",
                    expected_value=True,
                    source="integrity test",
                ),
            ),
        ),
        approved_by="human:int",
    )
    case.validate()
    return case


# ------------------------------------------------------- full digest, real bytes


def test_screenshot_digest_is_full_length_and_verifies(teamflow: str, tmp_path: Path) -> None:
    report = LocalRunner(teamflow).run(_case())
    shots = [e for e in report.evidence if e["evidence_type"] == "screenshot"]
    assert shots, "a real run must capture at least one screenshot"
    shot = shots[0]

    digest = shot["integrity_check"].removeprefix("sha256:")
    assert len(digest) == 64, "integrity must be a full SHA-256, not a prefix"
    raw = bytes.fromhex(shot["payload"]["image"])
    assert digest == hashlib.sha256(raw).hexdigest()

    manager = _manager(tmp_path)
    blob = LocalBlobStore(tmp_path / "blobs")
    stored = manager.import_record(shot, blob_store=blob)

    # The blob is content-addressed and retrievable by its own digest.
    key = stored["attachment_ref"]
    assert blob.verify(key) is True
    assert blob.get(key) == raw
    assert stored["payload"]["binary_sha256"] == "sha256:" + digest

    # Manager-level verification recomputes over the bytes, and fails on tamper.
    assert manager.verify(stored["evidence_id"], raw).valid is True
    tampered = manager.verify(stored["evidence_id"], raw + b"\x00")
    assert tampered.valid is False
    assert "mismatch" in tampered.detail.lower() or "sha256" in tampered.detail.lower()


def test_playwright_trace_digest_matches_the_file(teamflow: str, tmp_path: Path) -> None:
    report = LocalRunner(teamflow, trace_dir=tmp_path / "traces").run(_case())
    traces = [e for e in report.evidence if e["evidence_type"] == "playwright_trace"]
    assert traces, "tracing was enabled, so trace evidence must exist"
    digest = traces[0]["integrity_check"].removeprefix("sha256:")
    assert len(digest) == 64
    path = Path(traces[0]["payload"]["path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


# ------------------------------------------------------------- trace hash


def test_trace_hash_is_computed_after_redaction_and_verifies(teamflow: str) -> None:
    script = (
        ToolCall("navigate", {"url": f"{teamflow}/"}, "open"),
        ToolCall("fill", {"selector": "[data-testid=username]", "value": "tester"}, "user"),
        ToolCall(
            "fill",
            {"selector": "[data-testid=password]", "value": "test-pass-123"},
            "password",
        ),
    )
    trace = Explorer(policy=ScriptedPolicy(script)).explore("i-hash", teamflow)
    assert trace.source_trace_hash.startswith("sha256:")
    assert "test-pass-123" not in json.dumps(trace.to_dict(), ensure_ascii=False)

    record = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
    assert verify_trace_record(record) is True

    # Any change to the persisted content invalidates the hash.
    tampered = json.loads(json.dumps(record))
    tampered["action_sequence"][0]["args"]["url"] = "http://127.0.0.1:9/evil"
    assert verify_trace_record(tampered) is False

    # And the hash is stable: recomputation over the same input is identical,
    # now over the full v2 scope (intent + decisions + versions included).
    assert (
        compute_trace_hash(
            exploration_id=trace.exploration_id,
            action_sequence=trace.action_sequence,
            observations=trace.observations,
            security_events=trace.security_events,
            stop_reason=trace.stop_reason,
            test_intent=trace.test_intent,
            agent_decisions=trace.agent_decisions,
            versions={
                "agent_version": trace.agent_version,
                "prompt_version": trace.prompt_version,
                "tool_version": trace.tool_version,
                "application_version": trace.application_version,
                "browser_version": trace.browser_version,
            },
            hash_version=trace.hash_version,
        )
        == trace.source_trace_hash
    )


def test_persisted_exploration_reports_integrity(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        exploration_data_dir=str(tmp_path / "explorations"),
        case_dir=str(tmp_path / "cases"),
        candidate_data_dir=str(tmp_path / "candidates"),
        benchmark_data_dir=str(tmp_path / "bench"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
    )
    trace = ExplorationTrace(
        exploration_id="exp_integ_1",
        test_intent_ref="i",
        application_version="v",
        browser_version="b",
        agent_version="a",
        prompt_version="p",
        tool_version="t",
        action_sequence=[{"tool": "navigate", "args": {"url": "http://127.0.0.1:8001/"}}],
    )
    trace.finalize()
    record = {**trace.to_dict(), "status": "COMPLETED"}
    (tmp_path / "explorations").mkdir(parents=True, exist_ok=True)
    (tmp_path / "explorations" / "exp_integ_1.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    with TestClient(build_app(settings)) as client:
        body = client.get("/api/v1/explorations/exp_integ_1").json()
        assert body["trace_integrity_verified"] is True

        # Corrupt it on disk: the read must say so instead of serving it as fine.
        path = tmp_path / "explorations" / "exp_integ_1.json"
        broken = json.loads(path.read_text(encoding="utf-8"))
        broken["stop_reason"] = "FORGED"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        assert (
            client.get("/api/v1/explorations/exp_integ_1").json()["trace_integrity_verified"]
            is False
        )


# ------------------------------------------------- persistence classification


def test_errors_are_classified_not_collapsed() -> None:
    assert classify_error(ValueError("duplicate sequence for producer")) is (
        PersistenceOutcome.DUPLICATE
    )
    assert classify_error(RuntimeError("UNIQUE constraint failed: ev_event.event_id")) is (
        PersistenceOutcome.DUPLICATE
    )
    assert classify_error(ValueError("jsondecodeerror: Expecting value")) is (
        PersistenceOutcome.CORRUPT
    )
    assert classify_error(ConnectionError("connection refused")) is (
        PersistenceOutcome.DEPENDENCY_UNAVAILABLE
    )
    assert classify_error(OSError("disk quota exceeded")) is PersistenceOutcome.FAILED


def test_partial_persistence_gets_its_own_status() -> None:
    counter = OutcomeCounter()
    counter.add(PersistenceOutcome.WRITTEN, "e1")
    counter.add(PersistenceOutcome.DUPLICATE, "e2")
    assert counter.summary()["status"] == "OK"

    counter.add(PersistenceOutcome.FAILED, "e3", "disk full")
    summary = counter.summary()
    assert summary["status"] == "PARTIAL"
    assert summary["written"] == 1
    assert summary["failures"][0]["ref"] == "e3"

    empty = OutcomeCounter()
    empty.add(PersistenceOutcome.DEPENDENCY_UNAVAILABLE, "e9", "db down")
    assert empty.summary()["status"] == "FAILED"


def test_evidence_import_twice_is_a_duplicate_not_a_failure(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = {
        "evidence_id": "evd_dup_1",
        "evidence_type": "dom_snapshot",
        "raw": True,
        "source": "browser:s1",
        "run_id": "run_1",
        "step_id": "s1",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "summary": "text",
        "attachment_ref": "",
        "integrity_check": "",
        "source_trust": "observed",
        "derived_from": (),
        "redacted": False,
        "retention": "run_scoped",
        "payload": {"text": "hi"},
    }
    manager.import_record(dict(record))
    with pytest.raises(Exception) as exc:
        # Bypass import_record's idempotent short-circuit to exercise the raw
        # conflict path the chain persistence layer classifies.
        manager._add(
            evidence_id="evd_dup_1",
            layer="raw",
            evidence_type="dom_snapshot",
            source="browser:s1",
            run_id="run_1",
            step_id="s1",
            summary="text",
            integrity_check="",
            source_trust="observed",
            derived_from=(),
            derivation_rule="",
            retention="run_scoped",
            attachment_ref="",
            payload={},
        )
    assert classify_error(exc.value) is PersistenceOutcome.DUPLICATE


# ------------------------------------------------------- storage integrity API


def test_storage_integrity_reports_corrupt_files(tmp_path: Path) -> None:
    explor = tmp_path / "explorations"
    explor.mkdir(parents=True, exist_ok=True)
    (explor / "exp_broken_1.json").write_text("{not json", encoding="utf-8")
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        exploration_data_dir=str(explor),
        case_dir=str(tmp_path / "cases"),
        candidate_data_dir=str(tmp_path / "candidates"),
        benchmark_data_dir=str(tmp_path / "bench"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
    )
    with TestClient(build_app(settings)) as client:
        # A corrupt file must not blank the list...
        assert client.get("/api/v1/explorations").json() == []
        # ...but it must be visible somewhere.
        body = client.get("/api/v1/evidence/storage-integrity").json()
    assert body["status"] == "DEGRADED"
    assert body["corrupt_total"] >= 1
    corrupt = body["stores"]["exploration_store"]["corrupt_files"]
    assert any("exp_broken_1.json" in p for p in corrupt)


# ------------------------------------------------------------- workspace


def test_chain_events_use_the_request_workspace(tmp_path: Path) -> None:
    """Run, events and chain report must agree on the workspace (R5-4.4 §6.4)."""
    case_dir = tmp_path / "cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    case = _case()
    payload: dict[str, Any] = json.loads(json.dumps(case.to_dict()))
    # Not needing a browser: the chain fails to execute, but persistence still runs.
    (case_dir / f"{case.case_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{(tmp_path / 'tg.db').as_posix()}",
        case_dir=str(case_dir),
        teamflow_base_url="http://127.0.0.1:9",  # unreachable on purpose
        exploration_data_dir=str(tmp_path / "explorations"),
        candidate_data_dir=str(tmp_path / "candidates"),
        benchmark_data_dir=str(tmp_path / "bench"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
    )
    with TestClient(build_app(settings)) as client:
        body = client.post(
            "/api/v1/chains", json={"case_id": case.case_id, "workspace_id": "ws-team-a"}
        ).json()
        assert body["workspace_id"] == "ws-team-a"
        store = client.app.state.event_store
        rows = store.query(workspace_id="ws-team-a")
        stored = client.get(f"/api/v1/chains/{body['chain_id']}").json()
    assert stored["workspace_id"] == "ws-team-a"
    # Whatever was persisted landed in the same workspace the chain reported.
    assert all(r["context_refs"].get("workspace_id") in (None, "ws-team-a") for r in rows)
