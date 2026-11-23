"""Explorer REST surface: policy honesty, safety gates and trace redaction.

These tests deliberately avoid launching a browser. The guarded execution loop
itself is covered by ``tests/test_llm_exploration.py``; here we pin the contract
the UI relies on:

* the LLM policy is never advertised as available;
* non-local targets are rejected before any browser starts;
* credential-shaped tool arguments are masked before a trace can be read.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.explorer_api import REDACTED, redact_actions
from apps.api.main import build_app
from packages.core.config import Settings


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        app_env="test",
        database_url="sqlite:///:memory:",
        exploration_data_dir=str(tmp_path / "explorations"),
        benchmark_data_dir=str(tmp_path / "benchmark"),
    )
    app = build_app(settings)
    with TestClient(app) as c:
        yield c


def test_policies_state_llm_is_not_available(client: TestClient) -> None:
    body = client.get("/api/v1/explorations/policies").json()
    by_id = {p["id"]: p for p in body["policies"]}

    assert by_id["scripted"]["verified"] is True
    assert by_id["scripted"]["is_llm"] is False
    # The LLM policy is declared but explicitly unverified: no fake capability.
    assert by_id["llm"]["is_llm"] is True
    assert by_id["llm"]["verified"] is False
    assert body["active_policy"] == "scripted"
    assert body["llm_blocked_reason"]


def test_llm_policy_is_refused_with_conflict(client: TestClient) -> None:
    resp = client.post("/api/v1/explorations", json={"policy": "llm"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize(
    "url", ["http://example.com/", "https://evil.test/x", "file:///etc/passwd"]
)
def test_non_local_target_is_rejected(client: TestClient, url: str) -> None:
    resp = client.post(
        "/api/v1/explorations",
        json={"policy": "scripted", "base_url": url},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_step_budget_is_validated(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/explorations",
        json={"policy": "scripted", "base_url": "http://127.0.0.1:8001", "max_steps": 999},
    )
    assert resp.status_code == 422


def test_list_is_empty_before_any_run(client: TestClient) -> None:
    assert client.get("/api/v1/explorations").json() == []


def test_unknown_exploration_is_404(client: TestClient) -> None:
    resp = client.get("/api/v1/explorations/exp_deadbeef")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_path_like_id_cannot_escape_the_store(client: TestClient) -> None:
    # A traversal-shaped id must not read outside the exploration directory.
    assert client.get("/api/v1/explorations/exp_..%2F..%2Fsecret").status_code == 404


def test_redact_actions_masks_credentials_only() -> None:
    actions = [
        {"tool": "fill", "args": {"selector": "[data-testid=password]", "value": "hunter2"}},
        {"tool": "fill", "args": {"selector": "[data-testid=username]", "value": "tester"}},
        {"tool": "navigate", "args": {"url": "http://127.0.0.1:8001/"}},
        {"tool": "fill", "args": {"selector": "#x", "value": "abc", "token": "t-123"}},
    ]
    clean, masked = redact_actions(actions)

    assert masked == 2
    # The password field's *value* key is sensitive -> masked.
    assert clean[0]["args"]["value"] == REDACTED
    # Non-sensitive args survive untouched, so the trace stays useful.
    assert clean[1]["args"]["value"] == "tester"
    assert clean[2]["args"]["url"] == "http://127.0.0.1:8001/"
    assert clean[3]["args"]["token"] == REDACTED
    assert clean[3]["args"]["selector"] == "#x"


def test_redact_actions_is_idempotent() -> None:
    actions = [{"tool": "fill", "args": {"password": "x"}}]
    once, n1 = redact_actions(actions)
    twice, n2 = redact_actions(once)
    assert n1 == 1
    assert twice[0]["args"]["password"] == REDACTED
    # Re-redacting an already-masked value does not double-count it.
    assert n2 == 1
