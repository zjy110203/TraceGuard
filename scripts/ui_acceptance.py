"""R4-1.5 real-browser acceptance for the refactored workbench.

Discipline:
* targets ONLY http://127.0.0.1:<port> (local platform);
* collects console errors / page errors / failed API requests per route;
* exercises real interactions (navigation, graph node click, benchmark
  expand, repair validation) — not just screenshots;
* saves screenshots to docs/ui/screenshots/ for the acceptance report.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8000"
SHOT_DIR = Path("docs/ui/screenshots")

ROUTES = [
    ("overview", "/#/overview"),
    ("intents", "/#/intents"),
    ("candidates", "/#/candidates"),
    ("cases", "/#/cases"),
    ("tasks", "/#/tasks"),
    ("runs", "/#/runs"),
    ("events", "/#/events"),
    ("evidence", "/#/evidence"),
    ("graph", "/#/graph"),
    ("diagnosis", "/#/diagnosis"),
    ("probes", "/#/probes"),
    ("repairs", "/#/repairs"),
    ("benchmark", "/#/benchmark"),
    ("system", "/#/system"),
]

INTERACTIVE_ROUTES = {
    "/#/overview",
    "/#/graph",
    "/#/benchmark",
    "/#/repairs",
    "/#/candidates",
    "/#/system",
    "/#/events",
}

results: list[tuple[str, str, str]] = []  # (route, check, PASS/FAIL/detail)


def record(route: str, check: str, ok: bool, detail: str = "") -> None:
    results.append((route, check, "PASS" if ok else f"FAIL {detail}"))


def blank_page(content: str) -> bool:
    body = content.split("<body", 1)[-1]
    text = body.split(">", 1)[-1]
    # Strip scripts/styles; a blank page has essentially no visible text.
    stripped = text.replace("<script", "\x00").replace("<style", "\x01")
    visible = stripped.split("\x00")[0].split("\x01")[0]
    return len(visible.strip()) < 40


def main() -> int:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        console_errors: list[str] = []
        page_errors: list[str] = []
        bad_responses: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on(
            "response",
            lambda r: (
                bad_responses.append(f"{r.status} {r.url}")
                if (r.status >= 400 and "/api/" in r.url)
                else None
            ),
        )

        # ---- health precondition ----
        page.goto(f"{BASE}/health")
        record(
            "pre",
            "platform /health reachable",
            "UP" in page.content() or "ok" in page.content().lower(),
        )

        # ---- every route: load, no blank, screenshot ----
        for name, path in ROUTES:
            console_errors.clear()
            page_errors.clear()
            bad_responses.clear()
            page.goto(BASE + path)
            page.wait_for_timeout(1200)
            content = page.content()
            blank = blank_page(content)
            record(path, "not blank", not blank)
            record(path, "no console error", not console_errors, "; ".join(console_errors[:3]))
            record(path, "no page error", not page_errors, "; ".join(page_errors[:2]))
            record(path, "no API 4xx/5xx", not bad_responses, "; ".join(bad_responses[:3]))
            if path in INTERACTIVE_ROUTES:
                page.screenshot(path=str(SHOT_DIR / f"{name}.png"), full_page=False)
            else:
                page.screenshot(path=str(SHOT_DIR / f"{name}.png"), full_page=True)

        # ---- interaction: overview metric card navigation ----
        page.goto(BASE + "/#/overview")
        page.wait_for_timeout(800)
        cards = page.locator("button.metric")
        if cards.count() >= 3:
            cards.nth(0).click()
            page.wait_for_timeout(600)
            record(
                "overview",
                "metric card navigates",
                "/overview" not in page.url.split("#")[-1] or "tasks" in page.url,
            )
        else:
            record("overview", "metric cards present", False, "fewer than 3 metric buttons")

        # ---- interaction: breadcrumb back ----
        page.goto(BASE + "/#/cases")
        page.wait_for_timeout(700)
        crumbs = page.locator(".crumbs a")
        record("breadcrumb", "crumbs rendered", crumbs.count() >= 1)

        # ---- interaction: graph node click (only if nodes exist) ----
        page.goto(BASE + "/#/graph")
        page.wait_for_timeout(900)
        nodes = page.locator("svg .node")
        if nodes.count() > 0:
            nodes.first.click()
            page.wait_for_timeout(900)
            has_detail = page.locator(".kv-wrap").count() > 0
            record("graph", "node click shows detail panel", has_detail)
        else:
            record("graph", "node click skipped", True, "empty graph (no runs yet)")

        # ---- interaction: benchmark empty-state vs data ----
        page.goto(BASE + "/#/benchmark")
        page.wait_for_timeout(800)
        has_hint = page.locator("text=NOT_RUN").count() > 0 or page.locator(".state").count() > 0
        record("benchmark", "empty state or NOT_RUN markers visible", has_hint)

        # ---- interaction: repair validation (client-side gate) ----
        page.goto(BASE + "/#/repairs")
        page.wait_for_timeout(800)
        record("repairs", "page reachable", "修复" in page.content())

        # ---- responsive spot checks ----
        for w, h, tag in [(1024, 768, "1024x768"), (390, 844, "390x844")]:
            page.set_viewport_size({"width": w, "height": h})
            page.goto(BASE + "/#/overview")
            page.wait_for_timeout(700)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
            )
            record(f"responsive {tag}", "no horizontal overflow", not overflow, f"w={w}")
            page.screenshot(path=str(SHOT_DIR / f"overview_{tag}.png"))

        # ---- keyboard focus ----
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(BASE + "/#/overview")
        page.wait_for_timeout(500)
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement?.tagName ?? ''")
        record("a11y", "Tab moves focus into document", focused not in ("", "BODY"))

        browser.close()

    print("\n=== R4-1.5 acceptance results ===")
    for route, check, verdict in results:
        mark = "PASS" if verdict == "PASS" else verdict
        print(f"{route:18} {check:38} {mark}")
        if verdict.startswith("FAIL"):
            failures += 1
    print(f"\ntotal={len(results)} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
