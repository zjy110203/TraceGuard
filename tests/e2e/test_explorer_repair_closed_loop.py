"""One persisted Explorer lineage through real drift, repair and immutable rollback."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from apps.teamflow.main import build_app as build_teamflow
from packages.core.config import Settings
from packages.execution.browser import browser_channel
from packages.execution.repair import RepairStore
from tests.e2e.test_user_journey import (
    test_user_journey_explore_compile_review_run as _initial_journey,
)
from tests.netutil import free_port, serve_uvicorn

ROOT = Path(__file__).resolve().parents[2]


class Platform:
    def __init__(self, work: Path, teamflow: str):
        self.work = work
        self.teamflow = teamflow
        self.process = None
        self.log = None
        self.generation = 0
        self.base = ""

    def start(self):
        self.generation += 1
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        env = {k.upper(): v for k, v in os.environ.items()}
        db_url = f"sqlite:///{(self.work / 'platform.db').as_posix()}"
        env.update(
            {
                "APP_ENV": "test",
                "DATABASE_URL": db_url,
                "TRACEGUARD_DATABASE_URL": db_url,
                "PYTHONUTF8": "1",
                "TEAMFLOW_PORT": self.teamflow.rsplit(":", 1)[1],
                "WORKBENCH_ENABLED": "true",
                "WORKBENCH_WORKERS": "1",
            }
        )
        for key, directory in {
            "CASE_DIR": "cases",
            "CHAIN_DATA_DIR": "chains",
            "BLOB_DIR": "blobs",
            "CANDIDATE_DATA_DIR": "candidates",
            "EXPLORATION_DATA_DIR": "explorations",
            "BENCHMARK_DATA_DIR": "benchmark",
        }.items():
            env[key] = str(self.work / directory)
        self.log = (self.work / f"api-{self.generation}.log").open("wb")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            assert self.process.poll() is None, "API exited; inspect its retained log"
            try:
                if httpx.get(self.base + "/health", timeout=1, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise AssertionError("API startup timeout")

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()

    def call(self, method, path, body=None):
        response = httpx.request(method, self.base + path, json=body, timeout=240, trust_env=False)
        assert response.is_success, response.text
        return response.json()


def test_explorer_repair_closed_loop():
    work = ROOT / ".traceguard-tmp" / "r5" / "f7-closed-loop" / uuid.uuid4().hex
    work.mkdir(parents=True)
    sut = build_teamflow(
        Settings(app_env="test", database_url=f"sqlite:///{(work / 'teamflow.db').as_posix()}")
    )
    report = {"workspace": str(work), "stages": {}, "restarts": []}
    report_path = work / "closed-loop-report.json"

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with serve_uvicorn(sut) as teamflow, sync_playwright() as pw:
        platform = Platform(work, teamflow)
        browser = pw.chromium.launch(channel=browser_channel(), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(20000)
        try:
            platform.start()
            _initial_journey(page, platform.base, teamflow)
            page.screenshot(path=str(work / "01-explorer-approved.png"), full_page=True)
            exploration = platform.call("GET", "/api/v1/explorations")[0]
            candidates = platform.call("GET", "/api/v1/candidates")
            assert len(candidates) == 1
            candidate = candidates[0]
            candidate_id = candidate["candidate_id"]
            candidate = platform.call("GET", f"/api/v1/candidates/{candidate_id}")
            case_id = candidate["case_id"]
            formal = platform.call("GET", f"/api/v1/repairs/cases/{case_id}/formal")["case"]
            assert formal["version"] == 1
            report.update(
                {
                    "explorer_id": exploration["exploration_id"],
                    "candidate_id": candidate_id,
                    "testcase_id": case_id,
                }
            )
            report["stages"]["initial_run"] = "PASSED"
            save()

            headers = {"X-Eval-Token": "local-dev-eval-token-change-me"}
            changed = httpx.post(
                teamflow + "/__eval__/ui-revision",
                headers=headers,
                json={"revision": "v2"},
                trust_env=False,
            )
            assert changed.status_code == 200
            page.goto(teamflow)
            assert page.locator('[data-testid="login-submit-v2"]').count() == 1
            assert page.locator('[data-testid="login-submit"]').count() == 0
            page.screenshot(path=str(work / "02-real-ui-drift.png"))
            failed = platform.call("POST", "/api/v1/chains", {"case_id": case_id})
            assert failed["test_verdict"] == "FAILED", failed
            run_id = failed["run_id"]
            diagnosis = platform.call("GET", f"/api/v1/runs/{run_id}/diagnosis")
            hypothesis = next(
                h for h in diagnosis["hypotheses"] if h["hypothesis_id"] == "h-locator-missing"
            )
            assert hypothesis["status"] == "SUPPORTED"
            refs = [e["ref_id"] for e in hypothesis["supporting"]]
            assert refs
            report.update(
                {
                    "failed_run_id": run_id,
                    "diagnosis_id": diagnosis["diagnosis_id"],
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "evidence_refs": refs,
                }
            )
            report["stages"]["failed_run"] = failed["test_verdict"]
            report["stages"]["diagnosis"] = diagnosis["diagnosis_status"]
            save()

            repaired = json.loads(json.dumps(formal))
            repaired["version"] = 2
            step = next(s for s in repaired["steps"] if "login-submit" in s["target"])
            before = step["target"]
            step["target"] = '[data-testid="login-submit-v2"]'
            # Exercise the manual structured workbench against this same diagnosis.
            page.goto(platform.base + "/#/repairs")
            page.get_by_label("正式用例 ID").fill(case_id)
            page.get_by_role("button", name="加载正式资产").click()
            page.locator(".create-card .notice-text").filter(has_text="已加载正式资产").wait_for()
            page.get_by_label("来源 Run ID").fill(run_id)
            page.get_by_role("button", name="加载来源诊断").click()
            page.get_by_label("选择受支持假设").select_option(hypothesis["hypothesis_id"])
            expect(page.get_by_label("Supporting Evidence Ref", exact=False)).to_have_value(
                ", ".join(refs)
            )
            page.get_by_text("人工 Locator 编辑", exact=True).click()
            page.get_by_label("步骤", exact=False).select_option(step["step_id"])
            page.get_by_label("新 Locator", exact=True).fill(step["target"])
            page.get_by_role("button", name="应用编辑并生成 Diff").click()
            ui_candidate = json.loads(page.get_by_label("候选 TestCase JSON").input_value())
            assert ui_candidate == repaired
            expect(page.get_by_label("候选变更预览")).to_contain_text(before)
            expect(page.get_by_label("候选变更预览")).to_contain_text(step["target"])
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST" and response.url.endswith("/api/v1/repairs")
                )
            ) as creation:
                page.get_by_role("button", name="创建提案", exact=True).click()
            assert creation.value.status == 201, creation.value.text()
            proposal = creation.value.json()
            pid = proposal["proposal_id"]
            report["repair_proposal_id"] = pid
            for action, state in (("validate", "VALIDATED"), ("submit", "PENDING_APPROVAL")):
                assert platform.call("POST", f"/api/v1/repairs/{pid}/{action}")["state"] == state
                report["stages"][action] = state
            approved = platform.call(
                "POST",
                f"/api/v1/repairs/{pid}/approve",
                {"approver": "closed-loop-reviewer", "approval_record": "F7-review"},
            )
            assert approved["state"] == "APPROVED"
            replay = platform.call("POST", f"/api/v1/repairs/{pid}/sandbox-replay")[
                "sandbox_replay"
            ]
            assert replay["status"] == "PASSED"
            assert replay["evidence"]["baseline"]["test_verdict"] == "FAILED"
            assert replay["evidence"]["candidate"]["test_verdict"] == "PASSED"
            assert replay["evidence"]["ui_revision"] == "v2"
            report["replay_id"] = replay["replay_id"]
            report["stages"]["replay"] = replay["status"]
            promoted = platform.call(
                "POST",
                f"/api/v1/repairs/{pid}/promote",
                {"operator": "closed-loop-operator", "reason": "replay verified"},
            )
            assert promoted["state"] == "PROMOTED"
            report["promotion_id"] = promoted["promotion_record"]["promotion_id"]
            report["stages"]["promotion"] = promoted["state"]
            save()
            v1 = work / "cases" / f"{case_id}.json"
            v2 = work / "cases" / f"{case_id}.v2.json"
            before_restart = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (v1, v2)}
            platform.stop()
            platform.start()
            assert platform.call("GET", f"/api/v1/cases/{case_id}")["version"] == 2
            assert (
                platform.call("GET", f"/api/v1/repairs/{pid}")["promotion_record"]
                == promoted["promotion_record"]
            )
            report["restarts"].append({"after_promotion": "PASS", "file_hashes": before_restart})
            passed = platform.call("POST", "/api/v1/chains", {"case_id": case_id})
            assert passed["test_verdict"] == "PASSED", passed
            assert passed["case_version"] == 2
            report["regression_run_id"] = passed["run_id"]
            report["stages"]["regression"] = "PASSED"

            page.goto(platform.base + "/#/repairs")
            page.wait_for_selector('[data-testid="rollback-form"]')
            page.screenshot(path=str(work / "03-promoted-repair.png"), full_page=True)
            page.get_by_label("回滚目标历史版本").fill("1")
            page.get_by_label("回滚操作者").fill("closed-loop-operator")
            page.get_by_label("回滚理由").fill("restore historical asset for audit")
            page.get_by_role("button", name="追加回滚版本", exact=True).click()
            page.wait_for_selector("text=ROLLED_BACK")
            rolled = platform.call("GET", f"/api/v1/repairs/{pid}")
            assert rolled["state"] == "ROLLED_BACK"
            report["rollback_id"] = rolled["promotion_record"]["rollback_id"]
            report["stages"]["rollback"] = "ROLLED_BACK"
            page.screenshot(path=str(work / "04-rollback-history.png"), full_page=True)
            platform.stop()
            platform.start()
            assert platform.call("GET", f"/api/v1/cases/{case_id}")["version"] == 3
            assert (
                platform.call("GET", f"/api/v1/repairs/{pid}")["promotion_record"]
                == rolled["promotion_record"]
            )
            for name, digest in before_restart.items():
                assert hashlib.sha256((work / "cases" / name).read_bytes()).hexdigest() == digest
            restored = platform.call("POST", "/api/v1/chains", {"case_id": case_id})
            assert restored["case_version"] == 3
            assert restored["test_verdict"] == "FAILED", restored
            assert restored["test_verdict"] == failed["test_verdict"]
            report["rollback_run_id"] = restored["run_id"]
            report["stages"]["rollback_execution"] = "FAILED (expected: v1 selector under v2 UI)"
            report["restarts"].append({"after_rollback": "PASS", "immutable_history": "PASS"})

            # Verify the full lineage from disk, independently of response values.
            stored_candidate = json.loads(
                (work / "candidates" / f"{candidate_id}.json").read_text(encoding="utf-8")
            )
            assert stored_candidate["source_exploration_id"] == exploration["exploration_id"]
            candidate_tx = json.loads(
                (work / "candidates" / "transactions" / f"{candidate_id}.json").read_bytes()
            )
            assert candidate_tx["stage"] == "DONE"
            assert candidate_tx["after"] == stored_candidate
            assert stored_candidate["review_transaction_id"] == candidate_tx["transaction_id"]
            assert (
                hashlib.sha256((work / "cases" / f"{case_id}.json").read_bytes()).hexdigest()
                == candidate_tx["formal_sha256"]
            )
            report["candidate_transaction_id"] = candidate_tx["transaction_id"]
            snapshot = RepairStore(work / "benchmark" / "repairs.jsonl").latest()
            stored = next(p for p in snapshot["proposals"] if p["proposal_id"] == pid)
            assert stored["source_run_id"] == run_id
            assert stored["source_hypothesis_id"] == hypothesis["hypothesis_id"]
            assert stored["source_evidence_refs"] == refs
            stored_diag = json.loads(
                (work / "benchmark" / "diagnoses" / f"dg_{run_id}.json").read_text(encoding="utf-8")
            )
            assert stored_diag == diagnosis
            chain = json.loads(
                (work / "chains" / f"{failed['chain_id']}.json").read_text(encoding="utf-8")
            )
            assert chain["case_id"] == case_id
            assert chain["execution"]["run_id"] == run_id
            assert set(refs) <= {e["evidence_id"] for e in chain["evidence"]}
            assert snapshot["replays"][pid]["replay_id"] == report["replay_id"]
            assert {t["transaction_id"] for t in snapshot["transactions"].values()} == {
                report["promotion_id"],
                report["rollback_id"],
            }
            assert all(t["status"] == "DONE" for t in snapshot["transactions"].values())
            assert (
                json.loads((work / "cases" / f"{case_id}.v3.json").read_text(encoding="utf-8"))[
                    "rollback_of_version"
                ]
                == 1
            )
            report["persistent_hashes"] = {
                str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest()
                for directory in ("cases", "candidates", "chains", "explorations", "benchmark")
                for p in (work / directory).rglob("*")
                if p.is_file()
            }
            report["status"] = "PASS"
        finally:
            save()
            browser.close()
            platform.stop()
            print(f"CLOSED_LOOP_REPORT={report_path}")
