"""Real process death and concurrent HTTP review decisions; no model calls."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from apps.api.candidate_api import CandidateStore
from packages.core.errors import ConflictError
from packages.execution import candidate_transaction as transaction
from packages.execution.candidate_transaction import CandidateTransactions
from tests import test_candidate_api as candidate_tests
from tests.test_candidate_api import _approve, _compile


@pytest.fixture
def client(tmp_path: Path) -> Any:
    yield from candidate_tests.client.__wrapped__(tmp_path)


def _tx(client: Any) -> CandidateTransactions:
    return CandidateTransactions(
        client.app.state.candidate_store, Path(client.app.state.settings.case_dir)
    )


def _parallel(client: Any, operations: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    barrier = Barrier(len(operations))

    def call(operation: tuple[str, dict[str, Any]]) -> Any:
        barrier.wait(timeout=10)
        return client.post(operation[0], json=operation[1])

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(call, operations))


@pytest.mark.parametrize(
    "actions", [("approve", "approve"), ("approve", "reject"), ("reject", "reject")]
)
def test_review_concurrent_http_has_one_decision(client: Any, actions: tuple[str, str]) -> None:
    cand = _compile(client)
    cid = cand["candidate_id"]
    body = {
        "reviewer": "human-qa",
        "approval_record": "REV-concurrent",
        "reason": "review rejected",
        "review_confirmed": [c["id"] for c in cand["review_checklist"] if c["required"]],
        "uncertainty_acknowledged": True,
    }
    responses = _parallel(
        client, [(f"/api/v1/candidates/{cid}/{action}", body) for action in actions]
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    tx = _tx(client)
    saved = client.app.state.candidate_store.get(cid)
    journal = json.loads(tx._path(cid).read_bytes())
    assert journal["stage"] == "DONE"
    assert journal["after"] == saved
    assert len(list(tx.root.glob("*.json"))) == 1
    assert (tx.case_dir / f"{cand['case_id']}.json").exists() == (saved["status"] == "APPROVED")


def test_concurrent_compile_does_not_overwrite_review(client: Any) -> None:
    url = "/api/v1/explorations/exp_test_1/compile"
    results = _parallel(client, [(url, {"title": str(n)}) for n in range(8)])
    assert {r.status_code for r in results} == {201}
    assert len({r.json()["candidate_id"] for r in results}) == 1
    assert len({r.json()["title"] for r in results}) == 1
    cand = results[0].json()
    cid = cand["candidate_id"]
    body = {
        "reviewer": "qa-lead",
        "approval_record": "REV-race",
        "review_confirmed": [c["id"] for c in cand["review_checklist"] if c["required"]],
        "uncertainty_acknowledged": True,
    }
    responses = _parallel(client, [(url, {}), (f"/api/v1/candidates/{cid}/approve", body)])
    assert responses[0].status_code in (201, 409)
    assert responses[1].status_code == 200
    assert client.app.state.candidate_store.get(cid)["status"] == "APPROVED"


CHILD = r"""
import json, os, sys
from pathlib import Path
from apps.api.candidate_api import CandidateStore
from packages.execution import candidate_transaction as m
root, cases, stage, mode, cid = sys.argv[1:]
def crash(s):
    if s == stage: os._exit(73)
m.checkpoint = crash
store = CandidateStore(Path(root))
tx = m.CandidateTransactions(store, Path(cases))
with store.lock(cid):
    if mode == 'recover':
        tx.recover(cid)
    else:
        payload = json.loads((Path(root) / 'decision.json').read_bytes())
        tx.commit(payload['before'],payload['after'],payload['formal'])
"""


def _child(tx: CandidateTransactions, cid: str, stage: str, mode: str) -> int:
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(tx.store.root), str(tx.case_dir), stage, mode, cid],
        capture_output=True,
        timeout=30,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode in (0, 73), result.stderr.decode("utf-8", errors="replace")
    return result.returncode


@pytest.mark.parametrize(
    "stage",
    [
        "PREPARED",
        "INTENT_DURABLE",
        "FORMAL_TEMP_DURABLE",
        "FORMAL_REPLACED",
        "FORMAL_CASE_DURABLE",
        "BEFORE_CANDIDATE_STATE",
        "CANDIDATE_TEMP_DURABLE",
        "CANDIDATE_REPLACED",
        "CANDIDATE_STATE_DURABLE",
        "BEFORE_DONE",
        "DONE",
    ],
)
def test_process_exit_recovery(client: Any, monkeypatch: Any, stage: str) -> None:
    cand = _compile(client)
    cid = cand["candidate_id"]
    tx = _tx(client)

    # Capture only a decision that passed the actual HTTP human review gate.
    def capture(self: Any, before: Any, after: Any, formal: Any = None) -> None:
        (self.store.root / "decision.json").write_text(
            json.dumps({"before": before, "after": after, "formal": formal}), encoding="utf-8"
        )

    with monkeypatch.context() as patch:
        patch.setattr(CandidateTransactions, "commit", capture)
        assert _approve(client, cid).status_code == 200
    assert _child(tx, cid, stage, "commit") == 73
    if stage != "PREPARED":
        assert _child(tx, cid, "BEFORE_CANDIDATE_STATE", "recover") in (0, 73)
    assert _child(tx, cid, "never", "recover") == 0
    assert _child(tx, cid, "never", "recover") == 0
    record = tx.store.get(cid)
    files = list(tx.case_dir.glob("*.json"))
    if stage == "PREPARED":
        assert record["status"] == "PENDING_REVIEW"
        assert files == []
        assert not tx._path(cid).exists()
    else:
        journal = json.loads(tx._path(cid).read_bytes())
        assert journal["stage"] == "DONE"
        assert record == journal["after"]
        assert record["reviewer"] == "qa-lead" and record["approval_record"] == "REV-1"
        assert len(files) == 1
        assert hashlib.sha256(files[0].read_bytes()).hexdigest() == journal["formal_sha256"]
        assert (
            json.loads(files[0].read_bytes())["_candidate_transaction"] == journal["transaction_id"]
        )
        assert record["review_confirmed"] == journal["review_confirmed"]
        assert record["uncertainty_acknowledged"] is True


@pytest.mark.parametrize("damage", ["content", "owner", "missing", "candidate", "intent"])
def test_recovery_blocks_mismatch_and_keeps_files(client: Any, damage: str) -> None:
    cand = _compile(client)
    cid = cand["candidate_id"]
    assert _approve(client, cid).status_code == 200
    tx = _tx(client)
    target = tx.case_dir / f"{cand['case_id']}.json"
    if damage == "missing":
        target.unlink()
    elif damage == "candidate":
        record = tx.store.get(cid)
        record["approval_record"] = "modified"
        tx.store.save(record)
    elif damage == "intent":
        raw = json.loads(tx._path(cid).read_bytes())
        raw["after"]["reviewer"] = "agent"
        tx._path(cid).write_text(json.dumps(raw), encoding="utf-8")
    else:
        raw = json.loads(target.read_bytes())
        raw["title" if damage == "content" else "_candidate_transaction"] = "unowned"
        target.write_text(json.dumps(raw), encoding="utf-8")
    original = target.read_bytes() if target.exists() else None
    with pytest.raises(ConflictError):
        tx.recover(cid)
    assert (target.read_bytes() if target.exists() else None) == original
    with pytest.raises(ConflictError):
        tx.recover(cid)
    assert json.loads(tx._path(cid).read_bytes())["stage"] == "BLOCKED"


def test_atomic_replace_failure_preserves_old_json(tmp_path: Path, monkeypatch: Any) -> None:
    store = CandidateStore(tmp_path)
    store.save({"candidate_id": "cand_atomic", "status": "OLD"})

    def fail(*args: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(transaction.os, "replace", fail)
    with pytest.raises(OSError):
        store.save({"candidate_id": "cand_atomic", "status": "NEW"})
    assert store.get("cand_atomic")["status"] == "OLD"
    assert not list(tmp_path.glob(".*.tmp"))


def test_unfinished_intent_reserves_formal_id(client: Any, monkeypatch: Any) -> None:
    cand = _compile(client)
    cid = cand["candidate_id"]
    tx = _tx(client)

    def fail(stage: str) -> None:
        if stage == "INTENT_DURABLE":
            raise OSError("simulated disk failure after intent")

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "checkpoint", fail)
        with pytest.raises(OSError):
            _approve(client, cid)
    before = dict(tx.store.get(cid))
    before["candidate_id"] = "cand_competitor"
    after = {**before, "status": "APPROVED", "reviewer": "human-qa", "reviewed_at": "2026-11-26"}
    tx.store.save(before)
    with pytest.raises(ConflictError, match="reserved"):
        tx.commit(before, after, {"case_id": before["case_id"]})
    assert not list(tx.case_dir.glob("*.json"))
    tx.recover(cid)
    assert tx.store.get(cid)["status"] == "APPROVED"


def test_application_startup_recovers_durable_approval(client: Any, monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    from apps.api.main import build_app

    cand = _compile(client)
    cid = cand["candidate_id"]

    def fail(stage: str) -> None:
        if stage == "FORMAL_REPLACED":
            raise OSError("stopped after formal replace")

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "checkpoint", fail)
        with pytest.raises(OSError):
            _approve(client, cid)
    settings = client.app.state.settings
    with TestClient(build_app(settings)) as restarted:
        record = restarted.get(f"/api/v1/candidates/{cid}").json()
        assert record["status"] == "APPROVED"
        assert restarted.get("/health").json()["candidate_recovery"]["blocked"] == []
    assert json.loads(_tx(client)._path(cid).read_bytes())["stage"] == "DONE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_record", "tampered"),
        ("review_confirmed", []),
        ("uncertainty_acknowledged", False),
        ("created_at", "tampered"),
    ],
)
def test_recovery_cross_checks_all_review_audit_fields(client: Any, field: str, value: Any) -> None:
    cand = _compile(client)
    cid = cand["candidate_id"]
    assert _approve(client, cid).status_code == 200
    tx = _tx(client)
    raw = json.loads(tx._path(cid).read_bytes())
    raw[field] = value
    tx._path(cid).write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConflictError):
        tx.recover(cid)
    assert json.loads(tx._path(cid).read_bytes())["stage"] == "BLOCKED"
    assert tx.store.get(cid)["approval_record"] == "REV-1"
