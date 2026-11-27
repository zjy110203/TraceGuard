"""Explicit opt-in path Oracles must prove actual browser outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import build_app
from apps.teamflow.models import Project
from packages.agents.exploration import verify_trace_record
from packages.agents.scenarios import Scenario, ScenarioRegistry, default_registry
from packages.core import database
from packages.core.config import Settings
from tests.test_agentic_scenarios import _isolated_teamflow

SCENARIOS = [
    "teamflow-login",
    "project-create",
    "project-query",
    "page-navigation",
    "project-edit",
    "form-validation",
]


def test_new_versions_are_opt_in_and_history_has_no_overrides() -> None:
    registry = default_registry()
    for sid in SCENARIOS:
        old = registry.require(sid)
        assert old.version == ("1.0.0" if sid == "form-validation" else "2.0.0")
        assert all(
            "goal_definition" not in p and "intent_templates" not in p for p in old.paths.values()
        )
        new = registry.require(sid, "2.1.0")
        assert new.digest != old.digest
        assert new.validate() == []


def test_legacy_version_cannot_accept_new_path_contract() -> None:
    new = default_registry().require("project-create", "2.1.0")
    historical = Scenario(**{**new.__dict__, "version": "2.0.0"})
    with pytest.raises(ValueError, match="version >= 2.1.0"):
        ScenarioRegistry().register(historical)


def _state_at(record: dict, phase: str, action_index: int | None = None) -> dict:
    snapshots = record["business_state_evidence"]["snapshots"]
    return next(
        sample["state"]
        for sample in snapshots
        if sample["phase"] == phase
        and (action_index is None or sample.get("action_index") == action_index)
    )


def _submit_action_indexes(record: dict, selector: str) -> list[int]:
    indexes = []
    step_index = 0
    for action in record["action_sequence"]:
        if action.get("tool") == "snapshot":
            continue
        step_index += 1
        if action.get("tool") == "click" and action.get("args", {}).get("selector") == selector:
            indexes.append(step_index)
    return indexes


def _assert_business_outcome(sid: str, kind: str, record: dict) -> None:
    """Pin UI assertions to the independently observed TeamFlow DB rows."""
    run_id = record["exploration_id"]
    prefix = f"TG_RUN_{run_id}_"
    setup = _state_at(record, "after_setup")
    final = _state_at(record, "final_before_cleanup")
    setup_rows = {row["id"]: row for row in setup["projects"]}
    final_rows = {row["id"]: row for row in final["projects"]}
    created = [
        row
        for row in final_rows.values()
        if row["id"] not in setup_rows and row["name"].startswith(prefix)
    ]

    if sid in {"teamflow-login", "project-query"} or (
        sid == "page-navigation" and kind != "boundary"
    ):
        assert final_rows == setup_rows

    if sid == "project-create":
        submits = _submit_action_indexes(record, "[data-testid=create-submit]")
        if kind == "exception":
            assert len(submits) == 1
            assert _state_at(record, "after_action", submits[0])["projects"] == setup["projects"]
            assert created == []
        elif kind == "recovery":
            assert len(submits) == 2
            assert _state_at(record, "after_action", submits[0])["projects"] == setup["projects"]
            recovered = _state_at(record, "after_action", submits[1])
            assert len(recovered["projects"]) == len(setup["projects"]) + 1
            assert len(created) == 1 and "recovered-" in created[0]["name"]
        else:
            assert len(submits) == 1
            assert len(created) == 1
            if kind == "boundary":
                assert len(created[0]["name"]) == 200

    elif sid == "form-validation":
        submits = _submit_action_indexes(record, "[data-testid=create-submit]")
        if kind in {"normal", "exception"}:
            assert len(submits) == 1
            assert _state_at(record, "after_action", submits[0])["projects"] == setup["projects"]
            assert created == []
        elif kind == "recovery":
            assert len(submits) == 2
            assert _state_at(record, "after_action", submits[0])["projects"] == setup["projects"]
            recovered = _state_at(record, "after_action", submits[1])
            assert len(recovered["projects"]) == len(setup["projects"]) + 1
            assert len(created) == 1 and "recovered-" in created[0]["name"]
        else:
            assert len(created) == 1 and len(created[0]["name"]) == 200

    elif sid == "project-edit":
        seed_rows = [row for row in setup_rows.values() if row["name"].startswith(prefix)]
        assert len(seed_rows) == 1
        seed = seed_rows[0]
        submits = _submit_action_indexes(record, "[data-testid=edit-project-submit]")
        assert seed["id"] in final_rows
        if kind == "exception":
            assert len(submits) == 1
            assert _state_at(record, "after_action", submits[0])["projects"] == setup["projects"]
            assert final_rows[seed["id"]] == seed
        elif kind == "recovery":
            assert len(submits) == 2
            assert _state_at(record, "after_action", submits[0])["projects"] == setup["projects"]
            recovered = _state_at(record, "after_action", submits[1])
            assert next(row for row in recovered["projects"] if row["id"] == seed["id"])[
                "name"
            ].startswith(prefix + "recovered-")
            assert final_rows[seed["id"]]["name"].startswith(prefix + "recovered-")
        else:
            assert final_rows[seed["id"]]["id"] == seed["id"]
            if kind == "boundary":
                assert len(final_rows[seed["id"]]["name"]) == 200
            else:
                assert final_rows[seed["id"]]["name"].startswith(prefix + "updated-")

    elif sid == "project-query":
        assert final_rows == setup_rows
        path = record["observations"][-1]["url_path"]
        if path.startswith("/projects/") and path != "/projects/new":
            project_id = path.rsplit("/", 1)[-1]
            if kind == "exception":
                assert project_id not in final_rows
            else:
                assert project_id in final_rows
                assert final_rows[project_id]["name"].startswith(prefix)

    elif sid == "page-navigation" and kind == "boundary":
        if created:
            assert len(created) == 1 and len(created[0]["name"]) == 200
        else:
            assert final_rows == setup_rows


@pytest.mark.parametrize("sid", SCENARIOS)
def test_all_four_paths_have_passing_real_browser_verdicts(tmp_path: Path, sid: str) -> None:
    scenario = default_registry().require(sid, "2.1.0")
    with _isolated_teamflow(tmp_path, test_user="qa_runner") as base:
        parsed_base = urlsplit(base)
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{(tmp_path / 'platform.db').as_posix()}",
            teamflow_host=parsed_base.hostname or "127.0.0.1",
            teamflow_port=parsed_base.port or 8001,
            teamflow_test_user="qa_runner",
            teamflow_test_password="test-pass-123",
            exploration_data_dir=str(tmp_path / "explorations"),
            candidate_data_dir=str(tmp_path / "candidates"),
            case_dir=str(tmp_path / "cases"),
            benchmark_data_dir=str(tmp_path / "benchmark"),
            chain_data_dir=str(tmp_path / "chains"),
            blob_dir=str(tmp_path / "blobs"),
        )
        with TestClient(build_app(settings)) as client:
            for kind in ("normal", "exception", "boundary", "recovery"):
                result = client.post(
                    "/api/v1/explorations",
                    json={
                        "scenario": sid,
                        "scenario_version": "2.1.0",
                        "path": kind,
                        "base_url": base,
                        "policy": "scripted",
                        "max_steps": 30,
                    },
                )
                assert result.status_code == 201, result.text
                record = result.json()
                (tmp_path / f"{sid}-{kind}-verdict.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                spec = scenario.path_spec_for_path(kind)
                assert record["goal_status"] == spec["expected_goal_status"], record["goal_reason"]
                assert record["test_case_verdict"] == "PASSED", record["path_expect"]
                assert all(
                    r["verdict"] == "PASSED"
                    for r in record["path_expect"]["results"]
                    if r["required"]
                )
                assert client.app.state.settings.eval_control_token not in json.dumps(record)
                _assert_business_outcome(sid, kind, record)
                assert record["data_lifecycle"]["state"] == "CLEANED"
                assert record["business_state_evidence"]["verdict"] == "PASS"
                assert record["business_state_evidence"]["cleanup"]["verdict"] == "PASS"
                assert (
                    record["business_state_evidence"]["cleanup"]["remaining_run_project_ids"] == []
                )
                assert all(
                    sample["status"] == "AVAILABLE"
                    for sample in record["business_state_evidence"]["snapshots"]
                )
                assert verify_trace_record(record)
                detail = client.get(f"/api/v1/explorations/{record['exploration_id']}").json()
                assert detail["scenario_digest_verified"] is True
                assert detail["goal_assertions_verified"] is True
                compiled = client.post(
                    f"/api/v1/explorations/{record['exploration_id']}/compile", json={}
                )
                if spec["expected_goal_status"] == "NOT_VERIFIED":
                    assert compiled.status_code == 409, compiled.text
                    details = compiled.json()["error"]["details"]
                    if sid == "project-query":
                        # The dynamic route satisfies the legacy TestIntent while the
                        # versioned goal correctly detects that the project was absent.
                        assert details["recorded"] == "NOT_VERIFIED"
                        assert details["recomputed"] == "VERIFIED"
                    else:
                        assert details["goal_status"] == "NOT_VERIFIED"
                    assert not any(
                        json.loads(p.read_bytes()).get("source_trace_hash")
                        == record["source_trace_hash"]
                        for p in (tmp_path / "candidates").glob("cand_*.json")
                    )
                else:
                    assert compiled.status_code == 201, compiled.text
                    assert compiled.json()["status"] == "PENDING_REVIEW"
                    path = tmp_path / "candidates" / f"{compiled.json()['candidate_id']}.json"
                    assert (
                        json.loads(path.read_bytes())["source_trace_hash"]
                        == record["source_trace_hash"]
                    )
                assert not list((tmp_path / "cases").glob("*.json"))
                if kind == "recovery":
                    sequence = next(
                        r
                        for r in record["path_expect"]["results"]
                        if r["kind"] == "multi_step_goal"
                    )
                    assert len(sequence["evidence_refs"]) == 2
                    assert len(set(sequence["evidence_refs"])) == 2
                if kind == "boundary" and sid in {
                    "project-create",
                    "project-edit",
                    "form-validation",
                }:
                    fills = [
                        a["args"]["value"]
                        for a in record["action_sequence"]
                        if a["tool"] == "fill"
                        and a["args"].get("selector")
                        in {"[data-testid=project-name]", "[data-testid=edit-project-name]"}
                    ]
                    assert len(fills[-1]) == 200
                    assert fills[-1] in record["observations"][-1]["text_excerpt"]
                if kind == "boundary" and sid == "project-query":
                    click = next(
                        a for a in reversed(record["action_sequence"]) if a["tool"] == "click"
                    )
                    target_id = (
                        click["args"]["selector"].split('data-project-id="')[1].split('"')[0]
                    )
                    assert record["observations"][-1]["url_path"] == f"/projects/{target_id}"
                sut_engine = database.create_db_engine(
                    f"sqlite:///{(tmp_path / 'teamflow.db').as_posix()}"
                )
                factory = database.make_session_factory(sut_engine)
                with factory() as session:
                    assert not any(
                        p.name.startswith("TG_RUN_") for p in session.scalars(select(Project))
                    )
                sut_engine.dispose()
