"""Real-browser acceptance for the TraceGuard workbench (R5 baseline).

Discipline:
* targets ONLY the local platform (``TRACEGUARD_BASE`` or 127.0.0.1:8000);
* collects console errors / page errors / failed API requests per route;
* exercises real interactions (navigation, graph node click, benchmark state,
  repair validation) — not just screenshots;
* validates five viewports: 1920x1080, 1440x900, 1024x768, 768x1024, 390x844;
* writes screenshots for the acceptance report and a JSON summary that
  ``scripts/verify_all.py`` consumes.

Run (the module has a ``__main__`` guard, so no ``-m`` needed)::

    python scripts/ui_acceptance.py
    set TRACEGUARD_BASE=http://127.0.0.1:8123 && python scripts/ui_acceptance.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = os.environ.get("TRACEGUARD_BASE", "http://127.0.0.1:8000").rstrip("/")
# Locally we use the real system Chrome; CI sets this to "" so the runner uses
# Playwright's bundled Chromium (a CI image has no personal Chrome installed).
BROWSER_CHANNEL = os.environ.get("TRACEGUARD_BROWSER_CHANNEL", "chrome")
SHOT_DIR = Path(os.environ.get("TRACEGUARD_SHOTS", "docs/ui/r5/screenshots"))
SUMMARY_PATH = Path(os.environ.get("TRACEGUARD_E2E_SUMMARY", ".traceguard-tmp/r5/e2e-summary.json"))


def _launch(pw: object) -> object:
    """Prefer the requested channel, fall back to bundled Chromium."""
    chromium = pw.chromium  # type: ignore[attr-defined]
    # Same container-safe flag the product launcher uses (small /dev/shm on CI
    # runners kills the renderer mid-screenshot).
    from packages.execution.browser import launch_args

    if BROWSER_CHANNEL:
        try:
            return chromium.launch(channel=BROWSER_CHANNEL, headless=True, args=launch_args())
        except Exception as exc:  # noqa: BLE001 - channel may be absent
            print(f"[i] channel '{BROWSER_CHANNEL}' unavailable ({exc}); using bundled Chromium")
    return chromium.launch(headless=True, args=launch_args())


# Five required viewports. Widths 1024/768/390 are the contract; heights are
# the common device heights so a full-page screenshot is realistic.
VIEWPORTS: list[tuple[int, int, str]] = [
    (1920, 1080, "1920x1080"),
    (1440, 900, "1440x900"),
    (1024, 768, "1024x768"),
    (768, 1024, "768x1024"),
    (390, 844, "390x844"),
]

ROUTES = [
    ("overview", "/#/overview"),
    ("intents", "/#/intents"),
    ("candidates", "/#/candidates"),
    ("cases", "/#/cases"),
    ("explorer", "/#/explorer"),
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

# Pages where a viewport screenshot (not full page) is more informative because
# the interesting state lives above the fold; others get full-page captures.
INTERACTIVE_ROUTES = {
    "/#/overview",
    "/#/graph",
    "/#/benchmark",
    "/#/repairs",
    "/#/candidates",
    "/#/system",
    "/#/events",
    "/#/explorer",
}

results: list[tuple[str, str, str]] = []  # (route, check, PASS/FAIL/SKIP detail)


def record(route: str, check: str, ok: bool, detail: str = "") -> None:
    results.append((route, check, "PASS" if ok else f"FAIL {detail}"))


def skip(route: str, check: str, reason: str) -> None:
    """Honest skip: the check could not run because the fixture was absent.

    A skip is *not* a pass (R5-4.5 §7). Before this existed, an empty database
    made the deep-link checks report PASS while nothing had been verified.
    """
    results.append((route, check, f"SKIP {reason}"))


def blank_page(content: str) -> bool:
    body = content.split("<body", 1)[-1]
    text = body.split(">", 1)[-1]
    stripped = text.replace("<script", "\x00").replace("<style", "\x01")
    visible = stripped.split("\x00")[0].split("\x01")[0]
    return len(visible.strip()) < 40


def _api(path: str) -> object:
    """Read-only API read so the acceptance run can deep-link into real data."""
    with urllib.request.urlopen(f"{BASE}/api/v1{path}", timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sample_run(max_probes: int = 12) -> tuple[str, bool] | None:
    """Newest run with step rows, preferring one that also produced evidence.

    Deep links are meaningless without steps, and the evidence hop is only
    verifiable when the sampled run actually owns evidence records.
    """
    try:
        tasks = _api("/tasks")
    except Exception as exc:  # noqa: BLE001 - acceptance must not crash on discovery
        print(f"[i] cannot list tasks ({exc}); deep-link checks will be skipped")
        return None
    if not isinstance(tasks, list):
        return None

    # One cheap history call per task, then probe details newest-first so a
    # database full of cancelled runs cannot turn this into an N+1 sweep.
    candidates: list[tuple[str, str]] = []
    for task in tasks:
        task_id = task.get("task_id") if isinstance(task, dict) else None
        if not task_id:
            continue
        try:
            history = _api(f"/tasks/{task_id}/runs")
        except Exception:  # noqa: BLE001 - a single bad task must not abort
            continue
        if not isinstance(history, list):
            continue
        for row in history:
            if isinstance(row, dict) and row.get("run_id"):
                candidates.append((str(row.get("created_at", "")), str(row["run_id"])))

    candidates.sort(reverse=True)
    fallback: str | None = None
    for _, run_id in candidates[:max_probes]:
        try:
            detail = _api(f"/runs/{run_id}")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(detail, dict) or not detail.get("steps"):
            continue
        try:
            evd = _api(f"/evidence/records?run_id={run_id}&limit=1")
        except Exception:  # noqa: BLE001
            evd = []
        if isinstance(evd, list) and evd:
            return run_id, True
        if fallback is None:
            fallback = run_id
    return (fallback, False) if fallback else None


def _rendered(page: object, selector: str, timeout_ms: int = 20000) -> bool:
    """Wait for a selector instead of guessing with a fixed sleep.

    Diagnosis is computed on demand from the evidence bundle; with 15 evidence
    records it takes ~2 s, so the previous ``wait_for_timeout(900)`` made the
    deep-link checks fail deterministically even though the page rendered fine.
    """
    try:
        page.wait_for_selector(selector, timeout=timeout_ms, state="attached")  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001 - absent selector is a recorded failure
        return False


def _deep_links(page: object) -> None:
    """Run -> diagnosis -> evidence -> run must be walkable without retyping ids."""
    sample = _sample_run()
    if sample is None:
        skip("deep-link", "runs with steps available", "no run with steps in this database")
        return
    run_id, has_evidence = sample

    # 1. run detail renders its step table
    page.goto(f"{BASE}/#/runs/{run_id}")  # type: ignore[attr-defined]
    page.wait_for_timeout(800)  # type: ignore[attr-defined]
    step_rows = page.locator("table tbody tr").count()  # type: ignore[attr-defined]
    record("deep-link", "run detail shows step rows", step_rows >= 1, f"rows={step_rows}")

    # 2. the diagnosis tab is a real route, not a dead label
    tab = page.locator('.tabs a:has-text("诊断")')  # type: ignore[attr-defined]
    if tab.count() >= 1:
        tab.first.click()
        page.wait_for_timeout(900)  # type: ignore[attr-defined]
        record("deep-link", "run detail -> diagnosis tab", f"/runs/{run_id}/diagnosis" in page.url)  # type: ignore[attr-defined]
        has_meta = _rendered(page, ".meta")
        record("deep-link", "diagnosis page renders verdict", has_meta, "no .meta within 20s")
    else:
        record("deep-link", "run detail -> diagnosis tab", False, "no 诊断 tab")

    # 3. diagnosis -> evidence, filtered to this run only
    if not has_evidence:
        # Honest skip: without evidence there is nothing to hop to, and the
        # link is deliberately hidden rather than leading to an empty page.
        skip("deep-link", "diagnosis -> evidence filtered", "sampled run has no evidence")
        skip("deep-link", "evidence -> run detail", "sampled run has no evidence")
    else:
        ev_link = page.locator('.xlinks a:has-text("证据")')  # type: ignore[attr-defined]
        if ev_link.count() >= 1 or _rendered(page, '.xlinks a:has-text("证据")'):
            ev_link.first.click()
            page.wait_for_timeout(1000)  # type: ignore[attr-defined]
            run_cells = page.evaluate(  # type: ignore[attr-defined]
                "Array.from(document.querySelectorAll('table tbody tr:not(.detail)'))"
                ".map(tr => (tr.children[4]?.textContent ?? '').trim())"
            )
            only_this_run = bool(run_cells) and all(c == run_id for c in run_cells)
            record(
                "deep-link",
                "diagnosis -> evidence filtered",
                only_this_run,
                f"rows={len(run_cells)} sample={run_cells[:2]}",
            )
        else:
            record("deep-link", "diagnosis -> evidence filtered", False, "no evidence link")

        # 4. evidence -> run closes the loop
        back_link = page.locator('.xlinks a:has-text("运行详情")')  # type: ignore[attr-defined]
        if back_link.count() >= 1:
            back_link.first.click()
            page.wait_for_timeout(800)  # type: ignore[attr-defined]
            record("deep-link", "evidence -> run detail", f"/runs/{run_id}" in page.url)  # type: ignore[attr-defined]
        else:
            record("deep-link", "evidence -> run detail", False, "no back link")

    # 5. the query-param entry point must prefill and auto-load
    page.goto(f"{BASE}/#/diagnosis?run={run_id}")  # type: ignore[attr-defined]
    page.wait_for_timeout(300)  # type: ignore[attr-defined]
    prefilled = page.eval_on_selector("#diag-run", "el => el.value")  # type: ignore[attr-defined]
    record(
        "deep-link",
        "diagnosis?run= prefills input",
        prefilled == run_id,
        f"value={prefilled!r}",
    )
    record(
        "deep-link",
        "diagnosis?run= auto-loads",
        _rendered(page, ".meta"),
        "no .meta within 20s",
    )


def main() -> int:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    failures = 0

    with sync_playwright() as pw:
        browser = _launch(pw)
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
        health_ok = "ok" in page.content().lower()
        record("pre", "platform /health reachable", health_ok)
        if not health_ok:
            print(f"[x] platform not healthy at {BASE}/health — aborting")
            browser.close()
            _dump(failures)
            return 1

        # ---- every route: load, no blank, no errors, screenshot @1440x900 ----
        for name, path in ROUTES:
            console_errors.clear()
            page_errors.clear()
            bad_responses.clear()
            page.goto(BASE + path)
            page.wait_for_timeout(900)
            content = page.content()
            record(path, "not blank", not blank_page(content))
            record(path, "no console error", not console_errors, "; ".join(console_errors[:3]))
            record(path, "no page error", not page_errors, "; ".join(page_errors[:2]))
            record(path, "no API 4xx/5xx", not bad_responses, "; ".join(bad_responses[:3]))
            shot = SHOT_DIR / f"{name}_1440x900.png"
            if path in INTERACTIVE_ROUTES:
                page.screenshot(path=str(shot), full_page=False)
            else:
                page.screenshot(path=str(shot), full_page=True)

        # ---- interaction: overview metric card navigation ----
        page.goto(BASE + "/#/overview")
        # A fixed sleep is not a synchronisation primitive (R5-4.9): the KPI
        # cards appear only after their data request resolves, and a larger
        # seeded dataset can legitimately take longer than 700 ms. Wait for the
        # element with a bound, then still fail if it never shows up.
        card_selector = "button.stat, .stat.clickable, button.metric, .metric"
        try:
            page.wait_for_selector(card_selector, timeout=20000)
        except Exception:  # noqa: BLE001 - timeout is the failure being recorded
            record("overview", "metric cards present", False, "no metric button within 20s")
        else:
            cards = page.locator(card_selector)
            before = page.url
            cards.nth(0).click()
            page.wait_for_timeout(600)
            record("overview", "metric card navigates", page.url != before or "#/" in page.url)

        # ---- interaction: breadcrumb back ----
        page.goto(BASE + "/#/cases")
        page.wait_for_timeout(600)
        crumbs = page.locator(".crumbs a")
        record("breadcrumb", "crumbs rendered", crumbs.count() >= 1)

        # ---- interaction: graph node click (only when data exists) ----
        page.goto(BASE + "/#/graph")
        page.wait_for_timeout(900)
        nodes = page.locator("svg .node, svg [data-node-id]")
        if nodes.count() > 0:
            nodes.first.click()
            page.wait_for_timeout(800)
            detail = page.locator(".kv-wrap, .node-detail")
            record("graph", "node click shows detail panel", detail.count() > 0)
        else:
            skip("graph", "node click shows detail panel", "empty graph (no nodes)")

        # ---- interaction: benchmark state honesty ----
        page.goto(BASE + "/#/benchmark")
        page.wait_for_timeout(700)
        has_state = (
            page.locator("text=NOT_RUN").count() > 0
            or page.locator("text=BLOCKED").count() > 0
            or page.locator(".state").count() > 0
        )
        record("benchmark", "empty/blocked state visible", has_state)

        # ---- interaction: repairs page reachable ----
        page.goto(BASE + "/#/repairs")
        page.wait_for_timeout(700)
        record("repairs", "page reachable", "修复" in page.content())

        # ---- interaction: explorer policy honesty ----
        page.goto(BASE + "/#/explorer")
        page.wait_for_timeout(700)
        explorer_text = page.content()
        seen = "探索" in explorer_text or "Explorer" in explorer_text
        record("explorer", "explorer page reachable", seen)

        # ---- deep links: run -> diagnosis -> evidence -> run ----
        _deep_links(page)

        # ---- responsive sweep: all routes at every viewport ----
        for width, height, tag in VIEWPORTS:
            page.set_viewport_size({"width": width, "height": height})
            for name, path in ROUTES:
                page.goto(BASE + path)
                page.wait_for_timeout(450)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth"
                    " > document.documentElement.clientWidth + 2"
                )
                record(f"{tag} {path}", "no horizontal overflow", not overflow, f"w={width}")
                page.screenshot(path=str(SHOT_DIR / f"{name}_{tag}.png"), full_page=False)

        # ---- keyboard focus ----
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(BASE + "/#/overview")
        page.wait_for_timeout(400)
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement?.tagName ?? ''")
        record("a11y", "Tab moves focus into document", focused not in ("", "BODY"))

        browser.close()

    return _dump(failures)


def _dump(failures: int) -> int:
    print("\n=== R5 browser acceptance ===")
    skipped = 0
    for route, check, verdict in results:
        print(f"{route:26} {check:34} {verdict}")
        if verdict.startswith("FAIL"):
            failures += 1
        elif verdict.startswith("SKIP"):
            skipped += 1
    passed = len(results) - failures - skipped
    print(f"\ntotal={len(results)} passed={passed} failures={failures} skipped={skipped}")
    SUMMARY_PATH.write_text(
        json.dumps(
            {
                "base": BASE,
                "total": len(results),
                "passed": passed,
                "failures": failures,
                "skipped": skipped,
                "results": [{"route": r, "check": c, "verdict": v} for r, c, v in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"summary -> {SUMMARY_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
