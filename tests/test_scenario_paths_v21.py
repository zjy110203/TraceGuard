"""Explicit opt-in path Oracles must prove actual browser outcomes."""

from __future__ import annotations

import json
from pathlib import Path

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


@pytest.mark.parametrize("sid", SCENARIOS)
def test_all_four_paths_have_passing_real_browser_verdicts(tmp_path: Path, sid: str) -> None:
    scenario = default_registry().require(sid, "2.1.0")
    with _isolated_teamflow(tmp_path) as base:
        settings = Settings(
            app_env="test",
            database_url=f"sqlite:///{(tmp_path / 'teamflow.db').as_posix()}",
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
                assert record["data_lifecycle"]["state"] == "CLEANED"
                assert verify_trace_record(record)
                detail = client.get(f"/api/v1/explorations/{record['exploration_id']}").json()
                assert detail["scenario_digest_verified"] is True
                assert detail["goal_assertions_verified"] is True
                compiled = client.post(
                    f"/api/v1/explorations/{record['exploration_id']}/compile", json={}
                )
                if spec["expected_goal_status"] == "NOT_VERIFIED":
                    assert compiled.status_code == 409, compiled.text
                    assert compiled.json()["error"]["details"]["goal_status"] == "NOT_VERIFIED"
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
                factory = database.make_session_factory(client.app.state.engine)
                with factory() as session:
                    assert not any(
                        p.name.startswith("TG_RUN_") for p in session.scalars(select(Project))
                    )
