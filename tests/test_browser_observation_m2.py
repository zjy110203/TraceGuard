from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from packages.agents.exploration import ExplorationTrace, ToolCall
from packages.agents.explorer import PlaywrightAdapter
from packages.agents.goal_assertions import evaluate_assertions
from tests.netutil import serve_http


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


def test_real_browser_redacts_url_and_evaluates_attrs_and_multistep_history() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"""<html><title>Start</title><body>
              <input data-testid='email' type='email' role='textbox'>
              <a data-testid='next' role='link'
                 href='/next?access_token=href-secret#password=abc'>Next</a>
              </body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    trace = ExplorationTrace(
        exploration_id="exp_m2_redacted_url",
        test_intent_ref="intent-m2",
        application_version="test",
        browser_version="chromium",
        agent_version="test-adapter",
        prompt_version="none",
        tool_version="1.0.0",
    )
    with (
        serve_http(Handler) as base,
        PlaywrightAdapter(trace, count_selectors=("input[type=email]",)) as adapter,
    ):
        adapter._page.goto(f"{base}/start?access_token=secret123#password=abc")
        first = adapter.observe(0).to_record()
        adapter.execute(ToolCall("click", {"selector": "[data-testid=next]"}))
        second = adapter.observe(1).to_record()

    serialized = str([first, second])
    assert "secret123" not in serialized
    assert "password=abc" not in serialized
    assert "href-secret" not in serialized
    assert first["url"].endswith("/start")
    attr_report = evaluate_assertions(
        [
            {
                "name": "field-role",
                "kind": "element_attr",
                "testid": "email",
                "attribute": "role",
                "value": "textbox",
            },
            {
                "name": "field-type",
                "kind": "element_attr",
                "testid": "email",
                "attribute": "type",
                "value": "email",
            },
            {
                "name": "safe-link-href",
                "kind": "element_attr",
                "testid": "next",
                "attribute": "href",
                "value": "/next",
            },
        ],
        first,
    )
    assert attr_report.verdict == "VERIFIED"
    selector_count_report = evaluate_assertions(
        [
            {
                "name": "email-field-count",
                "kind": "table_row_count",
                "selector": "input[type=email]",
                "value": 1,
            }
        ],
        first,
    )
    assert selector_count_report.verdict == "VERIFIED"
    multi_report = evaluate_assertions(
        [
            {
                "name": "start-then-transition",
                "kind": "multi_step_goal",
                "assertions": [
                    {
                        "observation_ref": first["observation_ref"],
                        "assertion": {"name": "start", "kind": "url_exact", "value": "/start"},
                    },
                    {
                        "observation_ref": second["observation_ref"],
                        "assertion": {"name": "next", "kind": "url_exact", "value": "/next"},
                    },
                ],
            }
        ],
        second,
        observations=[first, second],
    )
    assert multi_report.verdict == "VERIFIED"
