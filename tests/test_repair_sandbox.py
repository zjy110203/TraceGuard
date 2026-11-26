"""RepairSandbox end-to-end tests: real Chrome, isolated TeamFlow subprocess.

Each test starts a throwaway TeamFlow (own SQLite, own port, own PID), replays a
drifted baseline and a candidate with real Chrome on the same data seed, and
verifies the sandbox verdicts — plus the security properties: the target
subprocess is stopped and the sandbox directory is deleted, while directories
belonging to other sandboxes are left untouched.

R5-4.10: the sandbox root is injected per test (``tmp_path``). The previous
shared root ``.traceguard-tmp/r3`` accumulated one directory per sandbox
forever (180 were found), so a host that guards bulk deletes eventually refused
the teardown and made these very tests fail for a reason unrelated to the code
under test. Isolating the root removes the accumulation instead of relaxing the
assertion.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.execution.models import TestCaseVersion, load_case
from packages.execution.sandbox import (
    MARKER_NAME,
    RepairSandbox,
    SandboxError,
)

CASES = Path(__file__).resolve().parents[1] / "benchmark" / "cases"
DRIFTED_TARGET = "[data-testid=legacy-new-project-link]"  # removed in a UI refactor
WRONG_TARGET = "[data-testid=also-wrong-new-project-link]"


def _original() -> TestCaseVersion:
    return load_case(CASES / "TC-CREATE-001.json")


def _with_version(case: TestCaseVersion, version: int) -> TestCaseVersion:
    return dataclasses.replace(case, version=version)


def _with_step_target(case: TestCaseVersion, step_id: str, target: str) -> TestCaseVersion:
    steps = tuple(
        dataclasses.replace(s, target=target) if s.step_id == step_id else s for s in case.steps
    )
    return dataclasses.replace(case, steps=steps)


def _bump_oracle_versions(case: TestCaseVersion) -> TestCaseVersion:
    """A v2 candidate keeps identical Oracle semantics (only the version label moves)."""
    bindings = tuple(
        dataclasses.replace(
            b,
            primary=dataclasses.replace(b.primary, version="2.0.0"),
            supporting=tuple(dataclasses.replace(x, version="2.0.0") for x in b.supporting),
        )
        for b in case.oracle_bindings
    )
    return dataclasses.replace(case, oracle_bindings=bindings)


@pytest.fixture()
def sandbox_root(tmp_path: Path) -> Path:
    """A private sandbox root per test: nothing accumulates across runs."""
    root = tmp_path / "sandboxes"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def sandbox_pair() -> tuple[TestCaseVersion, TestCaseVersion]:
    """Drifted base (a locator removed by a hypothetical UI refactor) + fixed candidate."""
    base = _with_step_target(_original(), "s6", DRIFTED_TARGET)
    candidate = _bump_oracle_versions(_with_version(_original(), 2))
    return base, candidate


def _sibling_of_other_sandbox(root: Path) -> Path:
    """A directory that must survive our cleanup: it belongs to another sandbox."""
    other = root / "sb_foreign_0001"
    other.mkdir(parents=True, exist_ok=True)
    (other / MARKER_NAME).write_text(
        json.dumps({"sandbox_id": "sb_foreign_0001", "project": "traceguard"}),
        encoding="utf-8",
    )
    (other / "keep_me.txt").write_text("do not delete", encoding="utf-8")
    return other


def test_locator_drift_is_repaired_in_the_sandbox(sandbox_pair: Any, sandbox_root: Path) -> None:
    base, candidate = sandbox_pair
    captured: list[dict[str, Any]] = []

    sandbox = RepairSandbox(
        proposal_id="rp_r3_001",
        base_case=base,
        candidate=candidate,
        control_case=load_case(CASES / "TC-CREATE-002.json"),
        on_evidence=captured.append,
        root=sandbox_root,
    )
    result = sandbox.run()

    assert result.verdict == "VALIDATED", result.reason
    assert result.baseline is not None and result.baseline.test_verdict == "FAILED"
    assert result.candidate is not None and result.candidate.test_verdict == "PASSED"
    assert result.control_before is not None and result.control_before.test_verdict == "PASSED"
    assert result.control_after is not None and result.control_after.test_verdict == "PASSED"
    # Evidence must survive cleanup: returned AND handed to the platform.
    assert result.evidence["verdict"] == "VALIDATED"
    assert result.evidence["target_url"].startswith("http://127.0.0.1:")
    assert len(result.evidence_sha256) == 64
    assert captured and captured[0] == result.evidence
    assert result.target_pid is not None


def test_sandbox_cleanup_is_scoped_to_its_own_directory(
    sandbox_pair: Any, sandbox_root: Path
) -> None:
    base, candidate = sandbox_pair
    sibling = _sibling_of_other_sandbox(sandbox_root)

    sandbox = RepairSandbox(
        proposal_id="rp_r3_002",
        base_case=base,
        candidate=candidate,
        on_evidence=lambda payload: None,
        root=sandbox_root,
    )
    result = sandbox.run()
    sandbox_dir = sandbox_root / result.sandbox_id

    assert not sandbox_dir.exists(), (
        f"our own sandbox directory must be deleted: {result.cleanup_error}"
    )
    assert sibling.exists(), "another sandbox's directory must never be touched"
    assert (sibling / "keep_me.txt").read_text(encoding="utf-8") == "do not delete"
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{result.evidence['target_url']}/health", timeout=2.0, trust_env=False)


def test_passing_baseline_is_not_repaired(sandbox_pair: Any, sandbox_root: Path) -> None:
    """A repair needs a drifted asset; validating against a PASS is meaningless."""
    base = _original()
    candidate = _bump_oracle_versions(_with_version(_original(), 2))

    result = RepairSandbox(
        proposal_id="rp_r3_003",
        base_case=base,
        candidate=candidate,
        on_evidence=lambda payload: None,
        root=sandbox_root,
    ).run()

    assert result.verdict == "NO_BEHAVIORAL_CHANGE"
    assert result.baseline is not None and result.baseline.test_verdict == "PASSED"


def test_candidate_that_does_not_fix_is_not_validated(
    sandbox_pair: Any, sandbox_root: Path
) -> None:
    base, _ = sandbox_pair
    candidate = _with_step_target(_with_version(base, 2), "s6", WRONG_TARGET)

    result = RepairSandbox(
        proposal_id="rp_r3_004",
        base_case=base,
        candidate=candidate,
        on_evidence=lambda payload: None,
        root=sandbox_root,
    ).run()

    assert result.verdict == "NOT_VALIDATED"
    assert result.baseline is not None and result.baseline.test_verdict == "FAILED"
    assert result.candidate is not None and result.candidate.test_verdict == "FAILED"


def test_target_startup_failure_is_reported_not_promoted(
    sandbox_pair: Any, sandbox_root: Path
) -> None:
    """A target that dies before becoming healthy yields SANDBOX_FAILED, not VALIDATED."""
    base, candidate = sandbox_pair
    captured: list[dict[str, Any]] = []

    sandbox = RepairSandbox(
        proposal_id="rp_r3_005",
        base_case=base,
        candidate=candidate,
        on_evidence=captured.append,
        root=sandbox_root,
    )

    def dying_start(self: RepairSandbox) -> str:
        self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", "raise SystemExit(3)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._target_pid = self._process.pid
        raise SandboxError("sandbox TeamFlow exited before becoming healthy")

    sandbox._start_target = dying_start.__get__(sandbox)  # type: ignore[method-assign]
    result = sandbox.run()

    assert result.verdict == "SANDBOX_FAILED"
    assert result.candidate is None
    assert result.evidence["verdict"] == "SANDBOX_FAILED"
    assert captured and captured[0]["verdict"] == "SANDBOX_FAILED"

    # Two different facts share this assertion, and conflating them is what made
    # this test red for the wrong reason. Either the sandbox really removed its
    # directory (assert strictly), or the HOST refused the delete — in which
    # case the product must have *recorded* the refusal ("cleanup blocked: ...")
    # instead of claiming success. The second branch is skipped, never passed:
    # a refused delete verifies nothing about our own cleanup logic.
    if result.cleanup_error.startswith("cleanup blocked:"):
        assert "blocked" in result.cleanup_error
        pytest.skip(
            "host delete guard refused the teardown "
            f"({result.cleanup_error}); the product recorded it honestly, "
            "but the filesystem outcome is environment-defined"
        )
    assert not (sandbox_root / result.sandbox_id).exists()
