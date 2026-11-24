"""Second-round full-chain integration.

Chain exercised here (every hop is real except the model, which is scripted and
labelled as such):

    intent → Explorer (real Chrome) → ExplorationTrace → Compiler → Candidate
    → HUMAN REVIEW (Oracle approval + promotion) → formal TestCaseVersion
    → LocalRunner (real Chrome against real TeamFlow) → Verdict
    → Event/Evidence → Evidence Graph → Diagnosis → Disposition

This also covers the *successful* promotion path, which the first round only
tested negatively (unapproved Oracle must be rejected).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from packages.agents.compiler import CandidateTestCase, TestCaseCompiler
from packages.agents.exploration import Budget
from packages.agents.explorer import Explorer
from packages.agents.llm import ModelConfig, ScriptedProvider, TracedClient
from packages.agents.llm_policy import LLMPolicy
from packages.evidence.graph import make_in_memory_graph
from packages.execution.models import TestCaseVersion, TestIntent
from packages.execution.pipeline import FullChain
from tests.netutil import release_reserved, reserve_port, take_reserved

TestCaseVersion.__test__ = False  # type: ignore[attr-defined] - pytest tries to collect it
TestIntent.__test__ = False

TF_PORT = reserve_port()
TF_BASE = f"http://127.0.0.1:{TF_PORT}"
EVAL = {"X-Eval-Token": "local-dev-eval-token-change-me"}
CASES = Path(__file__).resolve().parents[1] / "benchmark" / "cases"

# The business goal the exploration is driven by (R5-4.6): the compiler now
# refuses to compile a trace without a declared, verifiable target.
INTENT = {
    "intent_id": "INT-CHAIN-001",
    "description": "log in as tester and reach the project list",
    "source_ref": "INT-CHAIN-001",
    "goal": "log in as tester and reach the project list",
    "expected_outcomes": [],
    "target": "/projects",
    "required_assertions": [],
    "constraints": [],
}


@pytest.fixture(scope="module")
def teamflow() -> Iterator[None]:
    config = uvicorn.Config(
        "apps.teamflow.main:app", host="127.0.0.1", port=TF_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": take_reserved(TF_PORT)}, daemon=True
    )
    thread.start()
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{TF_BASE}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:  # pragma: no cover
            pytest.fail("TeamFlow did not become healthy")
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "server thread did not stop"
        release_reserved(TF_PORT)


def _decision(action: str, rationale: str, **args: str) -> str:
    return json.dumps({"action": action, "args": args, "rationale": rationale})


def _explore() -> Any:
    script = [
        _decision("navigate", "open login", url=f"{TF_BASE}/"),
        _decision("fill", "username", selector="[data-testid=username]", value="tester"),
        _decision("fill", "password", selector="[data-testid=password]", value="test-pass-123"),
        _decision("click", "submit", selector="[data-testid=login-submit]"),
        _decision("finish", "done"),
    ]
    provider = ScriptedProvider(script)
    client = TracedClient(
        provider=provider,
        config=ModelConfig(provider="test", model="scripted-not-a-model", max_retries=1),
    )
    policy = LLMPolicy(
        client=client,
        goal="log in as tester",
        allowed_tools=frozenset({"navigate", "fill", "click", "assert_text", "finish"}),
    )
    explorer = Explorer(policy=policy, budget=Budget(max_steps=12, max_tool_calls=24))
    # R5-4.6: the exploration is goal-driven — the declared target is what the
    # compiler later verifies against observed evidence.
    return explorer.explore("INT-CHAIN-001", TF_BASE, intent=INTENT)


def _approve_oracles(candidate: CandidateTestCase) -> CandidateTestCase:
    """Simulates the human Oracle review step (the only path to a valid formal case)."""
    bindings = tuple(
        replace(
            binding,
            primary=replace(binding.primary, approved=True, approved_by="human:alice"),
            supporting=tuple(
                replace(s, approved=True, approved_by="human:alice") for s in binding.supporting
            ),
        )
        for binding in candidate.oracle_bindings
    )
    return replace(candidate, oracle_bindings=bindings)


def test_candidate_is_not_formal_until_a_human_reviews_it(teamflow: None) -> None:
    trace = _explore()
    candidate = TestCaseCompiler().compile(trace, case_id="TC-INT-001", title="login flow")

    # The gate is at promotion, not merely at validate(): promote() builds the
    # formal version and refuses it because the Oracle is still unapproved.
    with pytest.raises(ValueError, match="unapproved"):
        TestCaseCompiler().promote(candidate, reviewer="alice", approval_record="CR-1")

    # after the human approves the Oracle, the same case validates
    approved = _approve_oracles(candidate)
    formal = TestCaseCompiler().promote(approved, reviewer="alice", approval_record="CR-1")
    formal.validate()
    assert isinstance(formal, TestCaseVersion)
    assert formal.approved_by.startswith("alice")
    assert formal.compiled_from.startswith("compiler:")


def test_chain_from_exploration_to_disposition(teamflow: None) -> None:
    with httpx.Client(base_url=TF_BASE, timeout=10, trust_env=False) as c:
        c.post("/__eval__/faults/clear", headers=EVAL)
        c.post("/__eval__/data/reset", headers=EVAL)

    trace = _explore()
    candidate = TestCaseCompiler().compile(trace, case_id="TC-INT-002", title="login flow")
    formal = TestCaseCompiler().promote(
        _approve_oracles(candidate), reviewer="alice", approval_record="CR-2"
    )

    graph, _ = make_in_memory_graph()
    result = FullChain(base_url=TF_BASE, graph=graph).run(
        "INT-CHAIN-002", formal, fault_events=[], evidence_gap="ui"
    )

    # 1. execution really ran and produced a verdict
    assert result.completed
    exec_info = result.execution
    assert exec_info.get("error") is None, exec_info.get("error")
    assert exec_info["test_verdict"] in {"PASSED", "FAILED", "INCONCLUSIVE", "NOT_EVALUATED"}
    assert exec_info["run_id"]

    # 2. traceability: candidate -> trace -> execution all share identifiers
    assert candidate.source_exploration_id == trace.exploration_id
    assert formal.case_id == candidate.case_id

    # 3. diagnosis references the same run and never invents CONFIRMED without a rule
    diagnosis = result.diagnosis
    assert diagnosis["run_id"] == exec_info["run_id"]
    # Two reachable shapes of CONFIRMED, and they must stay distinguishable:
    #   * a failing run   -> CONFIRMED root cause, backed by a confirmation rule
    #   * a healthy run    -> CONFIRMED "no failure observed", which by definition
    #     has NO root cause (the compiler now produces an evaluable oracle, so
    #     this branch is reachable here; it used to be masked by NOT_EVALUATED)
    if diagnosis["diagnosis_status"] == "CONFIRMED":
        assert any(h.get("confirmation_rule") for h in diagnosis["hypotheses"])
        if exec_info["test_verdict"] == "PASSED":
            assert not diagnosis["root_causes"]
            assert diagnosis["symptom"] == "none"
            assert any("healthy" in n.lower() for n in diagnosis["notes"])
        else:
            assert diagnosis["root_causes"]

    # 4. disposition is always routed, never left blank
    assert result.disposition

    # 5. the graph contains nodes for this run
    nodes = graph.nodes() if hasattr(graph, "nodes") else []
    assert nodes, "evidence graph must contain nodes after a real run"

    # 6. every stage recorded a status
    assert all(stage.status for stage in result.stages)


def test_chain_records_a_failed_stage_instead_of_passing(teamflow: None) -> None:
    """A case whose assertion cannot hold must not be reported as PASSED."""
    bad = TestCaseCompiler().promote(
        _approve_oracles(
            TestCaseCompiler().compile(
                _explore(), case_id="TC-INT-003", title="intentionally failing login"
            )
        ),
        reviewer="alice",
        approval_record="CR-3",
    )
    # Force an impossible expectation on the primary Oracle of the first assertion.
    first = bad.oracle_bindings[0]
    broken_primary = replace(first.primary, expected_value="__never_matches__")
    bad = replace(
        bad,
        oracle_bindings=(
            replace(first, primary=broken_primary),
            *bad.oracle_bindings[1:],
        ),
    )

    graph, _ = make_in_memory_graph()
    result = FullChain(base_url=TF_BASE, graph=graph).run("INT-CHAIN-003", bad, fault_events=[])
    assert result.completed
    assert result.execution["test_verdict"] != "PASSED"
