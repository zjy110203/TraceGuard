"""Real process termination / restart on the host filesystem, including Windows."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from packages.core.errors import ConflictError, ValidationError
from packages.execution.models import load_case
from packages.execution.repair import RepairService, RepairStore
from tests.test_repair import _base, _locator_fix


def prepared(root: Path, pid: str = "p", *, different: bool = False) -> RepairService:
    root.mkdir(exist_ok=True)
    path = root / "base.json"
    if not path.exists():
        path.write_text(json.dumps(_base().to_dict()), encoding="utf-8")
    base = load_case(path)
    svc = RepairService(root / "repairs.jsonl", case_dir=root)
    candidate = _locator_fix(base)
    if different:
        candidate = replace(candidate, title="different candidate")
    svc.create_proposal(pid, base, candidate, change_kinds=("locator_update",), diff=[])
    svc.validate(pid)
    svc.submit_for_approval(pid)
    svc.approve(pid, approver="reviewer", approval_record="r")
    evidence = {"test": "transaction protocol unit evidence"}
    digest = hashlib.sha256(json.dumps(evidence, indent=2, sort_keys=True).encode()).hexdigest()
    svc.save_replay(
        pid,
        {
            "proposal_id": pid,
            "candidate_digest": svc.case_digest(candidate),
            "base_version": 1,
            "evidence": evidence,
            "evidence_hash": digest,
            "evidence_sha256": digest,
            "status": "PASSED",
        },
    )
    return svc


def promote(svc: RepairService, pid: str = "p"):
    return svc.promote(
        pid,
        current_formal=load_case(svc.case_dir / "base.json"),
        operator="operator",
        promotion_reason="tested",
    )


def child(root: Path, stage: str, action: str = "promotion") -> subprocess.CompletedProcess:
    script = """
import os, sys
from pathlib import Path
from packages.execution.repair import RepairService
from packages.execution.models import load_case
root = Path(sys.argv[1])
svc = RepairService(root / 'repairs.jsonl', case_dir=root)
svc.fault_hook = lambda stage: os._exit(73) if stage == sys.argv[2] else None
if sys.argv[3] == 'promotion':
    svc.promote('p', current_formal=load_case(root / 'base.json'), operator='operator')
elif sys.argv[3] == 'rollback':
    svc.rollback('p', target_version=load_case(root / 'base.json'),
                 reason='undo', operator='operator')
else:
    svc.recover()
"""
    return subprocess.run(
        [sys.executable, "-c", script, str(root), stage, action],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONUTF8": "1"},
    )


@pytest.mark.parametrize(
    "stage",
    [
        "before_intent",
        "intent",
        "temp_written",
        "temp_synced",
        "replaced",
        "committed",
        "before_state",
    ],
)
def test_process_death_restart_and_repeat_recovery(tmp_path, stage):
    prepared(tmp_path)
    result = child(tmp_path, stage)
    assert result.returncode == 73, result.stderr
    svc = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    if stage in {"before_intent", "intent", "temp_written", "temp_synced"}:
        assert svc.get("p")["state"] == "APPROVED"
        assert not (tmp_path / "TC-REPAIR.v2.json").exists()
        promote(svc)
    assert svc.get("p")["state"] == "PROMOTED"
    audit = svc.get("p")["promotion_record"]
    svc.recover()
    again = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    promote(again)
    assert again.get("p")["promotion_record"] == audit
    assert len(again._transactions) == 1
    assert all(tx["status"] == "DONE" for tx in again._transactions.values())
    assert load_case(tmp_path / "TC-REPAIR.v2.json").version == 2
    assert again.get_replay("p")["status"] == "PASSED"


def test_recovery_itself_can_die(tmp_path):
    prepared(tmp_path)
    assert child(tmp_path, "replaced").returncode == 73
    # Install hook before _restore so this is a real interrupted startup recovery.
    script = """
import os, sys
from pathlib import Path
from packages.execution.repair import RepairService
class Crash(RepairService):
    def _finish_transaction(self, tx):
        os._exit(74)
Crash(Path(sys.argv[1]) / 'repairs.jsonl', case_dir=Path(sys.argv[1]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=30)
    assert result.returncode == 74
    svc = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    assert svc.get("p")["state"] == "PROMOTED"


def test_state_persist_failure_preserves_owned_file_for_recovery(tmp_path, monkeypatch):
    svc = prepared(tmp_path)
    original = svc._persist

    def persist():
        if svc.get("p")["state"] == "PROMOTED":
            raise OSError("disk state failure")
        original()

    monkeypatch.setattr(svc, "_persist", persist)
    with pytest.raises(OSError):
        promote(svc)
    assert (tmp_path / "TC-REPAIR.v2.json").exists()
    restored = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    assert restored.get("p")["state"] == "PROMOTED"


def test_mismatch_blocked_and_no_unowned_adoption(tmp_path):
    prepared(tmp_path)
    assert child(tmp_path, "intent").returncode == 73
    path = tmp_path / "TC-REPAIR.v2.json"
    path.write_text(
        json.dumps(_locator_fix(load_case(tmp_path / "base.json")).to_dict()), encoding="utf-8"
    )
    before = path.read_bytes()
    svc = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    assert next(iter(svc._transactions.values()))["status"] == "BLOCKED"
    assert svc.get("p")["state"] == "APPROVED"
    assert path.read_bytes() == before
    assert "BLOCKED" in svc.get("p")["failure_reason"]


@pytest.mark.parametrize("identical", [True, False])
def test_persisted_concurrent_cas(tmp_path, identical):
    svc = prepared(tmp_path, "a")
    svc = prepared(tmp_path, "b", different=not identical)
    barrier = Barrier(2)

    def run(pid):
        barrier.wait()
        return promote(svc, pid)[0].state

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(run, ["a", "b"]))
    assert sorted(result) == ["PROMOTED", "PROMOTION_FAILED"]
    winner = "a" if result[0] == "PROMOTED" else "b"
    loser = "b" if winner == "a" else "a"
    restored = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    assert restored.get(winner)["promotion_record"]["promotion_id"]
    assert restored.get(loser)["promotion_record"] == {}
    assert len(restored._transactions) == 1
    assert restored.get_replay(loser)["status"] == "PASSED"
    assert restored._disk_current("TC-REPAIR").version == 2


def test_jsonl_torn_tail_real_restart_preserves_bytes(tmp_path):
    svc = prepared(tmp_path)
    path = tmp_path / "repairs.jsonl"
    before = path.read_bytes()
    with path.open("ab") as f:
        f.write(b'{"proposals": [\xe4')
    script = """
from pathlib import Path
import sys
from packages.execution.repair import RepairService
p=Path(sys.argv[1]); s=RepairService(p/'repairs.jsonl', case_dir=p)
assert s.get('p')['state']=='APPROVED'
s.save_replay('p', s.get_replay('p'))
"""
    for _ in range(2):
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=30)
        assert result.returncode == 0
    assert path.read_bytes().startswith(before + b'{"proposals": [\xe4')
    assert len(list(tmp_path.glob("*.bin"))) == 1
    assert svc.get("p")["state"] == "APPROVED"


def test_jsonl_middle_corruption_fails_closed(tmp_path):
    path = tmp_path / "repairs.jsonl"
    path.write_bytes(b'{}\n{"broken"\n{}\n')
    with pytest.raises(ValidationError, match="corrupt repair history"):
        RepairStore(path).latest()


@pytest.mark.parametrize("raw", [b"not json", b'{"a":bad}', b"[]"])
def test_malformed_complete_tail_is_not_torn_write(tmp_path, raw):
    path = tmp_path / "repairs.jsonl"
    path.write_bytes(b"{}\n" + raw)
    with pytest.raises(ValidationError):
        RepairStore(path).latest()
    assert path.read_bytes() == b"{}\n" + raw


def test_completed_transaction_missing_or_modified_asset_blocks(tmp_path):
    svc = prepared(tmp_path)
    promote(svc)
    path = tmp_path / "TC-REPAIR.v2.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["title"] = "unexpected external modification"
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    svc = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    with pytest.raises(ConflictError, match="BLOCKED"):
        promote(svc)
    assert path.read_bytes() == before
    assert svc.transactions("p")[0]["status"] == "BLOCKED"


def test_create_rechecks_current_base_under_same_lock(tmp_path):
    svc = prepared(tmp_path)
    promote(svc)
    base = load_case(tmp_path / "base.json")
    with pytest.raises(ConflictError, match="VERSION_CONFLICT"):
        svc.create_proposal(
            "stale", base, _locator_fix(base), change_kinds=("locator_update",), diff=[]
        )
    assert len(svc.list()) == 1


@pytest.mark.parametrize("stage", ["intent", "replaced", "before_state"])
def test_rollback_crash_and_immutable_history(tmp_path, stage):
    svc = prepared(tmp_path)
    promote(svc)
    old = {p.name: p.read_bytes() for p in [tmp_path / "base.json", tmp_path / "TC-REPAIR.v2.json"]}
    assert child(tmp_path, stage, "rollback").returncode == 73
    svc = RepairService(tmp_path / "repairs.jsonl", case_dir=tmp_path)
    svc.rollback(
        "p", target_version=load_case(tmp_path / "base.json"), reason="undo", operator="ops"
    )
    assert svc.get("p")["state"] == "ROLLED_BACK"
    assert svc._disk_current("TC-REPAIR").version == 3
    raw = json.loads((tmp_path / "TC-REPAIR.v3.json").read_text(encoding="utf-8"))
    assert raw["rollback_of_version"] == 1
    assert svc.get("p")["promotion_record"]["rollback_id"]
    for name, data in old.items():
        assert (tmp_path / name).read_bytes() == data
    assert (
        load_case(tmp_path / "TC-REPAIR.v3.json").steps == load_case(tmp_path / "base.json").steps
    )


def test_rollback_conflict_and_concurrent_idempotency(tmp_path):
    svc = prepared(tmp_path)
    promote(svc)
    target = load_case(tmp_path / "base.json")
    barrier = Barrier(2)

    def run(_):
        barrier.wait()
        return svc.rollback("p", target_version=target, reason="undo", operator="ops").state

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(run, [1, 2])) == ["ROLLED_BACK", "ROLLED_BACK"]
    assert len(svc._transactions) == 2
    assert len(list(tmp_path.glob("TC-REPAIR.v*.json"))) == 2

    other = tmp_path / "other"
    svc = prepared(other)
    promote(svc)
    raw = json.loads((other / "TC-REPAIR.v2.json").read_text(encoding="utf-8"))
    raw["version"] = 3
    (other / "later.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConflictError, match="VERSION_CONFLICT"):
        svc.rollback("p", target_version=target, reason="undo", operator="ops")
