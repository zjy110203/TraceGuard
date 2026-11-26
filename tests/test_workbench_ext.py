"""Workbench extension endpoints: real data, honest empties, gated approval.

These tests pin the second-round contract between the Vue workbench and the
backend: every page must be able to load from a real endpoint, empty collections
must stay empty rather than be filled with demo rows, and repair approval must be
refused when the caller tries to approve its own change.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import build_app
from packages.core.config import Settings

CASES_SRC = Path(__file__).resolve().parents[1] / "benchmark" / "cases"


@pytest.fixture()
def client(tmp_path: Path) -> Any:
    """App wired to a throwaway case dir so intent writes never touch the repo."""
    cases = tmp_path / "cases"
    cases.mkdir()
    for name in ("TC-CREATE-001.json", "TC-CREATE-002.json", "locators.json"):
        src = CASES_SRC / name
        if src.exists():
            shutil.copy(src, cases / name)

    db = os.path.join(tempfile.gettempdir(), f"tg_ext_{uuid.uuid4().hex[:8]}.db")
    app = build_app(
        Settings(
            app_env="test",
            case_dir=str(cases),
            database_url="sqlite:///" + db.replace("\\", "/"),
            # benchmark store must be empty for the start-empty assertions
            benchmark_data_dir=str(tmp_path / "bdata"),
        )
    )
    app.state.case_dir = cases
    with TestClient(app) as c:
        yield c
    Path(db).unlink(missing_ok=True)


def test_overview_counts_real_state(client: Any) -> None:
    data = client.get("/api/v1/overview").json()
    assert data["tasks"] == 0 and data["runs"] == 0
    assert data["cases"] >= 2
    assert isinstance(data["runs_by_status"], dict)
    assert data["repairs_pending"] == 0


def test_intents_are_derived_from_case_files(client: Any) -> None:
    rows = client.get("/api/v1/intents").json()
    assert rows, "cases carry intents, so the list cannot be empty"
    assert all(r["origin"] == "case" for r in rows)
    assert all(r["case_id"] for r in rows)


def test_intent_creation_persists_and_deduplicates(client: Any) -> None:
    body = {"intent_id": "INT-T1", "description": "created through the API"}
    first = client.post("/api/v1/intents", json=body)
    assert first.status_code == 201
    assert client.post("/api/v1/intents", json=body).status_code in (409, 400)

    ids = {r["intent_id"] for r in client.get("/api/v1/intents").json()}
    assert "INT-T1" in ids


def test_intent_requires_both_fields(client: Any) -> None:
    assert (
        client.post("/api/v1/intents", json={"intent_id": "x", "description": ""}).status_code
        == 422
    )


def test_case_detail_exposes_steps_assertions_and_versions(client: Any) -> None:
    detail = client.get("/api/v1/cases/TC-CREATE-001").json()
    assert detail["case_id"] == "TC-CREATE-001"
    assert detail["steps"] and detail["assertions"] and detail["oracle_bindings"]
    assert detail["source_file"].endswith(".json")
    assert detail["versions"][0]["version"] == detail["version"]


def test_unknown_case_is_404_not_empty_document(client: Any) -> None:
    assert client.get("/api/v1/cases/NOPE-404").status_code == 404


def test_probe_registry_covers_the_gateway_whitelist(client: Any) -> None:
    rows = client.get("/api/v1/probes").json()
    from packages.agents.probes import READ_ONLY_PROBES

    assert {r["probe_id"] for r in rows} == set(READ_ONLY_PROBES)
    assert all(r["read_only"] is True for r in rows)
    assert all(r["risk_level"] in {"LOW", "MEDIUM"} for r in rows)


def test_probe_audit_starts_empty_and_is_not_faked(client: Any) -> None:
    assert client.get("/api/v1/probes/audit").json() == []


def test_repairs_and_benchmark_start_empty(client: Any) -> None:
    assert client.get("/api/v1/repairs").json() == []
    assert client.get("/api/v1/benchmark/datasets").json() == []
    assert client.get("/api/v1/benchmark/experiments").json() == []


def test_diagnosis_endpoint_requires_an_existing_run(client: Any) -> None:
    assert client.get("/api/v1/runs/run-does-not-exist/diagnosis").status_code == 404


def test_real_run_diagnosis_is_the_only_repair_evidence_source(tmp_path: Path) -> None:
    from apps.teamflow.main import build_app as build_teamflow_app
    from tests.netutil import serve_uvicorn

    case_id = "TC-CREATE-001"
    cases = tmp_path / "cases"
    cases.mkdir()
    for filename in (f"{case_id}.json", "locators.json"):
        shutil.copy(CASES_SRC / filename, cases / filename)
    case_path = cases / f"{case_id}.json"
    formal = json.loads(case_path.read_text(encoding="utf-8"))
    drifted = json.loads(json.dumps(formal))
    step = next(item for item in drifted["steps"] if item["step_id"] == "s6")
    step["target"] = "[data-testid=traceguard-missing-project-link]"
    case_path.write_text(json.dumps(drifted), encoding="utf-8")

    teamflow_settings = Settings(
        app_env="test", database_url=f"sqlite:///{(tmp_path / 'teamflow.db').as_posix()}"
    )
    with serve_uvicorn(build_teamflow_app(teamflow_settings)) as teamflow_url:
        teamflow_port = int(teamflow_url.rsplit(":", 1)[1])
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
            teamflow_port=teamflow_port,
            case_dir=str(cases),
            benchmark_data_dir=str(tmp_path / "benchmark"),
            exploration_data_dir=str(tmp_path / "explorations"),
            candidate_data_dir=str(tmp_path / "candidates"),
            chain_data_dir=str(tmp_path / "chains"),
            blob_dir=str(tmp_path / "blobs"),
        )
        app = build_app(settings)
        with TestClient(app) as client:
            created_task = client.post(
                "/api/v1/tasks",
                json={
                    "task_id": "task-real-diagnosis",
                    "case_id": case_id,
                    "title": "real diagnosis",
                },
            )
            assert created_task.status_code == 201, created_task.text
            submitted = client.post(
                "/api/v1/tasks/task-real-diagnosis/runs", json={"timeout_s": 30}
            )
            assert submitted.status_code == 202, submitted.text
            run_id = submitted.json()["run_id"]
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                run = client.get(f"/api/v1/runs/{run_id}").json()
                if run.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.1)
            report = client.get(f"/api/v1/runs/{run_id}/report").json()
            assert report.get("test_verdict") == "FAILED", report
            diagnosis_response = client.get(f"/api/v1/runs/{run_id}/diagnosis")
            assert diagnosis_response.status_code == 200, diagnosis_response.text
            diagnosis = diagnosis_response.json()
            supported = next(
                (
                    item
                    for item in diagnosis["hypotheses"]
                    if item["status"] in {"SUPPORTED", "CONFIRMED"}
                ),
                None,
            )
            assert supported is not None, diagnosis
            evidence_refs = [item["ref_id"] for item in supported["supporting"]]
            assert evidence_refs

            candidate = json.loads(json.dumps(formal))
            candidate["version"] = formal["version"] + 1
            original_step = next(item for item in candidate["steps"] if item["step_id"] == "s6")
            original_step["target"] = next(
                item for item in formal["steps"] if item["step_id"] == "s6"
            )["target"]
            proposal = client.post(
                "/api/v1/repairs",
                json={
                    "case_id": case_id,
                    "candidate": candidate,
                    "change_kinds": ["locator_update"],
                    "diff": [],
                    "source_run_id": run_id,
                    "hypothesis_id": supported["hypothesis_id"],
                    "evidence_refs": evidence_refs,
                },
            )
            assert proposal.status_code == 201, proposal.text
            assert proposal.json()["source_run_id"] == run_id
            assert proposal.json()["source_hypothesis_id"] == supported["hypothesis_id"]


def test_self_approval_of_repair_is_refused(client: Any) -> None:
    resp = client.post(
        "/api/v1/repairs/rp-1/approve",
        json={"approver": "agent", "approval_record": "looks fine"},
    )
    assert resp.status_code == 422
    assert "human" in resp.json()["error"]["message"]


@pytest.mark.parametrize(
    "body", [{"reason": "insufficient evidence"}, {"reason": "x", "reviewer": ""}]
)
def test_repair_reject_requires_explicit_reviewer(client: Any, body: dict[str, str]) -> None:
    response = client.post("/api/v1/repairs/rp-1/reject", json=body)
    assert response.status_code == 422


def test_approval_requires_a_record(client: Any) -> None:
    resp = client.post(
        "/api/v1/repairs/rp-1/approve",
        json={"approver": "alice", "approval_record": "  "},
    )
    assert resp.status_code == 422


def test_repair_api_requires_supported_evidence_and_advances_review_state(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apps.api.extensions as extensions

    monkeypatch.setattr(
        extensions,
        "run_diagnosis",
        lambda _run_id, _request: {
            "hypotheses": [
                {
                    "hypothesis_id": "hyp-1",
                    "status": "SUPPORTED",
                    "supporting": [{"ref_id": "ev-1"}],
                }
            ]
        },
    )
    formal_response = client.get("/api/v1/repairs/cases/TC-CREATE-001/formal")
    assert formal_response.status_code == 200
    candidate = formal_response.json()["case"]
    candidate["version"] += 1
    payload = {
        "case_id": "TC-CREATE-001",
        "candidate": candidate,
        "change_kinds": ["locator_update"],
        "diff": [],
        "source_run_id": "run-1",
        "hypothesis_id": "hyp-1",
        "evidence_refs": ["ev-1"],
    }
    unsupported = {**payload, "evidence_refs": ["ev-unrelated"]}
    assert client.post("/api/v1/repairs", json=unsupported).status_code == 422

    created = client.post("/api/v1/repairs", json=payload)
    assert created.status_code == 201, created.text
    proposal_id = created.json()["proposal_id"]
    from packages.execution.repair import RepairService

    store_path = client.app.state.repair_service.store_path
    assert store_path is not None
    client.app.state.repair_service = RepairService(store_path)
    assert client.get(f"/api/v1/repairs/{proposal_id}").json()["state"] == "DRAFT"
    assert created.json()["source_run_id"] == "run-1"
    assert created.json()["source_evidence_refs"] == ["ev-1"]
    assert client.post(f"/api/v1/repairs/{proposal_id}/validate").json()["state"] == "VALIDATED"
    assert (
        client.post(f"/api/v1/repairs/{proposal_id}/submit").json()["state"] == "PENDING_APPROVAL"
    )
    denied = client.post(
        f"/api/v1/repairs/{proposal_id}/approve",
        json={"approver": "system", "approval_record": "auto"},
    )
    assert denied.status_code == 422
    approved = client.post(
        f"/api/v1/repairs/{proposal_id}/approve",
        json={"approver": "reviewer@example.test", "approval_record": "review-42"},
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "APPROVED"


def test_repair_api_replays_and_promotes_append_only_version(
    client: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import apps.api.extensions as extensions

    case_id = "TC-CREATE-001"
    case_path = client.app.state.case_dir / f"{case_id}.json"
    formal = json.loads(case_path.read_text(encoding="utf-8"))
    drifted = json.loads(json.dumps(formal))
    for step in drifted["steps"]:
        if step["step_id"] == "s6":
            step["target"] = "[data-testid=traceguard-missing-project-link]"
    case_path.write_text(json.dumps(drifted), encoding="utf-8")

    monkeypatch.setattr(
        extensions,
        "run_diagnosis",
        lambda _run_id, _request: {
            "hypotheses": [
                {
                    "hypothesis_id": "hyp-repair",
                    "status": "SUPPORTED",
                    "supporting": [{"ref_id": "ev-repair"}],
                }
            ]
        },
    )
    candidate = json.loads(json.dumps(formal))
    candidate["version"] = formal["version"] + 1
    payload = {
        "case_id": case_id,
        "candidate": candidate,
        "change_kinds": ["locator_update"],
        "diff": [
            {
                "kind": "locator_update",
                "target": "s6",
                "before": "[data-testid=traceguard-missing-project-link]",
                "after": "[data-testid=new-project-link]",
            }
        ],
        "source_run_id": "run-repair",
        "hypothesis_id": "hyp-repair",
        "evidence_refs": ["ev-repair"],
    }
    created = client.post("/api/v1/repairs", json=payload)
    assert created.status_code == 201, created.text
    proposal_id = created.json()["proposal_id"]
    assert client.post(f"/api/v1/repairs/{proposal_id}/validate").json()["state"] == "VALIDATED"
    assert (
        client.post(f"/api/v1/repairs/{proposal_id}/submit").json()["state"] == "PENDING_APPROVAL"
    )
    assert (
        client.post(
            f"/api/v1/repairs/{proposal_id}/approve",
            json={"approver": "repair-reviewer", "approval_record": "review-1"},
        ).status_code
        == 200
    )
    from apps.teamflow.main import build_app as build_teamflow
    from tests.netutil import serve_uvicorn

    target = build_teamflow(
        Settings(app_env="test", database_url=f"sqlite:///{(tmp_path / 'sut.db').as_posix()}")
    )
    with serve_uvicorn(target) as base_url:
        client.app.state.teamflow_base_url = base_url
        replay = client.post(f"/api/v1/repairs/{proposal_id}/sandbox-replay")
    assert replay.status_code == 200, replay.text
    assert replay.json()["sandbox_replay"]["status"] == "PASSED"
    promoted = client.post(
        f"/api/v1/repairs/{proposal_id}/promote",
        json={"operator": "release-operator", "reason": "verified browser repair"},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["state"] == "PROMOTED"
    assert (client.app.state.case_dir / f"{case_id}.v2.json").exists()
    detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert detail["version"] == 2
    assert detail["versions"][-1]["file"] == f"{case_id}.v2.json"
    repeated = client.post(
        f"/api/v1/repairs/{proposal_id}/promote",
        json={"operator": "release-operator", "reason": "retry"},
    )
    assert repeated.json()["promotion_record"] == promoted.json()["promotion_record"]
    for actor in ("", "agent", "system", "mock"):
        refused = client.post(
            f"/api/v1/repairs/{proposal_id}/rollback",
            json={"target_version": 1, "operator": actor, "reason": "undo"},
        )
        assert refused.status_code == 422
    rolled = client.post(
        f"/api/v1/repairs/{proposal_id}/rollback",
        json={"target_version": 1, "operator": "release-operator", "reason": "undo"},
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["state"] == "ROLLED_BACK"
    assert client.get(f"/api/v1/cases/{case_id}").json()["version"] == 3
    assert rolled.json()["promotion_record"]["rollback_id"]
    assert (client.app.state.case_dir / f"{case_id}.v2.json").exists()


def test_dispatch_status_exposes_broker_semantics(client: Any) -> None:
    data = client.get("/api/v1/dispatch/status").json()
    assert set(data["broker"]) == {"ready", "inflight", "dead"}
    assert data["dead_letters"] == []
    assert data["workers"] >= 1
    assert "handled" in data["dispatcher"]


def test_repair_http_race_persisted_audit_and_same_candidate(client: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from packages.execution.models import load_case
    from packages.execution.repair import RepairService
    from tests.test_repair_transactions import prepared

    root = client.app.state.case_dir
    prepared(root, "a")
    service = prepared(root, "b")
    client.app.state.repair_service = service
    barrier = Barrier(2)

    def run(pid):
        barrier.wait()
        return client.post(
            f"/api/v1/repairs/{pid}/promote", json={"operator": "ops", "reason": "race"}
        )

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, ["a", "b"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = "a" if results[0].status_code == 200 else "b"
    loser = "b" if winner == "a" else "a"
    restored = RepairService(root / "repairs.jsonl", case_dir=root)
    assert restored.get(winner)["state"] == "PROMOTED"
    assert restored.get(loser)["state"] == "PROMOTION_FAILED"
    assert restored.get(loser)["promotion_record"] == {}
    assert restored.get_replay(loser)["status"] == "PASSED"
    assert restored._disk_current("TC-REPAIR").version == 2
    assert load_case(root / "TC-REPAIR.v2.json").steps == restored._candidates[winner].steps
    audit = client.get("/api/v1/repairs/audit").json()
    assert sum(bool(row["promotion_record"]) for row in audit) == 1
    assert sum(len(row["transactions"]) for row in audit) == 1
    again = client.post(
        f"/api/v1/repairs/{winner}/promote", json={"operator": "ops", "reason": "retry"}
    )
    assert again.json()["promotion_record"] == restored.get(winner)["promotion_record"]
    assert client.get("/api/v1/repairs/audit").json() == audit


def test_rollback_file_failure_retries_durable_intent(client: Any, monkeypatch) -> None:
    import packages.execution.repair as repair
    from tests.test_repair_transactions import prepared, promote

    svc = prepared(client.app.state.case_dir)
    client.app.state.repair_service = svc
    promote(svc)
    before = (svc.case_dir / "TC-REPAIR.v2.json").read_bytes()

    def fail_replace(*args):
        raise OSError("injected Windows replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(repair.os, "replace", fail_replace)
        response = client.post(
            "/api/v1/repairs/p/rollback",
            json={"target_version": 1, "operator": "ops", "reason": "undo"},
        )
        assert response.status_code == 422
    assert svc.get("p")["state"] == "PROMOTED"
    assert (svc.case_dir / "TC-REPAIR.v2.json").read_bytes() == before
    assert not (svc.case_dir / "TC-REPAIR.v3.json").exists()
    assert svc.transactions("p")[-1]["status"] == "INTENT"
    response = client.post(
        "/api/v1/repairs/p/rollback",
        json={"target_version": 1, "operator": "ops", "reason": "undo"},
    )
    assert response.status_code == 200, response.text
    assert svc._disk_current("TC-REPAIR").version == 3


def test_register_workbench_publishes_eval_plane_settings(client: Any) -> None:
    """Regression guard: diagnosis needs these keys or it silently loses fault evidence."""
    state = client.app.state
    assert getattr(state, "teamflow_base_url", None), "teamflow_base_url not published"
    assert getattr(state, "eval_token", None), "eval_token not published"


def test_fault_event_window_keeps_arming_but_drops_history() -> None:
    """Faults are armed BEFORE a run; a strict window would hide the ARMED entry
    and silently demote every UI-fault diagnosis to PROBABLE."""
    from apps.api.extensions import _in_window

    start = "2026-11-22T11:49:34.300599+00:00"
    finish = "2026-11-22T11:49:35.900000+00:00"

    armed = {"occurred_at": "2026-11-22T11:49:33.000000"}  # 1.3s before start
    assert _in_window(armed, start, finish) is True

    historical = {"occurred_at": "2026-11-22T08:00:00.000000"}  # hours earlier
    assert _in_window(historical, start, finish) is False

    during = {"occurred_at": "2026-11-22T11:49:35.000000"}
    assert _in_window(during, start, finish) is True

    unparseable = {"occurred_at": ""}
    assert _in_window(unparseable, start, finish) is True  # keep, never silently drop
