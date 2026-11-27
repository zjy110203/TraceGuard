from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import build_app as build_platform_app
from apps.teamflow.main import build_app as build_teamflow_app
from packages.core.config import Settings
from scripts.local_backup import DIRECTORY_ROLES, create
from scripts.restore_check import _validate_historical_assets

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("topology", ["shared", "separate"])
def test_restored_multi_database_runtime_passes_real_browser_business_run(
    tmp_path: Path, topology: str
) -> None:
    teamflow_db = tmp_path / "teamflow.db"
    platform_db = tmp_path / "platform.db"
    if topology == "shared":
        teamflow_db = platform_db
    explicit_sut_db = "" if topology == "shared" else f"sqlite:///{teamflow_db.as_posix()}"
    test_user = "restore_checker"
    password = "restore-check-pass-123"

    teamflow_settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{teamflow_db.as_posix()}",
        teamflow_database_url=explicit_sut_db,
        teamflow_test_user=test_user,
        teamflow_test_password=password,
    )
    with TestClient(build_teamflow_app(teamflow_settings)) as client:
        assert client.get("/health").status_code == 200

    platform_settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{platform_db.as_posix()}",
        teamflow_database_url=explicit_sut_db,
        teamflow_test_user=test_user,
        teamflow_test_password=password,
        case_dir=str(tmp_path / "platform-cases"),
        candidate_data_dir=str(tmp_path / "platform-candidates"),
        exploration_data_dir=str(tmp_path / "platform-explorations"),
        chain_data_dir=str(tmp_path / "platform-chains"),
        blob_dir=str(tmp_path / "platform-blobs"),
        benchmark_data_dir=str(tmp_path / "platform-benchmark"),
    )
    with TestClient(build_platform_app(platform_settings)) as client:
        assert client.get("/health").status_code == 200

    plan: dict[str, str] = {"topology": topology}
    if topology == "shared":
        plan["database"] = str(platform_db)
    else:
        plan["platform_database"] = str(platform_db)
        plan["teamflow_database"] = str(teamflow_db)
    for role in DIRECTORY_ROLES:
        path = tmp_path / "role-sources" / role
        path.mkdir(parents=True)
        plan[role] = str(path)
    backup = tmp_path / "offline-v2"
    assert create(plan, backup, offline=True)["status"] == "VERIFIED"

    work = tmp_path / "restore-work"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore_check.py"),
            "--backup",
            str(backup),
            "--work",
            str(work),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    report = json.loads((work / "restore-report.json").read_bytes())
    assert report["ok"] is True
    assert {row["step"] for row in report["steps"] if row["ok"]} >= {
        "backup-verified",
        "restore-copy",
        "database-topology",
        "teamflow-started",
        "platform-started",
        "platform-schema",
        "teamflow-schema",
        "restored-historical-assets",
        "restored-history-api",
        "restored-product-endpoints",
        "restored-browser-business-run",
        "restored-teamflow-state-clean",
    }
    browser = report["browser_run"]
    assert browser["test_case_verdict"] == "PASSED"
    assert browser["business_state_verdict"] == "PASS"
    assert browser["cleanup_verdict"] == "PASS"
    assert browser["eval_token_leaked"] is False


def test_historical_chain_reference_damage_is_rejected(tmp_path: Path) -> None:
    restored_root = tmp_path / "restored"
    payload = restored_root / "payload"
    for role in (*DIRECTORY_ROLES, "platform_database"):
        (payload / role).mkdir(parents=True)
    chain_id = "chain_history_test"
    (payload / "chains" / f"{chain_id}.json").write_text(
        json.dumps({"chain_id": chain_id, "run_id": "run_missing"}), encoding="utf-8"
    )
    platform_db = payload / "platform_database" / "platform.db"
    with sqlite3.connect(platform_db) as conn:
        conn.execute("CREATE TABLE tg_run (run_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO tg_run VALUES ('run_present')")

    with pytest.raises(ValueError, match="chain run reference is missing"):
        _validate_historical_assets(restored_root, platform_db)


def test_restore_reports_teamflow_startup_failure_and_preserves_source(
    tmp_path: Path,
) -> None:
    platform_db = tmp_path / "platform.db"
    teamflow_db = tmp_path / "teamflow.db"
    with sqlite3.connect(teamflow_db) as conn:
        # Valid SQLite bytes with an incompatible application table: TeamFlow's
        # startup seed must fail rather than the restore checker masking it.
        conn.execute("CREATE TABLE tf_workspace (id TEXT PRIMARY KEY)")

    plan: dict[str, str] = {
        "topology": "separate",
        "platform_database": str(platform_db),
        "teamflow_database": str(teamflow_db),
    }
    with sqlite3.connect(platform_db) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
    for role in DIRECTORY_ROLES:
        path = tmp_path / "role-sources" / role
        path.mkdir(parents=True)
        plan[role] = str(path)
    backup = tmp_path / "offline-v2"
    assert create(plan, backup, offline=True)["status"] == "VERIFIED"

    work = tmp_path / "restore-work"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore_check.py"),
            "--backup",
            str(backup),
            "--work",
            str(work),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )

    assert result.returncode == 2
    report = json.loads((work / "restore-report.json").read_bytes())
    steps = {row["step"]: row["ok"] for row in report["steps"]}
    assert steps["restore-copy"] is True
    assert steps["teamflow-started"] is False
    assert report["ok"] is False
    with sqlite3.connect(teamflow_db) as conn:
        assert conn.execute("PRAGMA table_info(tf_workspace)").fetchall()[0][1] == "id"
