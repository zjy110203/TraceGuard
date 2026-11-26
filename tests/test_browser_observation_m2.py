from __future__ import annotations

from packages.agents.exploration import ExplorationTrace
from packages.agents.explorer import PlaywrightAdapter
from packages.agents.goal_assertions import evaluate_assertions


def test_real_browser_observes_long_page_structurally_without_unbounded_text() -> None:
    trace = ExplorationTrace(
        exploration_id="exp_m2_long_page",
        test_intent_ref="intent-m2",
        application_version="test",
        browser_version="chromium",
        agent_version="test-adapter",
        prompt_version="none",
        tool_version="1.0.0",
    )
    rows = "".join(f"<tr><td>project-{index:04d}</td></tr>" for index in range(500))
    with PlaywrightAdapter(trace) as adapter:
        adapter._page.set_content(f"<html><title>Long projects</title><table>{rows}</table></html>")
        observation = adapter.observe(7)
    record = observation.to_record()
    assert len(observation.page_text) <= 3000
    assert len(record["text_excerpt"]) <= 400
    assert record["dom_counts"]["table_row_count"] == 500
    report = evaluate_assertions(
        [{"kind": "table_row_count", "name": "all-projects", "value": 500}], record
    )
    assert report.verdict == "VERIFIED"


def test_browser_never_emits_password_autocomplete_or_scenario_sensitive_values() -> None:
    trace = ExplorationTrace(
        exploration_id="exp_m3_sensitive",
        test_intent_ref="intent-m3",
        application_version="test",
        browser_version="chromium",
        agent_version="test-adapter",
        prompt_version="none",
        tool_version="1.0.0",
    )
    with PlaywrightAdapter(trace, sensitive_selectors=("[data-testid=secret-field]",)) as adapter:
        adapter._page.set_content(
            """<input data-testid='secret-field' value='scenario-only-secret'>
            <input data-testid='password' type='password' value='password-secret'>
            <input data-testid='otp' autocomplete='one-time-code' value='otp-secret'>"""
        )
        record = adapter.observe(3).to_record()
    payload = str(record)
    assert "scenario-only-secret" not in payload
    assert "password-secret" not in payload
    assert "otp-secret" not in payload
    sensitive = [item for item in record["visible_elements"] if item.get("sensitive")]
    assert len(sensitive) == 3
    assert all(item["name"] == "«redacted»" for item in sensitive)
