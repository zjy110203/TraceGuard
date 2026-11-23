"""Trace -> Compiler -> Candidate -> human review -> formal case.

No browser and no LLM: the exploration trace is written straight into the
explorer store, so these tests pin the pipeline contract rather than execution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import build_app
from packages.core.config import Settings

TRACE_ACTIONS = [
    {"tool": "navigate", "args": {"url": "http://127.0.0.1:8001/"}, "rejected": False},
    {
        "tool": "fill",
        "args": {"selector": "loc:login.username", "value": "tester"},
        "rejected": False,
    },
    {
        "tool": "fill",
        "args": {
            "selector": "loc:login.password",
            "value": "«redacted»",
            "value_ref": "teamflow.password",
            "redacted": True,
        },
        "rejected": False,
    },
    {"tool": "click", "args": {"selector": "loc:login.submit"}, "rejected": False},
    {"tool": "click", "args": {"selector": "https://evil.test/x"}, "rejected": True},
]

# R5-4.1: a compilable trace must carry the structured page states, otherwise
# the compiler has nothing to justify a business assertion with.
TRACE_OBSERVATIONS = [
    {
        "url": "http://127.0.0.1:8001/",
        "url_path": "/",
        "title": "TeamFlow 登录",
        "visible_testids": ["username", "password", "login-submit"],
        "text_excerpt": "TeamFlow 受控被测应用",
        "state_summary": "login",
        "step_index": 1,
        "phase": "pre",
    },
    {
        "url": "http://127.0.0.1:8001/projects",
        "url_path": "/projects",
        "title": "项目列表 · TeamFlow",
        "visible_testids": ["new-project-link", "project-list", "project-item"],
        "text_excerpt": "项目列表",
        "state_summary": "projects",
        "step_index": 4,
        "phase": "pre",
    },
]

TRACE_INTENT = {
    "intent_id": "i-1",
    "description": "登录 TeamFlow 并进入项目列表",
    "source_ref": "i-1",
    "goal": "登录 TeamFlow 并进入项目列表",
    "expected_outcomes": [],
    "target": "/projects",
    "required_assertions": [],
    "constraints": [],
}


def _trace_hash(actions: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"exploration_id": "exp_test_1", "action_sequence": actions, "stop_reason": "GOAL_REACHED"},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    explor_dir = tmp_path / "explorations"
    explor_dir.mkdir(parents=True, exist_ok=True)
    (explor_dir / "exp_test_1.json").write_text(
        json.dumps(
            {
                "exploration_id": "exp_test_1",
                "test_intent_ref": "i-1",
                "application_version": "r5",
                "browser_version": "chrome",
                "agent_version": "scripted-policy-1.0.0-NOT-LLM",
                "prompt_version": "none",
                "tool_version": "1.0.0",
                "action_sequence": TRACE_ACTIONS,
                "observations": TRACE_OBSERVATIONS,
                "test_intent": TRACE_INTENT,
                "page_state_ref": "",
                "event_refs": [],
                "evidence_refs": [],
                "stop_reason": "GOAL_REACHED",
                "source_trace_hash": _trace_hash(TRACE_ACTIONS),
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:00:10+00:00",
                "status": "COMPLETED",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    case_dir = tmp_path / "cases"
    case_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        case_dir=str(case_dir),
        exploration_data_dir=str(explor_dir),
        benchmark_data_dir=str(tmp_path / "benchmark"),
        chain_data_dir=str(tmp_path / "chains"),
        blob_dir=str(tmp_path / "blobs"),
        candidate_data_dir=str(tmp_path / "candidates"),
    )
    with TestClient(build_app(settings)) as c:
        yield c


def _compile(client: TestClient, **body: Any) -> dict[str, Any]:
    resp = client.post("/api/v1/explorations/exp_test_1/compile", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_compile_produces_pending_candidate(client: TestClient) -> None:
    cand = _compile(client)
    assert cand["status"] == "PENDING_REVIEW"
    assert cand["case_id"].startswith("TC-CAND-")
    assert cand["is_llm"] is False
    # Rejected actions never become steps; the four explored actions do, and the
    # compiler adds assertion steps derived from the observed page states.
    detail = client.get(f"/api/v1/candidates/{cand['candidate_id']}").json()
    explored = [s for s in detail["steps"] if s["action"] in {"navigate", "fill", "click"}]
    assert len(explored) == 4
    assert cand["step_count"] > 4
    assert any(s["action"].startswith("assert") for s in detail["steps"])
    # Every assertion names its evidence, and none of them is "a click passed".
    assert cand["oracle_sources"]
    assert all(o["source"] for o in cand["oracle_sources"])
    assert not any(o["source_ref"].startswith("ui.s4.") for o in cand["oracle_sources"])
    assert cand["uncertainty_notes"]


def test_compiled_candidate_is_not_written_to_case_library(client: TestClient) -> None:
    cand = _compile(client)
    assert client.get("/api/v1/cases").json() == []
    detail = client.get(f"/api/v1/candidates/{cand['candidate_id']}").json()
    # The Oracle stays unapproved until a human signs off.
    assert detail["oracle_bindings"][0]["primary"]["approved"] is False
    assert detail["source_exploration_id"] == "exp_test_1"


def test_compile_rejects_unknown_exploration(client: TestClient) -> None:
    assert client.post("/api/v1/explorations/exp_missing/compile", json={}).status_code == 404


def test_compile_rejects_bad_case_id(client: TestClient) -> None:
    resp = client.post("/api/v1/explorations/exp_test_1/compile", json={"case_id": "not a case id"})
    assert resp.status_code == 422


def test_approve_requires_reviewer_and_record(client: TestClient) -> None:
    cand = _compile(client)
    url = f"/api/v1/candidates/{cand['candidate_id']}/approve"
    assert client.post(url, json={"reviewer": "", "approval_record": "x"}).status_code == 422
    assert client.post(url, json={"reviewer": "qa", "approval_record": ""}).status_code == 422


def test_approve_creates_formal_case_with_approved_oracle(client: TestClient) -> None:
    cand = _compile(client, case_id="TC-CAND-001", title="compiled login")
    resp = client.post(
        f"/api/v1/candidates/{cand['candidate_id']}/approve",
        json={"reviewer": "qa-lead", "approval_record": "REV-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED"

    cases = client.get("/api/v1/cases").json()
    assert [c["case_id"] for c in cases] == ["TC-CAND-001"]
    assert cases[0]["approved_by"].startswith("qa-lead")
    assert cases[0]["compiled_from"].startswith("compiler:")

    detail = client.get("/api/v1/cases/TC-CAND-001").json()
    assert detail["oracle_bindings"][0]["primary"]["approved"] is True
    assert detail["oracle_bindings"][0]["primary"]["approved_by"] == "qa-lead"


def test_approve_is_single_use(client: TestClient) -> None:
    cand = _compile(client)
    url = f"/api/v1/candidates/{cand['candidate_id']}/approve"
    body = {"reviewer": "qa", "approval_record": "REV-1"}
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json=body).status_code == 409


def test_reject_closes_candidate_without_writing_case(client: TestClient) -> None:
    cand = _compile(client)
    resp = client.post(
        f"/api/v1/candidates/{cand['candidate_id']}/reject",
        json={"reviewer": "qa", "reason": "locator too brittle"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    assert client.get("/api/v1/cases").json() == []


def test_recompile_after_review_is_refused(client: TestClient) -> None:
    """A review decision must not be erasable by re-running the compiler."""
    cand = _compile(client)
    client.post(
        f"/api/v1/candidates/{cand['candidate_id']}/reject",
        json={"reviewer": "qa", "reason": "brittle selector"},
    )
    resp = client.post("/api/v1/explorations/exp_test_1/compile", json={})
    assert resp.status_code == 409
    stored = client.get(f"/api/v1/candidates/{cand['candidate_id']}").json()
    assert stored["status"] == "REJECTED"
    assert stored["rejection_reason"] == "brittle selector"


def test_recompile_pending_candidate_is_idempotent(client: TestClient) -> None:
    first = _compile(client)
    second = _compile(client)
    assert first["candidate_id"] == second["candidate_id"]
    assert len(client.get("/api/v1/candidates").json()) == 1


def test_candidate_store_rejects_path_traversal(client: TestClient) -> None:
    assert client.get("/api/v1/candidates/../../etc/passwd").status_code in (404, 400)
