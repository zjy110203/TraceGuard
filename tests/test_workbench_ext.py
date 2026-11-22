"""Workbench extension endpoints: real data, honest empties, gated approval.

These tests pin the second-round contract between the Vue workbench and the
backend: every page must be able to load from a real endpoint, empty collections
must stay empty rather than be filled with demo rows, and repair approval must be
refused when the caller tries to approve its own change.
"""

from __future__ import annotations

import os
import shutil
import tempfile
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


def test_self_approval_of_repair_is_refused(client: Any) -> None:
    resp = client.post(
        "/api/v1/repairs/rp-1/approve",
        json={"approver": "agent", "approval_record": "looks fine"},
    )
    assert resp.status_code == 422
    assert "human" in resp.json()["error"]["message"]


def test_approval_requires_a_record(client: Any) -> None:
    resp = client.post(
        "/api/v1/repairs/rp-1/approve",
        json={"approver": "alice", "approval_record": "  "},
    )
    assert resp.status_code == 422


def test_dispatch_status_exposes_broker_semantics(client: Any) -> None:
    data = client.get("/api/v1/dispatch/status").json()
    assert set(data["broker"]) == {"ready", "inflight", "dead"}
    assert data["dead_letters"] == []
    assert data["workers"] >= 1
    assert "handled" in data["dispatcher"]


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
