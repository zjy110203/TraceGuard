"""Stage 1 acceptance tests — TeamFlow 核心业务路径 + 五类故障 + Ground Truth 隔离.

These tests ARE the M0/M1 exit evidence: normal path repeatable, independent
state consistent, five faults injectable/cleanable, Ground Truth not exposed
to the business/Agent plane.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def login(client: TestClient) -> None:
    r = client.post("/api/v1/auth/login", json={"username": "tester", "password": "test-pass-123"})
    assert r.status_code == 200


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "teamflow"


def test_full_happy_path(client: TestClient, eval_headers: dict[str, str]) -> None:
    """核心业务路径: 登录 -> Workspace -> 创建 -> 成功 -> 列表 -> 详情 -> 状态一致."""
    client.post("/__eval__/data/reset", headers=eval_headers)
    assert login(client) is None

    me = client.get("/api/v1/auth/me").json()
    assert me["workspace_id"] == "ws-default"

    created = client.post("/api/v1/projects", json={"name": "Alpha", "description": "d"})
    assert created.status_code == 201
    pid = created.json()["id"]

    listed = client.get("/api/v1/projects").json()
    assert any(p["id"] == pid for p in listed)

    detail = client.get(f"/api/v1/projects/{pid}").json()
    assert detail["name"] == "Alpha"

    # Independent state query reads authoritative DB rows.
    state = client.get("/api/v1/state/independent").json()
    assert state["project_count"] == 1
    assert state["projects"][0]["id"] == pid
    assert state["source"].endswith("tf_project")


def test_business_plane_cannot_read_ground_truth(
    client: TestClient, eval_headers: dict[str, str]
) -> None:
    """Ground Truth 不暴露给 Agent：业务端点不返回 GT，eval 无 token 401."""
    login(client)
    client.post(
        "/__eval__/scenarios/S1",
        headers=eval_headers,
        json={
            "fault_id": None,
            "description": "baseline",
            "metadata": {"data_seed": "seed-1"},
            "ground_truth": {"expected_project_count": 0, "expected_verdict": "PASSED"},
        },
    )
    # Business endpoints must not leak GT.
    for path in ["/api/v1/projects", "/api/v1/state/independent", "/health"]:
        body = client.get(path).text.lower()
        assert "expected_project_count" not in body
        assert "ground_truth" not in body
    # Eval endpoints require token.
    assert client.get("/__eval__/scenarios/S1/ground-truth").status_code == 401
    gt = client.get("/__eval__/scenarios/S1/ground-truth", headers=eval_headers).json()
    assert gt["expected"]["expected_project_count"] == 0


@pytest.mark.parametrize("fid", ["TF-API-001", "TF-DB-001"])
def test_write_blocking_faults(client: TestClient, eval_headers: dict[str, str], fid: str) -> None:
    login(client)
    client.post("/__eval__/data/reset", headers=eval_headers)
    client.post(f"/__eval__/faults/{fid}/enable", headers=eval_headers)
    r = client.post("/api/v1/projects", json={"name": "ShouldFail"})
    assert r.status_code in {400, 500}
    # Critically: no row persisted.
    assert client.get("/api/v1/state/independent").json()["project_count"] == 0
    client.post(f"/__eval__/faults/{fid}/disable", headers=eval_headers)


def test_ui_fault_ui_lies_state_truthful(client: TestClient, eval_headers: dict[str, str]) -> None:
    """TF-UI-001: 成功提示缺失，但真实状态=已创建。UI 会说谎，Oracle 状态不会。"""
    login(client)
    client.post("/__eval__/data/reset", headers=eval_headers)
    client.post("/__eval__/faults/TF-UI-001/enable", headers=eval_headers)
    r = client.post("/api/v1/projects", json={"name": "UIFault"})
    assert r.status_code == 201
    html = client.get("/projects").text
    assert 'data-testid="success-message-hidden"' in html
    assert client.get("/api/v1/state/independent").json()["project_count"] == 1
    client.post("/__eval__/faults/clear", headers=eval_headers)


def test_faults_are_repeatable_and_clearable(
    client: TestClient, eval_headers: dict[str, str]
) -> None:
    """五类故障可注入、可查询、可清理、可重复。"""
    catalog = client.get("/__eval__/fault-catalog", headers=eval_headers).json()
    ids = {f["fault_id"] for f in catalog}
    assert ids == {"TF-API-001", "TF-NET-001", "TF-DB-001", "TF-UI-001", "TF-TIME-001"}

    for fid in ids:
        client.post(f"/__eval__/faults/{fid}/enable", headers=eval_headers)
    enabled = [
        f for f in client.get("/__eval__/faults", headers=eval_headers).json() if f["enabled"]
    ]
    assert len(enabled) == 5

    cleared = client.post("/__eval__/faults/clear", headers=eval_headers).json()
    assert cleared["cleared"] >= 5
    enabled_after = [
        f for f in client.get("/__eval__/faults", headers=eval_headers).json() if f["enabled"]
    ]
    assert enabled_after == []


def test_data_reset_is_repeatable(client: TestClient, eval_headers: dict[str, str]) -> None:
    login(client)
    client.post("/api/v1/projects", json={"name": "Temp"})
    assert client.get("/api/v1/state/independent").json()["project_count"] == 1
    r = client.post("/__eval__/data/reset", headers=eval_headers).json()
    assert r["projects_deleted"] == 1
    assert client.get("/api/v1/state/independent").json()["project_count"] == 0
    # Repeat reset — idempotent, test account still works.
    client.post("/__eval__/data/reset", headers=eval_headers)
    assert login(client) is None
