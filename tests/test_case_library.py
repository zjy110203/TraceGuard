"""Stage-2 acceptance: 10-20 independently runnable TeamFlow cases.

Every case in ``benchmark/cases/`` is a formal TestCaseVersion loaded through
the public loader (not hand-built in the test). Each is executed against a real
TeamFlow server with real system Chrome via the deterministic runner; no Agent
decides the verdict. This is the "10~20 条可独立运行的测试用例" exit criterion.

Also covers the two stage-2 additions:
  * logical ``loc:`` targets resolved through LocatorRegistry priority policy;
  * Playwright Trace captured as SHA-256-integrity Evidence.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
import uvicorn

from packages.execution.locators import LocatorRegistry
from packages.execution.models import TestCaseVersion, load_cases_dir
from packages.execution.runner import LocalRunner
from tests.netutil import release_reserved, reserve_port, take_reserved

for _model in (TestCaseVersion,):
    _model.__test__ = False

TF_PORT = reserve_port()
BASE = f"http://127.0.0.1:{TF_PORT}"
EVAL_H = {"X-Eval-Token": "local-dev-eval-token-change-me"}
CASES_DIR = Path("benchmark/cases")
LOCATORS_FILE = CASES_DIR / "locators.json"


@pytest.fixture(scope="module")
def cases() -> tuple[TestCaseVersion, ...]:
    loaded = load_cases_dir(CASES_DIR)
    assert len(loaded) >= 10, "stage 2 requires 10-20 independently runnable cases"
    assert len(loaded) <= 20, "stage 2 scopes to 10-20 cases"
    return loaded


@pytest.fixture(scope="module")
def locators() -> LocatorRegistry:
    return LocatorRegistry.from_file(LOCATORS_FILE)


@pytest.fixture(scope="module")
def teamflow_server() -> Generator[None, None, None]:
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=TF_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": take_reserved(TF_PORT)}, daemon=True
    )
    thread.start()
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{BASE}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("TeamFlow server did not become healthy")
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "server thread did not stop"
        release_reserved(TF_PORT)


def _reset(client: httpx.Client) -> None:
    client.post("/__eval__/faults/clear", headers=EVAL_H)
    client.post("/__eval__/data/reset", headers=EVAL_H)


def test_library_is_well_formed(cases: tuple[TestCaseVersion, ...]) -> None:
    """Structural contract: unique ids, monotonic v1, every assertion bound."""
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for case in cases:
        case.validate()
        assert case.version == 1
        bound = {b.assertion_id for b in case.oracle_bindings}
        assert {a.assertion_id for a in case.assertions} == bound
        assert case.compiled_from == "manual" and case.approved_by == "human"


@pytest.mark.parametrize("index", range(len(load_cases_dir(CASES_DIR))))
def test_each_case_runs_independently(
    index: int,
    cases: tuple[TestCaseVersion, ...],
    teamflow_server: None,
    locators: LocatorRegistry,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    case = cases[index]
    with httpx.Client(base_url=BASE, timeout=10, trust_env=False) as client:
        _reset(client)

    trace_dir = tmp_path_factory.mktemp(f"traces_{index}")
    runner = LocalRunner(BASE, locators=locators, trace_dir=trace_dir)
    report = runner.run(case)

    # A healthy TeamFlow must yield a clean PASS for a human-approved case.
    assert report.execution_status.value == "COMPLETED", report.verdict_notes
    assert report.test_verdict == "PASSED", (
        f"{case.case_id}: {report.verdict_notes}; steps="
        f"{[(s.step_id, s.ok, s.detail) for s in report.steps]}"
    )
    assert all(s.ok for s in report.steps), [
        (s.step_id, s.detail) for s in report.steps if not s.ok
    ]
    assert report.events and report.events[0]["event_type"] == "run.started"


def test_trace_evidence_has_integrity(
    cases: tuple[TestCaseVersion, ...],
    teamflow_server: None,
    locators: LocatorRegistry,
    tmp_path: Path,
) -> None:
    """Playwright Trace is captured as evidence and recorded with SHA-256."""
    with httpx.Client(base_url=BASE, timeout=10, trust_env=False) as client:
        _reset(client)
    runner = LocalRunner(BASE, locators=locators, trace_dir=tmp_path)
    report = runner.run(cases[0])
    traces = [e for e in report.evidence if e["evidence_type"] == "playwright_trace"]
    assert len(traces) == 1
    assert traces[0]["integrity_check"].startswith("sha256:")
    assert Path(traces[0]["payload"]["path"]).exists()
