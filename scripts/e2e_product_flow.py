"""Product-level end-to-end acceptance for TraceGuard (R5-4.5).

What this verifies
------------------
One *real* product flow, with nothing mocked:

    isolated TeamFlow  ->  real browser exploration (scripted policy)
    -> structured trace  ->  compiled candidate (pending review)
    -> human approval  ->  formal case written
    -> FullChain execution under a first-class Run
    -> business-level Oracle verdict  ->  events / evidence / blobs persisted
    -> correlation graph  ->  diagnosis  ->  run details
    -> negative control (action ok, business false => never PASSED)
    -> cancel in flight + retry + cleanup

Every step records a real assertion about observed state (an HTTP body, a file
on disk, a byte-level hash). A step is ``PASS``, ``FAIL`` or ``SKIP``; ``SKIP``
only happens when a prerequisite failed, never to fake success.

Isolation (why this never touches product data)
-----------------------------------------------
* TeamFlow gets its own SQLite file and its own port.
* The platform API gets its own SQLite file and its own port.
* every append-only store (cases / chains / blobs / candidates / explorations /
  benchmark) points into the run directory;
* the run directory is unique per invocation (timestamp + pid), so no state is
  ever reused between two runs;
* the product ``data/traceguard.db`` is hashed before and after; a change is a
  hard failure.

Usage
-----
    .venv/Scripts/python.exe scripts/e2e_product_flow.py
    .venv/Scripts/python.exe scripts/e2e_product_flow.py --build-web
    .venv/Scripts/python.exe scripts/e2e_product_flow.py --json out/result.json

Exit code: 0 only when every step passed and none was skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
PY = sys.executable

TERMINAL_RUN_STATES = frozenset({"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"})
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _is_windows() -> bool:
    return os.name == "nt"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class StepResult:
    name: str
    status: str
    detail: str
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.name,
            "status": self.status,
            "detail": self.detail,
            "duration_s": round(self.duration_s, 2),
        }


@dataclass
class FlowResult:
    work_dir: str
    base_url: str
    teamflow_url: str
    steps: list[StepResult] = field(default_factory=list)
    cases: dict[str, str] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.status == PASS)

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if s.status == FAIL]

    @property
    def skipped(self) -> list[StepResult]:
        return [s for s in self.steps if s.status == SKIP]

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped

    def summary(self) -> dict[str, Any]:
        return {
            "work_dir": self.work_dir,
            "base_url": self.base_url,
            "teamflow_url": self.teamflow_url,
            "total": len(self.steps),
            "passed": self.passed,
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "ok": self.ok,
            "failed_steps": [s.name for s in self.failed],
            "skipped_steps": [s.name for s in self.skipped],
            "steps": [s.to_dict() for s in self.steps],
            "cases": self.cases,
            "runs": self.runs,
            "artifacts": self.artifacts,
        }


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #


class Api:
    """Thin JSON client bound to one base URL (never proxies through env)."""

    def __init__(self, base: str) -> None:
        import httpx

        self.base = base
        self._client = httpx.Client(base_url=base, timeout=300.0, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, **params: Any) -> tuple[int, Any]:
        response = self._client.get(path, params=params or None)
        return response.status_code, _decode(response)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        response = self._client.post(path, json=payload)
        return response.status_code, _decode(response)

    def get_raw(self, path: str) -> tuple[int, str]:
        response = self._client.get(path)
        return response.status_code, response.text


def _decode(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - non-JSON is a finding, not a crash
        return {"_raw": response.text[:400]}


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #


class ProductFlow:
    def __init__(
        self,
        work: Path,
        *,
        build_web: bool = False,
        browser_channel: str = "chrome",
        keep_going: bool = True,
    ) -> None:
        self.work = work
        self.build_web = build_web
        self.browser_channel = browser_channel
        self.keep_going = keep_going
        self.result = FlowResult(work_dir=str(work), base_url="", teamflow_url="")
        self._procs: list[tuple[str, subprocess.Popen[bytes]]] = []
        self._logs: list[Any] = []
        self._step_started = 0.0
        self.api: Api | None = None
        self.tf: Api | None = None
        # Known test credential, generated here so the plaintext-leak check has a
        # value to search for without reading any real secret from the config.
        self.username = "e2e-reviewer"
        self.password = "e2e-" + hashlib.sha256(os.urandom(16)).hexdigest()[:18]
        self.stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")

    # ---------------------------------------------------------------- records

    def _record(self, name: str, status: str, detail: str) -> None:
        elapsed = time.monotonic() - self._step_started if self._step_started else 0.0
        self._step_started = 0.0
        self.result.steps.append(StepResult(name, status, detail, elapsed))
        flag = {PASS: "OK  ", FAIL: "FAIL", SKIP: "SKIP"}[status]
        print(f"[{flag}] {name}: {detail}", flush=True)

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self._record(name, PASS if ok else FAIL, detail)
        return ok

    def skip(self, name: str, reason: str) -> None:
        self._record(name, SKIP, reason)

    def begin(self) -> None:
        self._step_started = time.monotonic()

    # ------------------------------------------------------------- processes

    def _spawn(self, name: str, argv: list[str], env: dict[str, str], port: int) -> bool:
        log_path = self.work / f"{name}.log"
        handle = log_path.open("w", encoding="utf-8")
        self._logs.append(handle)
        proc = subprocess.Popen(  # noqa: S603 - argv is internal, no shell
            argv,
            cwd=str(ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        self._procs.append((name, proc))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print(f"[x] {name} exited early (rc={proc.returncode}); see {log_path.name}")
                return False
            if _healthy(f"http://127.0.0.1:{port}/health"):
                return True
            time.sleep(0.2)
        print(f"[x] {name} did not become healthy in time; see {log_path.name}")
        return False

    def _stop_all(self) -> None:
        for name, proc in reversed(self._procs):
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                print(f"[!] {name} did not stop gracefully; killed")
        for handle in self._logs:
            handle.close()

    # ------------------------------------------------------------------ steps

    def run(self) -> FlowResult:
        self.work.mkdir(parents=True, exist_ok=True)
        prod_db = ROOT / "data" / "traceguard.db"
        prod_fingerprint = (
            (sha256_file(prod_db), prod_db.stat().st_mtime_ns, prod_db.stat().st_size)
            if prod_db.exists()
            else None
        )

        tf_port = free_port()
        api_port = free_port()
        tf_base = f"http://127.0.0.1:{tf_port}"
        base = f"http://127.0.0.1:{api_port}"
        self.result.teamflow_url = tf_base
        self.result.base_url = base

        # ---- 1. isolation layout ------------------------------------------
        self.begin()
        dirs = {
            "cases": self.work / "cases",
            "chains": self.work / "chains",
            "blobs": self.work / "blobs",
            "candidates": self.work / "candidates",
            "explorations": self.work / "explorations",
            "benchmark": self.work / "benchmark",
            "traces": self.work / "traces",
        }
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        platform_db = self.work / "platform.db"
        teamflow_db = self.work / "teamflow.db"
        leftover_cases = sorted(dirs["cases"].glob("*.json"))
        outside = [
            p
            for p in (platform_db, teamflow_db, *dirs.values())
            if ROOT / "data" in p.parents and self.work != ROOT / "data"
        ]
        self.check(
            "isolation-layout",
            not leftover_cases and not outside and self.work.is_absolute(),
            f"work={_rel(self.work)} cases_before={len(leftover_cases)} "
            f"outside_data_dir={len(outside)} platform_db={_rel(platform_db)}",
        )

        # ---- 2/3. bring up the SUT and the platform ------------------------
        common = os.environ.copy()
        common.update(
            {
                "PYTHONUTF8": "1",
                "APP_ENV": "test",
                "TEAMFLOW_HOST": "127.0.0.1",
                "TEAMFLOW_PORT": str(tf_port),
                # Both processes read the same values, so the seeded account and
                # the provider-side credential agree by construction.
                "TEAMFLOW_TEST_USER": self.username,
                "TEAMFLOW_TEST_PASSWORD": self.password,
            }
        )
        tf_env = common | {
            "DATABASE_URL": f"sqlite:///{teamflow_db.as_posix()}",
            "TRACEGUARD_DATABASE_URL": f"sqlite:///{teamflow_db.as_posix()}",
        }
        api_env = common | {
            "DATABASE_URL": f"sqlite:///{platform_db.as_posix()}",
            "TRACEGUARD_DATABASE_URL": f"sqlite:///{platform_db.as_posix()}",
            "CASE_DIR": str(dirs["cases"]),
            "CHAIN_DATA_DIR": str(dirs["chains"]),
            "BLOB_DIR": str(dirs["blobs"]),
            "CANDIDATE_DATA_DIR": str(dirs["candidates"]),
            "EXPLORATION_DATA_DIR": str(dirs["explorations"]),
            "BENCHMARK_DATA_DIR": str(dirs["benchmark"]),
        }

        self.begin()
        tf_up = self._spawn(
            "teamflow",
            [
                PY,
                "-m",
                "uvicorn",
                "apps.teamflow.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(tf_port),
                "--log-level",
                "warning",
            ],
            tf_env,
            tf_port,
        )
        self.check(
            "teamflow-isolated-up",
            tf_up and teamflow_db.exists(),
            f"url={tf_base} db={_rel(teamflow_db)} exists={teamflow_db.exists()}",
        )
        if not tf_up:
            self.skip("platform-up", "TeamFlow failed to start")
            return self._finish(prod_fingerprint, prod_db)

        self.begin()
        api_up = self._spawn(
            "platform",
            [
                PY,
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
            api_env,
            api_port,
        )
        self.check(
            "platform-up",
            api_up,
            f"url={base} db={_rel(platform_db)} store_dirs=6 cases_dir={_rel(dirs['cases'])}",
        )
        if not api_up:
            self.skip("frontend-served", "platform API failed to start")
            return self._finish(prod_fingerprint, prod_db)

        self.api = Api(base)
        self.tf = Api(tf_base)
        try:
            self._flow(dirs, base, tf_base)
        finally:
            if self.api is not None:
                self.api.close()
            if self.tf is not None:
                self.tf.close()
            self._stop_all()
        return self._finish(prod_fingerprint, prod_db)

    # ------------------------------------------------------------------ flow

    def _flow(self, dirs: dict[str, Path], base: str, tf_base: str) -> None:
        api = self.api
        assert api is not None

        # ---- 4. frontend build artifacts ----------------------------------
        self.begin()
        index_html = WEB / "dist" / "index.html"
        built = index_html.exists()
        if not built and self.build_web:
            built = self._build_web()
        shell = index_html.read_text(encoding="utf-8") if built else ""
        has_app_mount = 'id="app"' in shell
        self.check(
            "frontend-built",
            built and "<script" in shell and has_app_mount,
            f"dist/index.html exists={built} bytes={len(shell)} has_app_mount={has_app_mount}",
        )

        # ---- 5. frontend served by the product API ------------------------
        self.begin()
        status, html = api.get_raw("/")
        assets_ok = "assets/" in html
        self.check(
            "frontend-served",
            status == 200 and assets_ok and len(html) > 200,
            f"GET / -> {status} bytes={len(html)} references_assets={assets_ok}",
        )

        # ---- 5b. the workbench UI really renders in a browser -------------
        self.begin()
        self._check_workbench_ui(base)

        # ---- 6. real browser exploration of the isolated SUT --------------
        self.begin()
        goal = "以测试账号登录 TeamFlow 并确认进入项目列表页"
        status, expl = api.post(
            "/api/v1/explorations",
            {
                "intent_ref": f"r5-4-5-{self.stamp}",
                "goal": goal,
                "expected_outcomes": ["新建项目"],
                # R5-4.6: the target is the human-declared SUCCESS page, not
                # the entry URL — the compiler verifies the trace actually
                # reached it before any candidate is produced.
                "target": "/projects",
                "required_assertions": ["state.project_count=0"],
                "constraints": ["仅访问本地 TeamFlow", "不得使用真实外部账号"],
                "base_url": tf_base,
                "policy": "scripted",
                "max_steps": 8,
            },
        )
        expl = expl if isinstance(expl, dict) else {}
        expl_id = str(expl.get("exploration_id") or "")
        explored = (
            status == 201
            and expl.get("status") == "COMPLETED"
            and expl.get("goal_status") == "VERIFIED"
            and expl.get("is_llm") is False
            and int(expl.get("step_count") or 0) >= 4
        )
        self.check(
            "explore-real-browser",
            explored,
            f"http={status} id={expl_id} status={expl.get('status')} "
            f"goal={expl.get('goal_status')} stop={expl.get('stop_reason')} "
            f"steps={expl.get('step_count')} policy={expl.get('policy')} "
            f"is_llm={expl.get('is_llm')} inference=NOT_REAL_INFERENCE",
        )
        if not explored:
            self._skip_rest("exploration did not complete")
            return

        # ---- 7. structured trace + integrity ------------------------------
        self.begin()
        status, trace = api.get(f"/api/v1/explorations/{expl_id}")
        trace = trace if isinstance(trace, dict) else {}
        observations = trace.get("observations") or []
        actions = trace.get("action_sequence") or []
        trace_hash = str(trace.get("source_trace_hash") or "")
        hash_hex = trace_hash.split(":", 1)[-1]
        keys_ok = bool(observations) and all(
            {"url", "visible_testids", "observed_at"} <= set(o) for o in observations
        )
        no_secret = self.password not in json.dumps(trace, ensure_ascii=False)
        self.check(
            "trace-structured",
            status == 200
            and keys_ok
            and len(actions) >= 4
            and trace.get("trace_integrity_verified") is True
            # A 16-char prefix would be 16, not 64: the hash must cover the whole
            # trace, otherwise the integrity claim is decorative.
            and len(hash_hex) == 64
            and no_secret,
            f"http={status} observations={len(observations)} actions={len(actions)} "
            f"hash_hex_len={len(hash_hex)} integrity={trace.get('trace_integrity_verified')} "
            f"no_plaintext_secret={no_secret}",
        )
        self.result.artifacts.append(_rel(dirs["explorations"]))

        # ---- 8. compile -> candidate (must stay pending) ------------------
        self.begin()
        case_id = f"TC-E2E-{self.stamp}"
        status, cand = api.post(
            f"/api/v1/explorations/{expl_id}/compile",
            {"case_id": case_id, "title": "R5-4.5 product flow: TeamFlow login"},
        )
        cand = cand if isinstance(cand, dict) else {}
        cand_id = str(cand.get("candidate_id") or "")
        pending = status == 201 and cand.get("status") == "PENDING_REVIEW"
        cand_sources = [
            str((s or {}).get("source_ref") or "") for s in cand.get("oracle_sources") or []
        ]
        self.check(
            "compile-candidate",
            pending and bool(cand.get("assertion_count")),
            f"http={status} candidate={cand_id} status={cand.get('status')} "
            f"steps={cand.get('step_count')} assertions={cand.get('assertion_count')} "
            f"oracle_sources={cand_sources} checklist={len(cand.get('review_checklist') or [])}",
        )
        if not pending:
            self._skip_rest("compile did not produce a pending candidate")
            return

        # ---- 9. nothing reaches the formal library before review ----------
        self.begin()
        written = sorted(p.name for p in dirs["cases"].glob("*.json"))
        status, blocked = api.post("/api/v1/chains", {"case_id": case_id})
        blocked_code = ""
        if isinstance(blocked, dict):
            blocked_code = str((blocked.get("error") or {}).get("code") or "")
        self.check(
            "no-case-before-review",
            not written and status in (404, 409),
            f"cases_in_library={written} POST /chains -> {status} blocked={blocked_code}",
        )

        # ---- 10. human approval (server-enforced review gate, R5-4.6) -----
        self.begin()
        status, cand_detail = api.get(f"/api/v1/candidates/{cand_id}")
        cand_detail = cand_detail if isinstance(cand_detail, dict) else {}
        required_ids = [
            str(item.get("id"))
            for item in cand_detail.get("review_checklist") or []
            if item.get("required", True)
        ]
        status, approved = api.post(
            f"/api/v1/candidates/{cand_id}/approve",
            {
                "reviewer": self.username,
                "approval_record": f"R5-4-5-REV-{self.stamp}",
                # The server refuses approval without explicit confirmation of
                # every required checklist item and of the uncertainty notes.
                "review_confirmed": required_ids,
                "uncertainty_acknowledged": True,
            },
        )
        approved = approved if isinstance(approved, dict) else {}
        case_file = str(approved.get("case_file") or "")
        self.check(
            "human-approval",
            status == 200 and approved.get("status") == "APPROVED" and bool(case_file),
            f"http={status} status={approved.get('status')} case_file={case_file} "
            f"reviewer={approved.get('reviewer')}",
        )
        if status != 200:
            self._skip_rest("approval failed")
            return
        self.result.cases["positive"] = case_id

        # ---- 11. formal case written and human-approved -------------------
        self.begin()
        status, case = api.get(f"/api/v1/cases/{case_id}")
        case = case if isinstance(case, dict) else {}
        bindings = case.get("oracle_bindings") or []
        sources = [str((b or {}).get("primary", {}).get("source_ref") or "") for b in bindings]
        case_json = json.dumps(case, ensure_ascii=False)
        business_sources = [s for s in sources if s.startswith(("ui.", "state."))]
        url_path_bound = any(s == "ui.url_path" for s in sources)
        independent_bound = any(s.startswith("state.") for s in sources)
        data_refs = case.get("data_refs") or []
        ref_bound = any(str((s or {}).get("value_ref") or "") for s in case.get("steps") or [])
        no_plaintext = self.password not in case_json and "«redacted»" not in case_json
        self.check(
            "formal-case-written",
            status == 200
            # promote() records "reviewer:approval_record" so the approval is
            # traceable to its record, not just to a name.
            and str(case.get("approved_by") or "").startswith(self.username)
            and int(case.get("version") or 0) >= 1
            and url_path_bound
            and bool(business_sources)
            and no_plaintext,
            f"http={status} version={case.get('version')} approved_by={case.get('approved_by')} "
            f"compiled_from={case.get('compiled_from')} sources={sources} "
            f"independent_api_oracle={independent_bound} data_refs={len(data_refs)} "
            f"value_ref_bound={ref_bound} no_plaintext={no_plaintext}",
        )
        self.result.artifacts.append(_rel(dirs["cases"]))

        # ---- 12. full-chain execution under a first-class Run ------------
        self.begin()
        status, chain = api.post("/api/v1/chains", {"case_id": case_id})
        chain = chain if isinstance(chain, dict) else {}
        run_id = str(chain.get("run_id") or "")
        chain_id = str(chain.get("chain_id") or "")
        stages = [str((s or {}).get("name") or "") for s in chain.get("stages") or []]
        required_stages = {"testcase", "execution", "verdict", "graph", "diagnosis", "cleanup"}
        persisted_status = str(chain.get("persistence_status") or "")
        self.check(
            "chain-executed",
            status == 201
            and bool(run_id)
            and chain.get("run_status") == "COMPLETED"
            and required_stages <= set(stages)
            and persisted_status in {"OK", "PARTIAL"}
            and not chain.get("graph_errors"),
            f"http={status} chain={chain_id} run={run_id} run_status={chain.get('run_status')} "
            f"verdict={chain.get('test_verdict')} exec={chain.get('execution_status')} "
            f"stages={stages} persistence={persisted_status} "
            f"graph_errors={chain.get('graph_errors')}",
        )
        if not run_id:
            self._skip_rest("chain run did not produce a run id")
            return
        self.result.runs["positive"] = run_id
        self.result.artifacts.append(_rel(dirs["chains"]))

        # full report: the assertions live here, not in the list projection
        _, report = api.get(f"/api/v1/chains/{chain_id}")
        report = report if isinstance(report, dict) else {}
        results = {
            str((r or {}).get("assertion_id")): r for r in report.get("assertion_results") or []
        }
        ui_observations = (report.get("observations") or {}).get("ui") or {}

        # ---- 13. the verdict is about the business goal, not the click ----
        self.begin()
        state_binding = next(
            (
                b
                for b in bindings
                if str((b or {}).get("primary", {}).get("source_ref") or "").startswith("state.")
            ),
            None,
        )
        state_assertion = str((state_binding or {}).get("assertion_id") or "")
        state_result = results.get(state_assertion) or {}
        url_result: dict[str, Any] = next(
            (
                r
                for b, r in (
                    (b, results.get(str((b or {}).get("assertion_id") or "")))
                    for b in bindings
                    if str((b or {}).get("primary", {}).get("source_ref")) == "ui.url_path"
                )
                if r
            ),
            {},
        )
        final_path = str(ui_observations.get("url_path") or "")
        evaluated = [
            r
            for r in results.values()
            if r.get("evaluated") is True and r.get("oracle_available") is True
        ]
        self.check(
            "oracle-business-verdict",
            chain.get("test_verdict") == "PASSED"
            and bool(state_assertion)
            and bool(url_result)
            and state_result.get("verdict") == "PASSED"
            and url_result.get("verdict") == "PASSED"
            and len(evaluated) == len(results)
            and final_path == "/projects",
            f"verdict={chain.get('test_verdict')} state_oracle={state_assertion}:"
            f"{state_result.get('verdict')} url_oracle={url_result.get('verdict')} "
            f"evaluated={len(evaluated)}/{len(results)} observed_path={final_path!r}",
        )

        # ---- 14. events persisted for this run and this workspace ---------
        self.begin()
        persisted = chain.get("persisted") or {}
        events_info = persisted.get("events") or {}
        status, events = api.get(
            "/api/v1/evidence/events", run_id=run_id, workspace_id="ws-default"
        )
        events = events if isinstance(events, list) else []
        own_run = [
            e
            for e in events
            if str(((e or {}).get("context_refs") or {}).get("run_id") or "") == run_id
        ]
        self.check(
            "events-persisted",
            status == 200
            and len(events) > 0
            and len(own_run) == len(events)
            and int(events_info.get("written") or 0) > 0,
            f"http={status} rows={len(events)} own_run={len(own_run)} "
            f"written={events_info.get('written')} duplicates={events_info.get('duplicates')} "
            f"status={events_info.get('status')}",
        )

        # ---- 15. evidence persisted for this run --------------------------
        self.begin()
        evidence_info = persisted.get("evidence") or {}
        status, records = api.get("/api/v1/evidence/records", run_id=run_id)
        records = records if isinstance(records, list) else []
        types = sorted({str((r or {}).get("evidence_type") or "") for r in records})
        with_blob = [r for r in records if ((r or {}).get("payload") or {}).get("blob")]
        self.check(
            "evidence-persisted",
            status == 200
            and len(records) > 0
            and int(evidence_info.get("written") or 0) > 0
            and bool(with_blob),
            f"http={status} records={len(records)} types={types} "
            f"written={evidence_info.get('written')} duplicates={evidence_info.get('duplicates')} "
            f"blobs={len(with_blob)} status={evidence_info.get('status')}",
        )

        # ---- 16. byte-level blob integrity -------------------------------
        self.begin()
        self._check_blobs(dirs["blobs"], records)

        # ---- 17. correlation graph ---------------------------------------
        self.begin()
        status_nodes, nodes = api.get("/api/v1/evidence/graph/nodes")
        nodes = nodes if isinstance(nodes, list) else []
        status_sub, subgraph = api.get(f"/api/v1/evidence/graph/subgraph/{run_id}:Run", depth=2)
        subgraph = subgraph if isinstance(subgraph, dict) else {}
        sub_nodes = subgraph.get("nodes") or []
        sub_edges = subgraph.get("edges") or []
        node_ids = {str((n or {}).get("node_id") or "") for n in sub_nodes}
        self.check(
            "graph-query",
            status_nodes == 200
            and len(nodes) > 0
            and status_sub == 200
            and f"{run_id}:Run" in node_ids
            and len(sub_edges) > 0,
            f"nodes={len(nodes)} subgraph_nodes={len(sub_nodes)} "
            f"subgraph_edges={len(sub_edges)} run_node_present={f'{run_id}:Run' in node_ids}",
        )

        # ---- 18. diagnosis ------------------------------------------------
        self.begin()
        status, diag = api.get(f"/api/v1/runs/{run_id}/diagnosis")
        diag = diag if isinstance(diag, dict) else {}
        self.check(
            "diagnosis-query",
            status == 200 and bool(diag.get("diagnosis_status") or diag.get("disposition")),
            f"http={status} diagnosis_status={diag.get('diagnosis_status')} "
            f"disposition={diag.get('disposition')}",
        )

        # ---- 19. run details + live progress trail -----------------------
        self.begin()
        status, run = api.get(f"/api/v1/runs/{run_id}")
        run = run if isinstance(run, dict) else {}
        status_report, run_report = api.get(f"/api/v1/runs/{run_id}/report")
        run_report = run_report if isinstance(run_report, dict) else {}
        report_stages = [str((s or {}).get("name") or "") for s in run_report.get("stages") or []]
        self.check(
            "run-details",
            status == 200
            and run.get("status") == "COMPLETED"
            and len(run.get("steps") or []) > 0
            and status_report == 200
            and "event_persistence" in report_stages
            and "evidence_persistence" in report_stages,
            f"http={status} status={run.get('status')} steps={len(run.get('steps') or [])} "
            f"verdict={run.get('test_verdict')} report_stages={report_stages}",
        )

        # ---- 20. negative control: action ok, business false --------------
        self.begin()
        self._check_no_false_pass(tf_base, dirs)

        # ---- 21. cancel in flight, then recover by retry -----------------
        self.begin()
        self._check_cancel_and_recover(case_id)

        # ---- 22. no plaintext secret anywhere in the artifacts -----------
        self.begin()
        leaks: list[str] = []
        scanned = 0
        for key in ("explorations", "chains", "candidates", "cases"):
            for path in sorted(dirs[key].rglob("*.json")):
                scanned += 1
                if self.password in path.read_text(encoding="utf-8", errors="replace"):
                    leaks.append(_rel(path))
        self.check(
            "no-plaintext-secret",
            not leaks and scanned > 0,
            f"files_scanned={scanned} leaks={leaks}",
        )
        self.result.artifacts.extend([_rel(dirs["candidates"]), _rel(dirs["blobs"])])

    # --------------------------------------------------------------- helpers

    def _skip_rest(self, reason: str) -> None:
        remaining = [
            "trace-structured",
            "compile-candidate",
            "no-case-before-review",
            "human-approval",
            "formal-case-written",
            "chain-executed",
            "oracle-business-verdict",
            "events-persisted",
            "evidence-persisted",
            "blob-integrity",
            "graph-query",
            "diagnosis-query",
            "run-details",
            "negative-control",
            "cancel-and-recover",
            "no-plaintext-secret",
        ]
        done = {s.name for s in self.result.steps}
        for name in remaining:
            if name not in done:
                self.skip(name, reason)

    def _build_web(self) -> bool:
        npm = shutil.which("npm.cmd" if _is_windows() else "npm")
        if npm is None:
            print("[x] npm not found; cannot build the frontend")
            return False
        print(f"$ (web) {npm} run build", flush=True)
        proc = subprocess.run(  # noqa: S603 - argv is internal, no shell
            [npm, "run", "build"],
            cwd=str(WEB),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        (self.work / "web-build.log").write_text(
            (proc.stdout or "") + (proc.stderr or ""), encoding="utf-8"
        )
        return proc.returncode == 0 and (WEB / "dist" / "index.html").exists()

    def _check_workbench_ui(self, base: str) -> None:
        """Load the product UI in a real browser (no assertions are faked)."""
        from playwright.sync_api import sync_playwright

        errors: list[str] = []
        try:
            with sync_playwright() as pw:
                chromium = pw.chromium
                browser = None
                if self.browser_channel:
                    try:
                        browser = chromium.launch(channel=self.browser_channel, headless=True)
                    except Exception as exc:  # noqa: BLE001 - channel may be absent
                        print(f"[i] channel '{self.browser_channel}' unavailable ({exc})")
                if browser is None:
                    browser = chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.on(
                    "console",
                    lambda m: errors.append(m.text) if m.type == "error" else None,
                )
                page.goto(f"{base}/#/candidates")
                page.wait_for_timeout(1200)
                mounted = page.evaluate("document.querySelector('#app')?.children.length ?? 0")
                body = page.inner_text("body")
                page.screenshot(path=str(self.work / "workbench-candidates.png"))
                browser.close()
        except Exception as exc:  # noqa: BLE001 - a browser failure is a finding
            self.check("workbench-ui-loads", False, f"{type(exc).__name__}: {exc}"[:200])
            return
        self.check(
            "workbench-ui-loads",
            bool(mounted) and len(body.strip()) > 40 and not errors,
            f"mount_children={mounted} text_len={len(body.strip())} console_errors={errors[:2]}",
        )

    def _check_blobs(self, blob_dir: Path, records: list[Any]) -> None:
        api = self.api
        assert api is not None
        checked = 0
        problems: list[str] = []
        for record in records:
            payload = (record or {}).get("payload") or {}
            blob = payload.get("blob") or {}
            key = str(blob.get("key") or "")
            # The persisted digest may carry the "sha256:" scheme prefix; what
            # matters is that it is the *full* digest of the stored bytes.
            expected = _hex_digest(blob.get("sha256"))
            declared = _hex_digest(payload.get("binary_sha256"))
            if not key or not expected:
                continue
            path = blob_dir / key[:2] / key[2:4] / key
            if not path.exists():
                problems.append(f"missing blob {key[:12]}")
                continue
            data = path.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
            checked += 1
            if len(expected) != 64 or actual != expected or (declared and actual != declared):
                problems.append(
                    f"hash mismatch {key[:12]} disk={actual[:12]} ref={expected[:12]} "
                    f"ref_len={len(expected)}"
                )
                continue
            if int(blob.get("size") or -1) != len(data):
                problems.append(f"size mismatch {key[:12]}")
                continue
            status, verdict = api.post(
                f"/api/v1/evidence/records/{record.get('evidence_id')}/verify",
                {"data_hex": data.hex()},
            )
            if status != 200 or not (verdict or {}).get("valid"):
                problems.append(
                    f"verify rejected {str(record.get('evidence_id'))[:12]}: "
                    f"{(verdict or {}).get('detail', '')[:40]}"
                )
        self.check(
            "blob-integrity",
            checked > 0 and not problems,
            f"blobs_checked={checked} full_sha256=True problems={problems[:3]}",
        )

    def _check_no_false_pass(self, tf_base: str, dirs: dict[str, Path]) -> None:
        """Action succeeds, business state does not => must not be PASSED.

        This is the R5-4.1 scenario D control case. The exploration and steps are
        identical to the passing one; only the declared business expectation
        (``state.project_count=1``) is false for a freshly reset SUT.
        """
        api = self.api
        assert api is not None
        status, expl = api.post(
            "/api/v1/explorations",
            {
                "intent_ref": f"r5-4-5-negative-{self.stamp}",
                "goal": "负向对照：登录动作成功但业务状态不满足预期",
                "expected_outcomes": ["新建项目"],
                "target": "/projects",
                "required_assertions": ["state.project_count=1"],
                "base_url": tf_base,
                "policy": "scripted",
                "max_steps": 8,
            },
        )
        expl = expl if isinstance(expl, dict) else {}
        if status != 201 or expl.get("status") != "COMPLETED":
            self.check("negative-control", False, f"exploration failed: http={status}")
            return
        case_id = f"TC-E2E-NEG-{self.stamp}"
        status, cand = api.post(
            f"/api/v1/explorations/{expl.get('exploration_id')}/compile",
            {"case_id": case_id, "title": "R5-4.5 negative control (business must fail)"},
        )
        cand = cand if isinstance(cand, dict) else {}
        cand_id = str(cand.get("candidate_id") or "")
        if status != 201:
            self.check("negative-control", False, f"compile failed: http={status} {cand}")
            return
        status, neg_detail = api.get(f"/api/v1/candidates/{cand_id}")
        neg_required = [
            str(item.get("id"))
            for item in (neg_detail if isinstance(neg_detail, dict) else {}).get("review_checklist")
            or []
            if item.get("required", True)
        ]
        api.post(
            f"/api/v1/candidates/{cand_id}/approve",
            {
                "reviewer": self.username,
                "approval_record": f"R5-4-5-NEG-{self.stamp}",
                "review_confirmed": neg_required,
                "uncertainty_acknowledged": True,
            },
        )
        # Approving the control case would pollute the library if it silently
        # failed, so refuse to continue unless it is really in the library.
        status, case = api.get(f"/api/v1/cases/{case_id}")
        if status != 200:
            self.check("negative-control", False, f"control case not approved: http={status}")
            return
        self.result.cases["negative"] = case_id

        status, chain = api.post("/api/v1/chains", {"case_id": case_id})
        chain = chain if isinstance(chain, dict) else {}
        run_id = str(chain.get("run_id") or "")
        if run_id:
            self.result.runs["negative"] = run_id
        report: dict[str, Any] = {}
        if chain.get("chain_id"):
            _, raw_report = api.get(f"/api/v1/chains/{chain.get('chain_id')}")
            report = raw_report if isinstance(raw_report, dict) else {}
        results = {
            str((r or {}).get("assertion_id")): r for r in report.get("assertion_results") or []
        }
        failed_assertions = [
            aid for aid, r in results.items() if str((r or {}).get("verdict")) == "FAILED"
        ]
        steps = report.get("steps") or []
        action_steps = [
            s for s in steps if str((s or {}).get("action")) in {"fill", "click", "navigate"}
        ]
        actions_ok = bool(action_steps) and all(bool((s or {}).get("ok")) for s in action_steps)
        self.check(
            "negative-control",
            status == 201
            and chain.get("test_verdict") not in (None, "PASSED")
            and bool(failed_assertions)
            and actions_ok,
            f"http={status} verdict={chain.get('test_verdict')} run={run_id} "
            f"failed_assertions={failed_assertions} actions_ok={actions_ok} "
            f"action_steps={len(action_steps)}",
        )

    def _check_cancel_and_recover(self, case_id: str) -> None:
        """Cancel a run while it executes, then recover by re-running."""
        api = self.api
        assert api is not None
        box: dict[str, Any] = {}

        def call() -> None:
            try:
                box["response"] = api.post("/api/v1/chains", {"case_id": case_id})
            except Exception as exc:  # noqa: BLE001 - surfaced below
                box["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=call, name="chain-cancel", daemon=True)
        worker.start()

        task_id = f"task_{case_id}"
        observed_state = ""
        cancelled_run = ""
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and worker.is_alive():
            status, history = api.get(f"/api/v1/tasks/{task_id}/runs")
            if status == 200 and isinstance(history, list) and history:
                rows = sorted(
                    history, key=lambda r: str((r or {}).get("created_at") or ""), reverse=True
                )
                row = rows[0] or {}
                run_id = str(row.get("run_id") or "")
                state = str(row.get("status") or "")
                if run_id and state not in TERMINAL_RUN_STATES:
                    observed_state = state
                    cancelled_run = run_id
                    api.post(f"/api/v1/runs/{run_id}/cancel")
                    break
            time.sleep(0.02)
        worker.join(timeout=600)

        response: tuple[int, Any] = box.get("response") or (0, {})
        chain = response[1] if isinstance(response[1], dict) else {}
        if not cancelled_run:
            self.check(
                "cancel-and-recover",
                False,
                f"run finished before cancel could be delivered "
                f"(observed={observed_state or 'none'}) error={box.get('error', '')}",
            )
            return
        run_id = str(chain.get("run_id") or "")
        stages = [str((s or {}).get("name") or "") for s in chain.get("stages") or []]
        cancelled_ok = bool(
            run_id == cancelled_run
            and chain.get("run_status") == "CANCELLED"
            and chain.get("test_verdict") != "PASSED"
            and chain.get("cancelled") is True
            and "cleanup" in stages
        )
        # Recovery: a fresh run must be created and must actually pass.
        status, retry = api.post("/api/v1/chains", {"case_id": case_id})
        retry = retry if isinstance(retry, dict) else {}
        retry_run = str(retry.get("run_id") or "")
        persisted = retry.get("persisted") or {}
        evidence_written = int((persisted.get("evidence") or {}).get("written") or 0)
        recovered = bool(
            status == 201
            and retry_run
            and retry_run != cancelled_run
            and retry.get("test_verdict") == "PASSED"
            and retry.get("run_status") == "COMPLETED"
            and evidence_written > 0
        )
        if retry_run:
            self.result.runs["retry"] = retry_run
        self.check(
            "cancel-and-recover",
            cancelled_ok and recovered,
            f"observed_state={observed_state} cancelled_run={cancelled_run} "
            f"run_status={chain.get('run_status')} verdict={chain.get('test_verdict')} "
            f"cleanup_stage={'cleanup' in stages} | retry_run={retry_run} "
            f"retry_verdict={retry.get('test_verdict')} evidence_written={evidence_written} "
            f"new_run_id={bool(retry_run) and retry_run != cancelled_run}",
        )

    # -------------------------------------------------------------- teardown

    def _finish(
        self,
        prod_fingerprint: tuple[str, int, int] | None,
        prod_db: Path,
    ) -> FlowResult:
        self.begin()
        now = (
            (sha256_file(prod_db), prod_db.stat().st_mtime_ns, prod_db.stat().st_size)
            if prod_db.exists()
            else None
        )
        self.check(
            "product-data-untouched",
            now == prod_fingerprint,
            f"path={_rel(prod_db)} identical={now == prod_fingerprint}",
        )
        return self.result


def _hex_digest(value: Any) -> str:
    """Normalise ``sha256:<hex>`` / ``<hex>`` into bare lowercase hex."""
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    return text


def _healthy(url: str) -> bool:
    import httpx

    try:
        response = httpx.get(url, timeout=1.0, trust_env=False)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_report(result: FlowResult, work: Path) -> Path:
    payload = result.summary()
    (work / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# Product E2E report", "", f"- work dir: `{_rel(work)}`", ""]
    lines.append("| step | status | detail |")
    lines.append("|------|--------|--------|")
    for step in result.steps:
        detail = step.detail.replace("|", "\\|")
        lines.append(f"| {step.name} | {step.status} | {detail} |")
    lines.append("")
    lines.append(
        f"**total={len(result.steps)} passed={result.passed} "
        f"failed={len(result.failed)} skipped={len(result.skipped)}**"
    )
    if result.failed:
        lines.append("")
        lines.append("Failed: " + ", ".join(s.name for s in result.failed))
    if result.skipped:
        lines.append("")
        lines.append("Skipped: " + ", ".join(s.name for s in result.skipped))
    report = work / "result.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TraceGuard product-level E2E")
    parser.add_argument(
        "--work-dir",
        default="",
        help="run directory (default: .traceguard-tmp/r5/product-e2e/<stamp>-<pid>)",
    )
    parser.add_argument(
        "--build-web",
        action="store_true",
        help="build apps/web before the flow (needed on a clean CI checkout)",
    )
    parser.add_argument(
        "--browser-channel",
        default=os.environ.get("TRACEGUARD_BROWSER_CHANNEL", "chrome"),
        help="Chromium channel; empty string uses Playwright's bundled build",
    )
    parser.add_argument("--json", default="", help="also write the JSON summary here")
    args = parser.parse_args(argv)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    work = (
        Path(args.work_dir)
        if args.work_dir
        else ROOT / ".traceguard-tmp" / "r5" / "product-e2e" / f"{stamp}-{os.getpid()}"
    )
    flow = ProductFlow(
        work,
        build_web=args.build_web,
        browser_channel=args.browser_channel,
    )
    print("=" * 70)
    print(f" TraceGuard product E2E @ {stamp}")
    print(f" python={PY}")
    print(f" work={_rel(work)}")
    print("=" * 70, flush=True)

    result = flow.run()
    report = _write_report(result, work)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result.summary(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print("\n" + "=" * 70)
    for step in result.steps:
        print(f"  {step.status:4} {step.name:26} {step.detail[:80]}")
    print("=" * 70)
    print(
        f" total={len(result.steps)} passed={result.passed} "
        f"failed={len(result.failed)} skipped={len(result.skipped)}"
    )
    print(f" report: {_rel(report)}")
    if result.failed:
        print(" RESULT: FAIL (" + ", ".join(s.name for s in result.failed) + ")")
        return 1
    if result.skipped:
        print(" RESULT: INCOMPLETE (" + ", ".join(s.name for s in result.skipped) + ")")
        return 3
    print(" RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
