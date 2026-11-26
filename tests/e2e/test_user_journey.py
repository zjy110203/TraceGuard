"""R5-4.8 §5.1: the USER JOURNEY E2E — real UI, real clicks, real backend.

Every step of the product loop is driven through the built SPA in real Chrome:

    打开 Explorer → 填写 TestIntent → 发起探索 → 查看轨迹与目标验证
    → 编译 Candidate → 审核页面逐项检查 Oracle / value_ref → 完成审核清单
    → 批准 → 打开正式 TestCase → 发起 FullChain → 查看 Run / 断言 /
    Evidence / Graph / Diagnosis → 负向场景。

Nothing in this file talks to the API directly for the actions under test —
the API is exercised BY the UI. (Direct API calls are used only to seed a
fault state for the negative scenario and to assert persistence afterwards.)
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, sync_playwright

from apps.api.main import build_app
from packages.core.config import Settings
from packages.execution.browser import browser_channel
from tests.netutil import release_reserved, reserve_port, take_reserved

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "apps" / "web" / "dist"


def _wait_healthy(base: str, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"{base} did not become healthy")


@pytest.fixture(scope="module")
def teamflow() -> Iterator[str]:
    port = reserve_port()
    db = Path(tempfile.mkdtemp(prefix="tg_journey_tf_")) / "teamflow.db"
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": take_reserved(port)}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(base)
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "server thread did not stop"
        release_reserved(port)
        db.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def platform(teamflow: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not (DIST / "index.html").exists():
        pytest.skip("workbench not built; run `npm run build` in apps/web")
    work = tmp_path_factory.mktemp("tg_journey")
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{(work / 'platform.db').as_posix()}",
        case_dir=str(work / "cases"),
        chain_data_dir=str(work / "chains"),
        blob_dir=str(work / "blobs"),
        candidate_data_dir=str(work / "candidates"),
        exploration_data_dir=str(work / "explorations"),
        benchmark_data_dir=str(work / "benchmark"),
        workbench_enabled=True,
        teamflow_port=int(teamflow.rsplit(":", 1)[1]),
    )
    app = build_app(settings)
    port = reserve_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": take_reserved(port)}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(base)
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "server thread did not stop"
        release_reserved(port)


@pytest.fixture(scope="module")
def page(platform: str) -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=browser_channel(), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.set_default_timeout(20000)
        yield page
        browser.close()


def _goto(page: Page, platform: str, path: str) -> None:
    page.goto(f"{platform}/#{path}", wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")


# ---------------------------------------------------------------- the journey


def test_user_journey_explore_compile_review_run(page: Page, platform: str, teamflow: str) -> None:
    # 1-2. open the Explorer and fill in the FULL TestIntent through the form
    _goto(page, platform, "/explorer")
    page.fill('[data-testid="exp-intent-ref"]', "journey:login")
    page.fill('[data-testid="exp-goal"]', "登录 TeamFlow 并进入项目列表")
    page.fill('[data-testid="exp-outcomes"]', "项目列表")
    page.fill('[data-testid="exp-target-path"]', "/projects")
    page.fill('[data-testid="exp-base-url"]', teamflow)
    page.select_option('[data-testid="exp-policy"]', "mock-agent")

    # 3. run the exploration — the button is disabled while running
    page.click("button:has-text('启动探索')")
    page.wait_for_selector('[data-testid="goal-status"]:has-text("VERIFIED")', timeout=60000)

    # 4-5. the trace view shows the three status axes separately
    detail = page.inner_text(".detail-card")
    assert "PLAN_EXHAUSTED" in detail or "GOAL_VERIFIED" in detail
    assert "VERIFIED" in detail
    traces = httpx.get(f"{platform}/api/v1/explorations", timeout=10, trust_env=False).json()
    trace = httpx.get(
        f"{platform}/api/v1/explorations/{traces[0]['exploration_id']}",
        timeout=10,
        trust_env=False,
    ).json()
    assert trace["policy"] == "mock-agent" and trace["is_real_model"] is False
    assert trace["hash_version"] == 3 and trace["trace_integrity_verified"] is True
    assert trace["scenario_id"] == "teamflow-login" and trace["scenario_digest"]
    assert trace["decision_records"]

    # 6. compile to a candidate
    page.click('[data-testid="compile-candidate"]')
    page.wait_for_selector("text=已生成候选", timeout=30000)

    # 7-8. open the review queue and the candidate detail
    page.click("text=前往审核队列")
    page.wait_for_load_state("networkidle")
    page.click('[data-testid="open-candidate"]')
    page.wait_for_selector('[data-testid="candidate-intent"]')

    # the review basis the spec demands is actually rendered
    review = page.inner_text("main")
    assert "登录 TeamFlow 并进入项目列表" in review  # goal
    assert "/projects" in review  # target
    assert "teamflow.password" in review  # value_ref / data refs
    assert page.locator('[data-testid="candidate-step"]').count() >= 4
    assert page.locator('[data-testid="candidate-oracles"] li').count() >= 1
    # 9. checklist exists and starts unchecked
    boxes = page.locator('[data-testid="review-checklist"] input[type=checkbox]')
    assert boxes.count() >= 3
    # the approve button is LOCKED until every required item is confirmed
    assert page.locator('[data-testid="approve-candidate"]').is_disabled()

    # 10. complete the checklist item by item
    for i in range(boxes.count()):
        boxes.nth(i).check()
    ack = page.locator('[data-testid="uncertainty-ack"] input[type=checkbox]')
    if ack.count():
        ack.check()

    # 11. approve with reviewer + record
    page.fill('[data-testid="reviewer"]', "journey-reviewer")
    page.fill('[data-testid="approval-record"]', "REV-JOURNEY-001")
    page.click('[data-testid="approve-candidate"]')
    page.wait_for_selector("text=已晋升为正式用例", timeout=30000)

    # 12. open the formal case
    page.click("a.nav-item:has-text('测试用例')")
    page.wait_for_load_state("networkidle")
    page.click("a:has-text('TC-CAND-')")
    page.wait_for_selector("text=断言与 Oracle 绑定")

    # 13. run the full chain from the case page (real browser run)
    page.click('[data-testid="run-chain"]')
    page.wait_for_selector("text=全链结果", timeout=180000)
    result_text = page.inner_text(".chain-result")
    assert "PASSED" in result_text, result_text

    # 14-15. run detail page from the chain result
    page.click("text=查看运行详情")
    page.wait_for_load_state("networkidle")
    run_page = page.inner_text("main")
    assert "run_" in run_page

    # 16-19. persisted events/evidence and diagnosis all bind to this same Run.
    chains = httpx.get(f"{platform}/api/v1/chains", timeout=10, trust_env=False).json()
    assert len(chains) == 1
    chain = chains[0]
    chain_detail = httpx.get(
        f"{platform}/api/v1/chains/{chain['chain_id']}", timeout=10, trust_env=False
    ).json()
    run_id = chain_detail["run_id"]
    assert run_id and run_id in run_page
    events = httpx.get(
        f"{platform}/api/v1/evidence/events",
        params={"run_id": run_id},
        timeout=10,
        trust_env=False,
    ).json()
    evidence = httpx.get(
        f"{platform}/api/v1/evidence/records",
        params={"run_id": run_id},
        timeout=10,
        trust_env=False,
    ).json()
    diagnosis = httpx.get(
        f"{platform}/api/v1/runs/{run_id}/diagnosis", timeout=10, trust_env=False
    ).json()
    graph_nodes = httpx.get(
        f"{platform}/api/v1/evidence/graph/nodes", timeout=10, trust_env=False
    ).json()
    assert events and all(item["context_refs"]["run_id"] == run_id for item in events)
    assert evidence and all(item["run_id"] == run_id for item in evidence)
    assert diagnosis["run_id"] == run_id
    assert any(item["node_id"] == f"{run_id}:Run" for item in graph_nodes)
    for nav, marker in (("证据台账", "证据"), ("证据图谱", "节点")):
        page.click(f"text={nav}")
        page.wait_for_load_state("networkidle")
        if nav == "证据图谱":
            page.wait_for_function(
                "document.body.innerText.includes('当前显示') || "
                "document.body.innerText.includes('图中还没有节点')",
                timeout=10000,
            )
        else:
            assert marker in page.inner_text("main")
    page.goto(f"{platform}/#/diagnosis", wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.fill("#diag-run", run_id)
    page.click("button:has-text('运行诊断')")
    page.wait_for_selector(".meta")
    assert run_id in page.inner_text("main")


def test_negative_scenario_without_goal_cannot_compile(
    page: Page, platform: str, teamflow: str
) -> None:
    """E2E negative: an exploration with NO declared goal cannot be compiled."""
    _goto(page, platform, "/explorer")
    page.fill('[data-testid="exp-intent-ref"]', "journey:no-goal")
    page.fill('[data-testid="exp-goal"]', "")
    page.fill('[data-testid="exp-outcomes"]', "")
    page.fill('[data-testid="exp-target-path"]', "")
    page.fill('[data-testid="exp-base-url"]', teamflow)
    page.click("button:has-text('启动探索')")
    page.wait_for_selector('[data-testid="goal-status"]:has-text("NOT_EVALUATED")', timeout=60000)

    compile_btn = page.locator('[data-testid="compile-candidate"]')
    assert compile_btn.is_disabled()
    assert "未验证" in page.inner_text(".compile-bar")

    # and the server enforces the same rule even for a crafted request
    resp = httpx.post(
        f"{platform}/api/v1/explorations",
        json={"intent_ref": "api-no-goal", "base_url": teamflow, "policy": "scripted"},
        timeout=90,
        trust_env=False,
    )
    assert resp.status_code == 201  # the exploration itself is allowed
    exp = resp.json()
    compile_resp = httpx.post(
        f"{platform}/api/v1/explorations/{exp['exploration_id']}/compile",
        json={},
        timeout=30,
        trust_env=False,
    )
    assert compile_resp.status_code == 409
    assert "scenario test-case verdict does not admit compilation" in compile_resp.json()["error"][
        "message"
    ]


def test_unsupported_scenario_is_rejected_by_the_api(platform: str) -> None:
    resp = httpx.post(
        f"{platform}/api/v1/explorations",
        json={
            "intent_ref": "x",
            "scenario": "checkout-flow",
            "base_url": "http://127.0.0.1:8001",
            "policy": "scripted",
        },
        timeout=30,
        trust_env=False,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["details"]["code"] == "UNSUPPORTED_SCENARIO"


def test_state_after_the_journey_is_persisted(platform: str) -> None:
    """The journey left real artifacts behind: candidates, case, chains."""
    candidates = httpx.get(f"{platform}/api/v1/candidates", timeout=10, trust_env=False).json()
    assert any(c["status"] == "APPROVED" for c in candidates)
    cases = httpx.get(f"{platform}/api/v1/cases", timeout=10, trust_env=False).json()
    assert cases, "the approved candidate became a formal case"
    chains = httpx.get(f"{platform}/api/v1/chains", timeout=10, trust_env=False).json()
    assert any(c.get("completed") for c in chains)
    approved = next(c for c in chains if c.get("completed"))
    assert approved.get("persistence_status") in {"OK", ""}
