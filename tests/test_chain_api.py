"""Full-chain surface: approval gate, honest failure handling, evidence import.

The chain route launches Playwright, so these tests never execute a real run.
They pin the parts that must hold regardless of the browser:

* an unapproved case can never be executed through the product API;
* an execution failure is stored as a recorded result, not surfaced as a 500;
* imported evidence keeps the integrity hash the runner computed and never
  re-derives trust;
* a chain report is only written after the evidence plane writes are attempted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.chain_api import ChainStore
from apps.api.main import build_app
from packages.core.config import Settings
from packages.core.errors import ConflictError, ValidationError
from packages.evidence.manager import EvidenceManager
from packages.evidence.storage import LocalBlobStore
from packages.execution.pipeline import ChainStage, FullChainReport

CASE: dict[str, Any] = {
    "case_id": "TC-TEST-001",
    "version": 1,
    "title": "test-only case",
    "intent": {"intent_id": "i-1", "description": "d", "source_ref": ""},
    "steps": [{"step_id": "s1", "action": "navigate", "target": "/", "value": "", "expected": ""}],
    "assertions": [
        {
            "assertion_id": "a1",
            "description": "a",
            "required": True,
            "source_ref": "",
            "notes": [],
        }
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


def _write_case(directory: Path, **changes: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "TC-TEST-001.json").write_text(
        json.dumps({**CASE, **changes}, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    case_dir = tmp_path / "cases"
    _write_case(case_dir)
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        case_dir=str(case_dir),
        exploration_data_dir=str(tmp_path / "explorations"),
        benchmark_data_dir=str(tmp_path / "benchmark"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
    )
    with TestClient(build_app(settings)) as c:
        yield c


# ------------------------------------------------------------------ routing


def test_chain_list_is_empty_before_any_run(client: TestClient) -> None:
    assert client.get("/api/v1/chains").json() == []


def test_chain_unknown_case_is_404(client: TestClient) -> None:
    resp = client.post("/api/v1/chains", json={"case_id": "TC-NOPE"})
    assert resp.status_code == 404


def test_chain_refuses_unapproved_case(client: TestClient, tmp_path: Path) -> None:
    # Rewrite the only case without an approval record, then rebuild the client.
    case_dir = tmp_path / "cases"
    _write_case(case_dir, approved_by="")
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        case_dir=str(case_dir),
        exploration_data_dir=str(tmp_path / "explorations"),
        benchmark_data_dir=str(tmp_path / "benchmark"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
    )
    with TestClient(build_app(settings)) as local:
        resp = local.post("/api/v1/chains", json={"case_id": "TC-TEST-001"})
    assert resp.status_code == 409
    assert "approved" in resp.text.lower()


def test_chain_unknown_id_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/chains/chain_deadbeef").status_code == 404


def test_chain_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = ChainStore(tmp_path / "chains")
    assert store.get("../../etc/passwd") is None
    # ValidationError is the domain refusal; anything wider would hide a
    # silently-accepted traversal.
    with pytest.raises(ValidationError):
        store.save({"chain_id": "../evil"})


def test_chain_execution_failure_is_recorded_not_raised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser that cannot start must yield a stored FAILED chain, not a 500."""

    class FailingChain:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run(self, scenario_id: str, _case: Any, **_kwargs: Any) -> FullChainReport:
            report = FullChainReport(scenario_id=scenario_id, chain_id="chain_fail_1")
            report.execution = {"error": "RuntimeError: no browser"}
            report.stages = [ChainStage("execution", "FAILED", "RuntimeError")]
            report.completed = True
            return report

    monkeypatch.setattr("apps.api.chain_api.FullChain", FailingChain)
    resp = client.post("/api/v1/chains", json={"case_id": "TC-TEST-001"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["test_verdict"] is None
    assert body["persisted"]["evidence"]["written"] == 0

    stored = client.get(f"/api/v1/chains/{body['chain_id']}").json()
    assert stored["execution"]["error"].startswith("RuntimeError")
    assert stored["case_id"] == "TC-TEST-001"
    assert stored["approved_by"] == "human"


# ------------------------------------------------------- evidence import


def _manager(tmp_path: Path) -> EvidenceManager:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from packages.evidence.manager import EvidenceBase

    engine = create_engine(f"sqlite:///{(tmp_path / 'evd.db').as_posix()}")
    EvidenceBase.metadata.create_all(engine)
    return EvidenceManager(sessionmaker(bind=engine, expire_on_commit=False))


def _record(**changes: Any) -> dict[str, Any]:
    base = {
        "evidence_id": "evd_test1",
        "evidence_type": "screenshot",
        "raw": True,
        "source": "browser:s1",
        "run_id": "run_1",
        "step_id": "s1",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "summary": "screenshot (3 bytes)",
        "attachment_ref": "screenshots/x.png",
        "integrity_check": "sha256:abc123",
        "source_trust": "observed",
        "derived_from": (),
        "redacted": False,
        "retention": "run_scoped",
        "payload": {"bytes": 3},
    }
    return {**base, **changes}


def test_import_record_preserves_runner_hash(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    stored = manager.import_record(_record())
    # The runner already hashed the captured bytes; import must not invent a
    # different digest over a JSON view of the record.
    assert stored["integrity_check"] == "sha256:abc123"
    assert stored["layer"] == "raw"
    assert manager.get("evd_test1") is not None


def test_import_record_is_idempotent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = manager.import_record(_record())
    second = manager.import_record(_record(summary="different on re-import"))
    assert first["evidence_id"] == second["evidence_id"]
    assert second["summary"] == "screenshot (3 bytes)"


def test_import_record_requires_sources_for_derived(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ConflictError):
        manager.import_record(_record(raw=False, derived_from=()))


def test_import_record_moves_screenshot_bytes_to_blob_store(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    blobs = LocalBlobStore(tmp_path / "blobs")
    stored = manager.import_record(_record(payload={"image": "89504e47"}), blob_store=blobs)

    # Hex is replaced by a content-addressed reference, but the bytes survive.
    assert "image" not in stored["payload"]
    assert stored["payload"]["byte_length"] == 4
    key = stored["payload"]["blob"]["key"]
    assert stored["attachment_ref"] == key
    assert blobs.get(key) == bytes.fromhex("89504e47")
