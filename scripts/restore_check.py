"""Restore and serve an isolated R5 deployment backup, including a real browser run.

The source package is read only. The restored copy is placed below ``--work``;
both services use only the explicitly declared shared/separate topology and data roles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.agents.exploration import verify_trace_record  # noqa: E402
from packages.core.metadata import PLATFORM_TABLES  # noqa: E402
from packages.execution.candidate_transaction import digest as candidate_digest  # noqa: E402
from packages.execution.candidate_transaction import json_bytes as candidate_json_bytes  # noqa: E402
from scripts.local_backup import restore, validate  # noqa: E402

TEAMFLOW_TABLES = (
    "tf_project",
    "tf_workspace",
    "tf_test_user",
    "eval_fault_config",
    "eval_scenario",
    "eval_ground_truth",
    "eval_event_log",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(
    base: str, timeout: float = 60.0, process: subprocess.Popen[bytes] | None = None
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            if httpx.get(f"{base}/health", timeout=1.0, trust_env=False).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


def _database_file(root: Path, role: str) -> Path:
    files = [path for path in (root / "payload" / role).iterdir() if path.is_file()]
    if len(files) != 1:
        raise ValueError(f"restored role {role} must contain exactly one database file")
    return files[0]


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
        return {
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _validate_historical_assets(root: Path, platform_db: Path) -> dict[str, int]:
    """Check restored lineage, formal versions, repair journals and blob refs."""
    payload = root / "payload"
    case_root = payload / "cases"
    candidate_root = payload / "candidates"
    exploration_root = payload / "explorations"
    chain_root = payload / "chains"
    blob_root = payload / "blobs"
    benchmark_root = payload / "benchmark"

    def read_objects(paths: list[Path], label: str) -> list[tuple[Path, dict[str, Any]]]:
        records: list[tuple[Path, dict[str, Any]]] = []
        for path in paths:
            try:
                row = json.loads(path.read_bytes())
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"invalid {label} asset {path.name}: {type(exc).__name__}"
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(f"invalid {label} asset shape: {path.name}")
            records.append((path, row))
        return records

    case_rows = read_objects(
        [path for path in case_root.glob("*.json") if path.name.startswith("TC-")], "case"
    )
    cases: dict[tuple[str, int], dict[str, Any]] = {}
    for path, row in case_rows:
        case_id, version = row.get("case_id"), row.get("version")
        if not isinstance(case_id, str) or not isinstance(version, int):
            raise ValueError(f"formal case identity is incomplete: {path.name}")
        cases[(case_id, version)] = row

    explorations = read_objects(list(exploration_root.glob("exp_*.json")), "exploration")
    trace_hashes: set[str] = set()
    for path, row in explorations:
        if row.get("exploration_id") != path.stem or not verify_trace_record(row):
            raise ValueError(f"exploration trace integrity failed: {path.name}")
        trace_hash = row.get("source_trace_hash")
        if isinstance(trace_hash, str) and trace_hash:
            trace_hashes.add(trace_hash)

    candidates = read_objects(list(candidate_root.glob("cand_*.json")), "candidate")
    candidate_rows = {path.stem: row for path, row in candidates}
    for path, row in candidates:
        if row.get("candidate_id") != path.stem:
            raise ValueError(f"candidate identity mismatch: {path.name}")
        source_hash = row.get("source_trace_hash")
        if source_hash and source_hash not in trace_hashes:
            raise ValueError(f"candidate references a missing exploration trace: {path.name}")

    transaction_rows = read_objects(
        list((candidate_root / "transactions").glob("cand_*.json")),
        "candidate transaction",
    )
    for path, tx in transaction_rows:
        if tx.get("candidate_id") != path.stem:
            raise ValueError(f"candidate transaction identity mismatch: {path.name}")
        stage = tx.get("stage")
        if stage not in {
            "INTENT_DURABLE",
            "FORMAL_CASE_DURABLE",
            "CANDIDATE_STATE_DURABLE",
            "DONE",
            "BLOCKED",
        }:
            raise ValueError(f"unknown candidate transaction stage: {path.name}")
        for key in ("before", "after"):
            if candidate_digest(candidate_json_bytes(tx[key])) != tx.get(f"{key}_sha256"):
                raise ValueError(f"candidate transaction {key} digest mismatch: {path.name}")
        current = candidate_rows.get(path.stem)
        if stage != "BLOCKED" and current is not None:
            current_sha = candidate_digest(candidate_json_bytes(current))
            if current_sha not in {tx["before_sha256"], tx["after_sha256"]}:
                raise ValueError(
                    f"candidate state matches neither transaction snapshot: {path.name}"
                )
        if tx.get("formal") is not None and stage in {
            "FORMAL_CASE_DURABLE",
            "CANDIDATE_STATE_DURABLE",
            "DONE",
        }:
            if tx["formal"] not in cases.values():
                raise ValueError(f"candidate formal case reference is missing: {path.name}")

    with sqlite3.connect(f"file:{platform_db.as_posix()}?mode=ro", uri=True) as conn:
        table_names = {
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        run_ids = (
            {row[0] for row in conn.execute("SELECT run_id FROM tg_run")}
            if "tg_run" in table_names
            else set()
        )
        evidence_ids = (
            {row[0] for row in conn.execute("SELECT evidence_id FROM evd_record")}
            if "evd_record" in table_names
            else set()
        )

    chains = read_objects(list(chain_root.glob("chain_*.json")), "chain")
    blob_refs: set[str] = set()
    run_refs: set[str] = set()
    evidence_by_run: dict[str, set[str]] = {}
    for path, row in chains:
        if row.get("chain_id") != path.stem:
            raise ValueError(f"chain identity mismatch: {path.name}")
        run_id = row.get("run_id")
        if run_id and run_id not in run_ids:
            raise ValueError(f"chain run reference is missing: {path.name}")
        if run_id:
            run_refs.add(str(run_id))
        case_id, case_version = row.get("case_id"), row.get("case_version")
        if case_id and isinstance(case_version, int) and (case_id, case_version) not in cases:
            raise ValueError(f"chain formal case version is missing: {path.name}")
        for evidence in row.get("evidence", []):
            evidence_id = evidence.get("evidence_id") if isinstance(evidence, dict) else None
            if evidence_id and evidence_id not in evidence_ids:
                raise ValueError(f"chain evidence row is missing: {path.name}:{evidence_id}")
            if evidence_id and run_id:
                evidence_by_run.setdefault(str(run_id), set()).add(str(evidence_id))
            if not isinstance(evidence, dict):
                continue
            payload_ref = evidence.get("payload", {}).get("blob", {})
            key = payload_ref.get("key") if isinstance(payload_ref, dict) else None
            if isinstance(key, str) and len(key) == 64:
                blob_refs.add(key)
    for key in blob_refs:
        blob_path = blob_root / key[:2] / key[2:4] / key
        if not blob_path.is_file() or hashlib.sha256(blob_path.read_bytes()).hexdigest() != key:
            raise ValueError(f"chain content-addressed blob is missing or corrupt: {key}")

    repair_paths = list(benchmark_root.rglob("repairs.jsonl"))
    repair_snapshots = 0
    repair_ids: set[str] = set()
    for path in repair_paths:
        for offset, line in enumerate(path.read_bytes().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                snapshot = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"repair journal line is invalid: {path.name}:{offset}") from exc
            if not isinstance(snapshot, dict):
                raise ValueError(f"repair journal entry is not an object: {path.name}:{offset}")
            repair_snapshots += 1
            for proposal in snapshot.get("proposals", []):
                if proposal.get("proposal_id"):
                    repair_ids.add(str(proposal["proposal_id"]))
                for evidence_id in proposal.get("source_evidence_refs", []):
                    if evidence_id not in evidence_ids:
                        raise ValueError(
                            f"repair evidence reference is missing: {path.name}:{evidence_id}"
                        )
            for tx in snapshot.get("transactions", {}).values():
                status = tx.get("status")
                if status not in {"INTENT", "COMMITTED", "DONE", "BLOCKED"}:
                    raise ValueError(f"unknown repair transaction status: {path.name}:{status}")
                if status in {"COMMITTED", "DONE"} and tx.get("asset") not in cases.values():
                    raise ValueError(f"repair formal case version is missing: {path.name}")

    return {
        "formal_case_versions": len(cases),
        "explorations": len(explorations),
        "candidates": len(candidates),
        "candidate_transactions": len(transaction_rows),
        "chains": len(chains),
        "verified_blobs": len(blob_refs),
        "repair_snapshots": repair_snapshots,
        "chain_ids": [row.get("chain_id") for _, row in chains],
        "candidate_ids": sorted(candidate_rows),
        "repair_ids": sorted(repair_ids),
        "run_ids": sorted(run_refs),
        "evidence_by_run": {
            run_id: sorted(evidence_ids) for run_id, evidence_ids in sorted(evidence_by_run.items())
        },
    }


def _alembic_upgrade(env: dict[str, str]) -> tuple[bool, str, bool]:
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *args],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    stamped = False
    engine = create_engine(env["TRACEGUARD_DATABASE_URL"])
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        has_platform_schema = bool(table_names & set(PLATFORM_TABLES))
        has_revision = "alembic_version" in table_names
        has_latest_evidence_schema = "evd_record" in table_names and "attachment_ref" in {
            column["name"] for column in inspector.get_columns("evd_record")
        }
    finally:
        engine.dispose()

    if not has_revision and has_latest_evidence_schema:
        # SQLAlchemy-created deployments have the current schema without an
        # Alembic marker. Mark the verified head before requesting upgrade.
        proc = run("stamp", "head")
        stamped = proc.returncode == 0
    else:
        if not has_revision and has_platform_schema:
            # A pre-marker database with platform tables starts at the root
            # revision so only the additive revisions are applied.
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            base = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_base()
            proc = run("stamp", base)
            if proc.returncode:
                detail = " ".join(
                    ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-2:]
                )
                return False, f"stamp base failed: {detail[-180:]}", False
            stamped = True
        proc = run("upgrade", "head")

    detail = " ".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-2:])
    if proc.returncode and "already exists" in ((proc.stderr or "") + (proc.stdout or "")):
        # Do not guess a migration position if the restored schema is only
        # partially present. Let the operator inspect the full error report.
        return False, detail[:240], stamped
    return proc.returncode == 0, detail[:240], stamped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup",
        default=".traceguard-tmp/r5/backup-v2",
        help="verified traceguard-offline-v2 directory",
    )
    parser.add_argument("--work", default=".traceguard-tmp/r5/restore-v2")
    args = parser.parse_args()

    backup = Path(args.backup)
    if not backup.is_absolute():
        backup = ROOT / backup
    backup = backup.resolve()
    work = Path(args.work)
    if not work.is_absolute():
        work = ROOT / work
    work = work.resolve()
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "backup": str(backup),
        "steps": [],
    }

    def step(name: str, ok: bool, detail: str) -> None:
        report["steps"].append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    api: subprocess.Popen[bytes] | None = None
    sut: subprocess.Popen[bytes] | None = None
    api_log = None
    sut_log = None
    try:
        validation = validate(backup)
        manifest = json.loads((backup / "manifest.json").read_bytes())
        if manifest.get("format") != "traceguard-offline-v2":
            raise ValueError("full runtime verification requires traceguard-offline-v2")
        topology = manifest.get("topology")
        if topology not in {"shared", "separate"}:
            raise ValueError("backup topology must be explicitly shared or separate")
        step(
            "backup-verified",
            True,
            f"payload_files={validation['files']} sqlite={validation['sqlite']}",
        )
        work.mkdir(parents=True, exist_ok=True)
        restored_root = work / f"restored-{datetime.now(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
        restored = restore(backup, restored_root, offline=True)
        report["restored_root"] = str(restored_root)
        report["topology"] = topology
        step("restore-copy", True, f"activated={restored['activated']} root={restored_root}")

        payload = restored_root / "payload"
        if topology == "shared":
            platform_db = _database_file(restored_root, "database")
            teamflow_db = platform_db
        else:
            platform_db = _database_file(restored_root, "platform_database")
            teamflow_db = _database_file(restored_root, "teamflow_database")
        topology_ok = (platform_db.resolve() == teamflow_db.resolve()) == (topology == "shared")
        step(
            "database-topology",
            topology_ok,
            f"topology={topology} platform={platform_db.name} teamflow={teamflow_db.name}",
        )

        common_env = os.environ.copy()
        common_env.update(
            {
                "APP_ENV": "test",
                "PYTHONUTF8": "1",
                "EVAL_CONTROL_TOKEN": f"restore-check-{secrets.token_hex(16)}",
                "TEAMFLOW_TEST_USER": "restore_checker",
                "TEAMFLOW_TEST_PASSWORD": "restore-check-pass-123",
                "TEAMFLOW_HOST": "127.0.0.1",
                "WORKBENCH_ENABLED": "true",
                "WORKBENCH_WORKERS": "1",
                "CASE_DIR": str(payload / "cases"),
                "CANDIDATE_DATA_DIR": str(payload / "candidates"),
                "EXPLORATION_DATA_DIR": str(payload / "explorations"),
                "CHAIN_DATA_DIR": str(payload / "chains"),
                "BLOB_DIR": str(payload / "blobs"),
                "BENCHMARK_DATA_DIR": str(payload / "benchmark"),
            }
        )
        sut_port = _free_port()
        api_port = _free_port()
        sut_base = f"http://127.0.0.1:{sut_port}"
        api_base = f"http://127.0.0.1:{api_port}"
        sut_database_url = f"sqlite:///{teamflow_db.as_posix()}"
        platform_database_url = f"sqlite:///{platform_db.as_posix()}"
        explicit_teamflow_database_url = "" if topology == "shared" else sut_database_url
        sut_env = common_env | {
            "DATABASE_URL": sut_database_url,
            "TEAMFLOW_DATABASE_URL": explicit_teamflow_database_url,
            "TEAMFLOW_PORT": str(sut_port),
        }
        api_env = common_env | {
            "DATABASE_URL": platform_database_url,
            "TEAMFLOW_DATABASE_URL": explicit_teamflow_database_url,
            "TEAMFLOW_PORT": str(sut_port),
            "API_PORT": str(api_port),
        }
        api_env["TRACEGUARD_DATABASE_URL"] = api_env["DATABASE_URL"]
        sut_env["TRACEGUARD_DATABASE_URL"] = sut_env["DATABASE_URL"]

        migrated, migration_detail, stamped = _alembic_upgrade(api_env)
        step(
            "alembic-upgrade-head",
            migrated,
            f"stamped_base={stamped} detail={migration_detail or 'no migration output'}",
        )

        sut_log = (work / f"{restored_root.name}-teamflow.log").open("w", encoding="utf-8")
        sut = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.teamflow.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(sut_port),
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT),
            env=sut_env,
            stdout=sut_log,
            stderr=subprocess.STDOUT,
        )
        sut_healthy = _wait_health(sut_base, process=sut)
        step("teamflow-started", sut_healthy, sut_base if sut_healthy else "no /health 200")

        api_log = (work / f"{restored_root.name}-platform.log").open("w", encoding="utf-8")
        api = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT),
            env=api_env,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        api_healthy = _wait_health(api_base, process=api)
        step("platform-started", api_healthy, api_base if api_healthy else "no /health 200")

        platform_tables = _tables(platform_db)
        sut_tables = _tables(teamflow_db)
        if topology == "shared":
            combined_tables = set(PLATFORM_TABLES) | set(TEAMFLOW_TABLES)
            platform_schema_ok = combined_tables <= platform_tables
            sut_schema_ok = combined_tables <= sut_tables
        else:
            platform_schema_ok = (
                set(PLATFORM_TABLES) <= platform_tables and "tf_project" not in platform_tables
            )
            sut_schema_ok = set(TEAMFLOW_TABLES) <= sut_tables and "tg_task" not in sut_tables
        step(
            "platform-schema",
            platform_schema_ok,
            f"required={len(PLATFORM_TABLES)} topology={topology}",
        )
        step(
            "teamflow-schema",
            sut_schema_ok,
            f"required={len(TEAMFLOW_TABLES)} topology={topology}",
        )

        historical_ok = False
        try:
            historical = _validate_historical_assets(restored_root, platform_db)
            report["historical_assets"] = historical
            historical_ok = True
            historical_detail = json.dumps(historical, ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001 - record the first broken lineage reference
            report["historical_assets_error"] = f"{type(exc).__name__}: {str(exc)[:220]}"
            historical_detail = report["historical_assets_error"]
        step("restored-historical-assets", historical_ok, historical_detail)

        history_api_ok = False
        if api_healthy and historical_ok:
            history_lists = {
                "chains": httpx.get(f"{api_base}/api/v1/chains", timeout=20, trust_env=False),
                "candidates": httpx.get(
                    f"{api_base}/api/v1/candidates", timeout=20, trust_env=False
                ),
                "repairs": httpx.get(f"{api_base}/api/v1/repairs", timeout=20, trust_env=False),
            }
            history_api_ok = all(response.status_code == 200 for response in history_lists.values())
            if history_api_ok:
                chain_ids = {row.get("chain_id") for row in history_lists["chains"].json()}
                candidate_ids = {
                    row.get("candidate_id") for row in history_lists["candidates"].json()
                }
                repair_ids = {row.get("proposal_id") for row in history_lists["repairs"].json()}
                history_api_ok = (
                    set(historical["chain_ids"]) <= chain_ids
                    and set(historical["candidate_ids"]) <= candidate_ids
                    and set(historical["repair_ids"]) <= repair_ids
                )
                if history_api_ok:
                    for chain_id in historical["chain_ids"][:1]:
                        history_api_ok = (
                            httpx.get(
                                f"{api_base}/api/v1/chains/{chain_id}",
                                timeout=20,
                                trust_env=False,
                            ).status_code
                            == 200
                        )
                    for candidate_id in historical["candidate_ids"][:1]:
                        history_api_ok = history_api_ok and (
                            httpx.get(
                                f"{api_base}/api/v1/candidates/{candidate_id}",
                                timeout=20,
                                trust_env=False,
                            ).status_code
                            == 200
                        )
                    for repair_id in historical["repair_ids"][:1]:
                        history_api_ok = history_api_ok and (
                            httpx.get(
                                f"{api_base}/api/v1/repairs/{repair_id}",
                                timeout=20,
                                trust_env=False,
                            ).status_code
                            == 200
                        )
                    for run_id in historical["run_ids"][:1]:
                        evidence_response = httpx.get(
                            f"{api_base}/api/v1/evidence/records",
                            params={"run_id": run_id},
                            timeout=20,
                            trust_env=False,
                        )
                        evidence_ids = (
                            {row.get("evidence_id") for row in evidence_response.json()}
                            if evidence_response.status_code == 200
                            else set()
                        )
                        expected_evidence = set(historical["evidence_by_run"].get(run_id, []))
                        history_api_ok = history_api_ok and expected_evidence <= evidence_ids
            history_detail = json.dumps(
                {
                    "chains": len(historical["chain_ids"]),
                    "candidates": len(historical["candidate_ids"]),
                    "repairs": len(historical["repair_ids"]),
                    "run_ids": len(historical["run_ids"]),
                },
                ensure_ascii=False,
            )
        else:
            history_detail = "Platform unavailable or static historical validation failed"
        step("restored-history-api", history_api_ok, history_detail)

        endpoints_ok = False
        browser_ok = False
        if api_healthy and sut_healthy:
            overview = httpx.get(f"{api_base}/api/v1/overview", timeout=20, trust_env=False)
            cases = httpx.get(f"{api_base}/api/v1/cases", timeout=20, trust_env=False)
            tasks = httpx.get(f"{api_base}/api/v1/tasks", timeout=20, trust_env=False)
            endpoints_ok = all(r.status_code == 200 for r in (overview, cases, tasks))
            endpoint_detail = (
                f"overview={overview.status_code} cases={cases.status_code} "
                f"tasks={tasks.status_code}"
            )
            step(
                "restored-product-endpoints",
                endpoints_ok,
                endpoint_detail,
            )

            response = httpx.post(
                f"{api_base}/api/v1/explorations",
                json={
                    "scenario": "project-create",
                    "scenario_version": "2.1.0",
                    "path": "normal",
                    "base_url": sut_base,
                    "policy": "scripted",
                    "max_steps": 30,
                },
                timeout=180,
                trust_env=False,
            )
            record = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            evidence = record.get("business_state_evidence", {})
            browser_ok = (
                response.status_code == 201
                and record.get("test_case_verdict") == "PASSED"
                and evidence.get("verdict") == "PASS"
                and evidence.get("cleanup", {}).get("verdict") == "PASS"
                and not any(
                    row.get("name", "").startswith(f"TG_RUN_{record.get('exploration_id', '')}_")
                    for sample in evidence.get("snapshots", [])
                    if sample.get("phase") == "after_cleanup"
                    for row in sample.get("state", {}).get("projects", [])
                )
            )
            report["browser_run"] = {
                "http_status": response.status_code,
                "exploration_id": record.get("exploration_id"),
                "goal_status": record.get("goal_status"),
                "test_case_verdict": record.get("test_case_verdict"),
                "business_state_verdict": evidence.get("verdict"),
                "cleanup_verdict": evidence.get("cleanup", {}).get("verdict"),
                "eval_token_leaked": common_env["EVAL_CONTROL_TOKEN"] in json.dumps(record),
            }
            browser_ok = browser_ok and not report["browser_run"]["eval_token_leaked"]
            step(
                "restored-browser-business-run",
                browser_ok,
                json.dumps(report["browser_run"], ensure_ascii=False),
            )

            with httpx.Client(base_url=sut_base, timeout=10, trust_env=False) as client:
                login = client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": common_env["TEAMFLOW_TEST_USER"],
                        "password": common_env["TEAMFLOW_TEST_PASSWORD"],
                    },
                )
                state = (
                    client.get("/api/v1/state/independent") if login.status_code == 200 else None
                )
            state_ok = (
                state is not None
                and state.status_code == 200
                and state.json().get("project_count") == 0
            )
            step(
                "restored-teamflow-state-clean",
                state_ok,
                f"login={login.status_code} state={state.status_code if state else 'not-read'}",
            )

        report["finished_at"] = datetime.now(UTC).isoformat()
        report["ok"] = all(item["ok"] for item in report["steps"])
    except Exception as exc:  # noqa: BLE001 - preserve an actionable machine report
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:300]
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["ok"] = False
        step("restore-check-error", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        for process in (api, sut):
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for handle in (api_log, sut_log):
            if handle is not None:
                handle.close()

    work.mkdir(parents=True, exist_ok=True)
    report_path = work / "restore-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"RESULT: {'PASS' if report['ok'] else 'FAIL'} -> {report_path}", flush=True)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
