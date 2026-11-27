"""R5-5.5 real browser repair loop using a manually edited Locator candidate.

This acceptance path deliberately does not call or depend on a repair generator.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from apps.teamflow.main import build_app as build_teamflow
from packages.core.config import Settings
from packages.execution.browser import browser_channel
from scripts.local_backup import DIRECTORY_ROLES
from scripts.local_backup import create as create_backup
from tests.e2e.test_explorer_repair_closed_loop import Platform
from tests.e2e.test_user_journey import (
    test_user_journey_explore_compile_review_run as _initial_journey,
)
from tests.netutil import serve_uvicorn

ROOT = Path(__file__).resolve().parents[2]
CASE_SOURCES = ROOT / "benchmark" / "cases"


def test_manual_repair_restarts_rolls_back_and_restores_real_history(
    tmp_path: Path, monkeypatch
) -> None:
    work = tmp_path / "manual-repair-loop"
    work.mkdir()
    monkeypatch.delenv("TEAMFLOW_DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("EVAL_CONTROL_TOKEN", "local-dev-eval-token-change-me")
    monkeypatch.setenv("TEAMFLOW_TEST_USER", "tester")
    monkeypatch.setenv("TEAMFLOW_TEST_PASSWORD", "test-pass-123")

    case_dir = work / "cases"
    case_dir.mkdir()
    for name in ("TC-CREATE-001.json", "TC-CREATE-002.json", "locators.json"):
        shutil.copy2(CASE_SOURCES / name, case_dir / name)

    report_path = work / "r55-manual-repair-report.json"
    report: dict[str, object] = {"workspace": str(work), "status": "RUNNING", "stages": {}}

    def save_report() -> None:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    sut_settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{(work / 'teamflow.db').as_posix()}",
        eval_control_token="local-dev-eval-token-change-me",
    )
    platform = Platform(work, "")
    with serve_uvicorn(build_teamflow(sut_settings)) as teamflow:
        platform.teamflow = teamflow
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=browser_channel(), headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(20000)
            try:
                platform.start()
                _initial_journey(page, platform.base, teamflow)
                report["stages"]["explorer_candidate_human_review_run"] = "PASS"

                exploration = platform.call("GET", "/api/v1/explorations")[0]
                candidate = platform.call("GET", "/api/v1/candidates")[0]
                case_id = candidate["case_id"]
                formal = platform.call("GET", f"/api/v1/repairs/cases/{case_id}/formal")["case"]
                run_dir = next(s for s in formal["steps"] if "login-submit" in s["target"])
                old_target = run_dir["target"]
                new_target = '[data-testid="login-submit-v2"]'

                changed = httpx.post(
                    f"{teamflow}/__eval__/ui-revision",
                    headers={"X-Eval-Token": "local-dev-eval-token-change-me"},
                    json={"revision": "v2"},
                    timeout=10,
                    trust_env=False,
                )
                assert changed.status_code == 200
                failed = platform.call("POST", "/api/v1/chains", {"case_id": case_id})
                assert failed["test_verdict"] == "FAILED", failed
                run_id = failed["run_id"]
                diagnosis = platform.call("GET", f"/api/v1/runs/{run_id}/diagnosis")
                hypothesis = next(
                    row
                    for row in diagnosis["hypotheses"]
                    if row["hypothesis_id"] == "h-locator-missing"
                )
                assert hypothesis["status"] == "SUPPORTED", diagnosis
                evidence_refs = [row["ref_id"] for row in hypothesis["supporting"]]
                assert evidence_refs
                report["stages"]["real_failed_run_and_supported_diagnosis"] = "PASS"

                manually_edited = json.loads(json.dumps(formal))
                manually_edited["version"] = formal["version"] + 1
                edited_step = next(
                    row for row in manually_edited["steps"] if row["step_id"] == run_dir["step_id"]
                )
                edited_step["target"] = new_target
                response = platform.call(
                    "POST",
                    "/api/v1/repairs",
                    {
                        "case_id": case_id,
                        "candidate": manually_edited,
                        "change_kinds": ["locator_update"],
                        "diff": [
                            {
                                "kind": "locator_update",
                                "target": run_dir["step_id"],
                                "before": old_target,
                                "after": new_target,
                            }
                        ],
                        "source_run_id": run_id,
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "evidence_refs": evidence_refs,
                    },
                )
                proposal_id = response["proposal_id"]
                assert response["state"] == "DRAFT"
                assert (
                    platform.call("GET", f"/api/v1/repairs/cases/{case_id}/formal")["case"][
                        "version"
                    ]
                    == formal["version"]
                )
                assert (
                    platform.call("POST", f"/api/v1/repairs/{proposal_id}/validate")["state"]
                    == "VALIDATED"
                )
                assert (
                    platform.call("POST", f"/api/v1/repairs/{proposal_id}/submit")["state"]
                    == "PENDING_APPROVAL"
                )
                approved = platform.call(
                    "POST",
                    f"/api/v1/repairs/{proposal_id}/approve",
                    {"approver": "r55-human-reviewer", "approval_record": "R55-MANUAL-001"},
                )
                assert approved["state"] == "APPROVED"
                replay = platform.call("POST", f"/api/v1/repairs/{proposal_id}/sandbox-replay")[
                    "sandbox_replay"
                ]
                assert replay["status"] == "PASSED", replay
                promoted = platform.call(
                    "POST",
                    f"/api/v1/repairs/{proposal_id}/promote",
                    {"operator": "r55-human-operator", "reason": "manual replay reviewed"},
                )
                assert promoted["state"] == "PROMOTED"
                report["stages"]["manual_locator_review_sandbox_promotion"] = "PASS"
                report["candidate_id"] = candidate["candidate_id"]
                report["exploration_id"] = exploration["exploration_id"]
                report["failed_run_id"] = run_id
                report["proposal_id"] = proposal_id
                report["evidence_refs"] = evidence_refs
                save_report()

                base_case_path = case_dir / f"{case_id}.json"
                version_two_path = case_dir / f"{case_id}.v2.json"
                immutable_hashes = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (base_case_path, version_two_path)
                }
                platform.stop()
                platform.start()
                assert platform.call("GET", f"/api/v1/cases/{case_id}")["version"] == 2
                assert (
                    platform.call("GET", f"/api/v1/repairs/{proposal_id}")["promotion_record"]
                    == promoted["promotion_record"]
                )
                regression = platform.call("POST", "/api/v1/chains", {"case_id": case_id})
                assert regression["case_version"] == 2
                assert regression["test_verdict"] == "PASSED", regression
                report["stages"]["restart_and_regression"] = "PASS"
                report["regression_run_id"] = regression["run_id"]

                rolled_back = platform.call(
                    "POST",
                    f"/api/v1/repairs/{proposal_id}/rollback",
                    {
                        "operator": "r55-human-operator",
                        "reason": "verify immutable historical selector",
                        "target_version": 1,
                    },
                )
                assert rolled_back["state"] == "ROLLED_BACK"
                platform.stop()
                platform.start()
                assert platform.call("GET", f"/api/v1/cases/{case_id}")["version"] == 3
                assert (
                    platform.call("GET", f"/api/v1/repairs/{proposal_id}")["promotion_record"]
                    == rolled_back["promotion_record"]
                )
                for name, digest in immutable_hashes.items():
                    assert hashlib.sha256((case_dir / name).read_bytes()).hexdigest() == digest
                historical = platform.call("POST", "/api/v1/chains", {"case_id": case_id})
                assert historical["case_version"] == 3
                assert historical["test_verdict"] == "FAILED", historical
                report["stages"]["rollback_restart_historical_run"] = "PASS"
                report["rollback_run_id"] = historical["run_id"]
                save_report()

                chain = json.loads((work / "chains" / f"{failed['chain_id']}.json").read_bytes())
                assert chain["run_id"] == run_id
                assert set(evidence_refs) <= {row["evidence_id"] for row in chain["evidence"]}
                repair_snapshot = json.loads(
                    (work / "benchmark" / "repairs.jsonl").read_bytes().splitlines()[-1]
                )
                stored_proposal = next(
                    row for row in repair_snapshot["proposals"] if row["proposal_id"] == proposal_id
                )
                assert stored_proposal["source_run_id"] == run_id
                assert set(stored_proposal["source_evidence_refs"]) == set(evidence_refs)
                report["stages"]["persisted_lineage_evidence_and_repair_history"] = "PASS"
                report["status"] = "PASS"
                save_report()
            finally:
                browser.close()
                platform.stop()

    plan: dict[str, str] = {
        "topology": "separate",
        "platform_database": str(work / "platform.db"),
        "teamflow_database": str(work / "teamflow.db"),
    }
    for role in DIRECTORY_ROLES:
        plan[role] = str(work / role)
    backup = tmp_path / "r55-offline-backup"
    assert create_backup(plan, backup, offline=True)["status"] == "VERIFIED"
    restored_work = tmp_path / "r55-restore-check"
    restored = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore_check.py"),
            "--backup",
            str(backup),
            "--work",
            str(restored_work),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    assert restored.returncode == 0, f"stdout:\n{restored.stdout}\nstderr:\n{restored.stderr}"
    restore_report = json.loads((restored_work / "restore-report.json").read_bytes())
    history = restore_report["historical_assets"]
    assert history["explorations"] >= 1
    assert history["candidate_transactions"] >= 1
    assert history["chains"] >= 3
    assert history["repair_snapshots"] >= 1
    assert history["verified_blobs"] >= 1
    assert restore_report["browser_run"]["business_state_verdict"] == "PASS"
    report["offline_backup_id"] = json.loads((backup / "manifest.json").read_bytes())["backup_id"]
    report["restore_report"] = str(restored_work / "restore-report.json")
    report["status"] = "PASS"
    save_report()
