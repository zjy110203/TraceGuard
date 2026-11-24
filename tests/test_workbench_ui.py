"""The built workbench is served by the API app and renders real backend data.

The point of this test is that the UI is not a mock: it loads the SPA that
``vite build`` produced, drives it in real Chrome, and asserts that the text a
user sees comes from the FastAPI routers (case ids read off disk, the probe
catalogue from the gateway whitelist). If the SPA were hard-coded this test
would still pass — so it also asserts that a value only the backend knows
(an assertion id from the real case file) becomes visible after navigation.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from apps.api.main import build_app
from packages.core.config import Settings
from packages.execution.browser import browser_channel
from tests.netutil import release_reserved, reserve_port, take_reserved

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "apps" / "web" / "dist"


@pytest.fixture(scope="module")
def served() -> Any:
    if not (DIST / "index.html").exists():
        pytest.skip("workbench not built; run `npm run build` in apps/web")

    port = reserve_port()
    db = os.path.join(tempfile.gettempdir(), f"tg_ui_{uuid.uuid4().hex[:8]}.db")
    settings = Settings(
        app_env="test",
        database_url="sqlite:///" + db.replace("\\", "/"),
        workbench_enabled=True,
    )
    app = build_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": take_reserved(port)}, daemon=True
    )
    thread.start()

    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                if httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:  # pragma: no cover
            raise RuntimeError("api server did not start")

        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "server thread did not stop"
        release_reserved(port)
        try:
            Path(db).unlink(missing_ok=True)
        except PermissionError:  # Windows keeps the sqlite handle briefly
            pass


def test_spa_index_is_served(served: str) -> None:
    resp = httpx.get(served + "/", timeout=5, trust_env=False)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "/assets/" in resp.text
    assert 'id="app"' in resp.text


def test_api_is_not_shadowed_by_the_static_mount(served: str) -> None:
    assert httpx.get(served + "/api/v1/overview", timeout=5, trust_env=False).status_code == 200
    assert httpx.get(served + "/version", timeout=5, trust_env=False).status_code == 200


def test_workbench_renders_real_cases_and_probes(served: str) -> None:
    console_errors: list[str] = []
    not_found: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=browser_channel(), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.on(
            "response",
            lambda r: not_found.append(r.url) if r.status == 404 else None,
        )

        page.goto(served + "/", wait_until="networkidle")
        # R5 renamed the brand subtitle to "质量智能平台"; assert the shell rendered.
        assert page.locator("text=质量智能平台").count() >= 1

        # Overview must show numbers fetched from /api/v1/overview.
        page.locator("a.nav-item", has_text="测试用例").click()
        page.wait_for_selector("table tbody tr")
        body = page.inner_text("main")
        assert "TC-CREATE-001" in body, "case id from disk did not reach the UI"

        # Navigate into the detail page and prove a backend-only value is shown.
        page.locator("a.mono", has_text="TC-CREATE-001").first.click()
        page.wait_for_selector("text=断言与 Oracle 绑定")
        detail = page.inner_text("main")
        assert "s1" in detail  # step ids live in the case JSON, not in the SPA
        # oracle source_ref + adjudication rule only exist in the backend
        assert "state.project_count" in detail
        assert "frozen_fallback_inconclusive_on_conflict" in detail

        # Probe catalogue comes from the gateway whitelist (7 ids).
        page.locator("a.nav-item", has_text="探针审计").click()
        page.wait_for_selector("text=探针注册表")
        probes = page.inner_text("main")
        for probe_id in ("DOM_SNAPSHOT", "DATABASE_READ", "NETWORK_READ"):
            assert probe_id in probes

        browser.close()

    assert not not_found, f"404 resources: {not_found}"
    assert not console_errors, f"console errors: {console_errors}"


def test_empty_pages_state_their_emptiness(served: str) -> None:
    """A page with no data must say so instead of inventing rows."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=browser_channel(), headless=True)
        page = browser.new_page()
        page.goto(served + "/#/repairs", wait_until="networkidle")
        page.wait_for_selector("main")
        text = page.inner_text("main")
        assert "尚无修复提案" in text
        assert page.locator("table tbody tr").count() == 0
        browser.close()
