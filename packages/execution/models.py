"""Stage-2 test assets: TestCase / TestCaseVersion / Oracle models.

Design constraints from the v1.1 roadmap and contract map:
  * C02: a formal TestCaseVersion is APPEND-ONLY. It must never be mutated in
    place. Every Run binds to a specific version.
  * C03: every formal Assertion declares a Primary Oracle, optional
    Supporting Oracles, expected behaviour, adjudication rule, version and
    source. AI may propose candidates but cannot approve a formal Oracle.
  * Stage 2 task 1: TestIntent, TestCase, TestCaseVersion, TestStep,
    TestDataRef, AssertionSpec, Precondition, Postcondition, CleanupPolicy.

The repository deliberately exposes no ``update``/``delete`` for versions —
only ``add_version``. This makes the append-only rule structural, not a
convention that can be forgotten.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from packages.execution.verdict import OracleVerdict


@dataclass(frozen=True)
class TestIntent:
    """Why this test exists — the sentence a human approves before coding."""

    intent_id: str
    description: str
    source_ref: str = ""


@dataclass(frozen=True)
class TestDataRef:
    """A reference to seeded data, never the Ground Truth itself."""

    ref_id: str
    locator: str  # e.g. "teamflow.project.name"
    value: str


@dataclass(frozen=True)
class Precondition:
    description: str
    setup_action: str = ""


@dataclass(frozen=True)
class Postcondition:
    description: str
    cleanup_action: str = ""


@dataclass(frozen=True)
class CleanupPolicy:
    """How to restore state after a Run (stage 1 gives us data/reset)."""

    strategy: str = "eval_reset"  # eval_reset | none
    reset_endpoint: str = "/__eval__/data/reset"


@dataclass(frozen=True)
class OracleSpec:
    """One Oracle source bound to an assertion (C03).

    ``expected_value`` is the authoritative expected value read from
    ``source_ref``. When present, the evaluator compares the observed value
    against it. When absent, ``expected`` acts as a direct verdict (used by
    manual/human-approved Oracles that cannot be machine-read).
    """

    oracle_id: str
    source_type: str  # independent_api | ui | network | manual
    source_ref: str  # e.g. "state.project_count" or "ui.success_visible"
    expected: OracleVerdict = OracleVerdict.PASSED
    version: str = "1.0.0"
    source: str = ""  # requirement / approval reference
    approved_by: str = "human"
    approved: bool = True
    expected_value: Any = None


@dataclass(frozen=True)
class OracleBinding:
    """Primary + Supporting Oracles for one assertion.

    P03 (priority / trust levels) is pending. We store the sources, but do NOT
    encode any "Primary wins" policy — adjudication lives in verdict.py and
    stays INCONCLUSIVE on disagreement.
    """

    assertion_id: str
    primary: OracleSpec
    supporting: tuple[OracleSpec, ...] = ()
    adjudication_rule: str = "frozen_fallback_inconclusive_on_conflict"


@dataclass(frozen=True)
class AssertionSpec:
    assertion_id: str
    description: str
    required: bool = True
    source_ref: str = ""
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestStep:
    """One deterministic action in a TestCaseVersion."""

    step_id: str
    action: str  # navigate | fill | click | assert | wait | api_call
    target: str  # selector or endpoint
    value: str = ""
    expected: str = ""


@dataclass(frozen=True)
class TestCaseVersion:
    """An immutable, append-only version of a TestCase (C02)."""

    case_id: str
    version: int
    title: str
    intent: TestIntent
    steps: tuple[TestStep, ...]
    assertions: tuple[AssertionSpec, ...]
    oracle_bindings: tuple[OracleBinding, ...]
    preconditions: tuple[Precondition, ...] = ()
    postconditions: tuple[Postcondition, ...] = ()
    data_refs: tuple[TestDataRef, ...] = ()
    cleanup: CleanupPolicy = field(default_factory=CleanupPolicy)
    compiled_from: str = "manual"
    approved_by: str = "human"

    def validate(self) -> None:
        """Structural checks. An empty required set is illegal (frozen rule)."""
        if not self.assertions:
            raise ValueError(f"{self.case_id} v{self.version}: no assertions")
        if not any(a.required for a in self.assertions):
            raise ValueError(f"{self.case_id} v{self.version}: no required assertion")
        assertion_ids = {a.assertion_id for a in self.assertions}
        bound_ids = {b.assertion_id for b in self.oracle_bindings}
        missing = assertion_ids - bound_ids
        if missing:
            raise ValueError(
                f"{self.case_id} v{self.version}: assertions without oracle binding: {missing}"
            )
        unapproved = [
            o.oracle_id
            for b in self.oracle_bindings
            for o in (b.primary, *b.supporting)
            if not o.approved
        ]
        if unapproved:
            raise ValueError(f"unapproved formal oracle(s): {unapproved}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TestCaseRepository:
    """Append-only store for TestCaseVersions.

    In-memory by default; ``save_dir`` optionally persists one JSON file per
    case (a list of versions). There is intentionally no update/delete API.
    """

    def __init__(self, save_dir: Path | None = None) -> None:
        self._versions: dict[str, list[TestCaseVersion]] = {}
        self.save_dir = save_dir
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    def add_version(self, version: TestCaseVersion) -> TestCaseVersion:
        version.validate()
        existing = self._versions.setdefault(version.case_id, [])
        if any(v.version == version.version for v in existing):
            raise ValueError(
                f"version {version.version} of {version.case_id} already exists; "
                "TestCaseVersion is append-only"
            )
        if existing and version.version <= existing[-1].version:
            raise ValueError(
                f"version {version.version} is not greater than latest "
                f"{existing[-1].version}; versions must increase monotonically"
            )
        existing.append(version)
        self._persist(version.case_id)
        return version

    def get(self, case_id: str, version: int | None = None) -> TestCaseVersion:
        versions = self._versions.get(case_id)
        if not versions:
            raise KeyError(f"unknown case: {case_id}")
        if version is None:
            return versions[-1]
        for v in versions:
            if v.version == version:
                return v
        raise KeyError(f"unknown version {version} of {case_id}")

    def versions(self, case_id: str) -> tuple[TestCaseVersion, ...]:
        return tuple(self._versions.get(case_id, ()))

    def all_cases(self) -> tuple[str, ...]:
        return tuple(sorted(self._versions))

    def _persist(self, case_id: str) -> None:
        if not self.save_dir:
            return
        payload = [v.to_dict() for v in self._versions[case_id]]
        (self.save_dir / f"{case_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _load(self) -> None:
        assert self.save_dir is not None
        for path in sorted(self.save_dir.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw:
                version = _version_from_dict(item)
                self._versions.setdefault(version.case_id, []).append(version)


def _oracle_verdict(value: Any) -> OracleVerdict:
    return OracleVerdict(value)


def _version_from_dict(item: dict[str, Any]) -> TestCaseVersion:
    """Rebuild a version from JSON (used only by repository load)."""
    data = dict(item)
    data["intent"] = TestIntent(**data["intent"])
    data["steps"] = tuple(TestStep(**s) for s in data["steps"])
    data["assertions"] = tuple(AssertionSpec(**a) for a in data["assertions"])
    data["preconditions"] = tuple(Precondition(**p) for p in data["preconditions"])
    data["postconditions"] = tuple(Postcondition(**p) for p in data["postconditions"])
    data["data_refs"] = tuple(TestDataRef(**d) for d in data["data_refs"])
    data["cleanup"] = CleanupPolicy(**data["cleanup"])
    bindings = []
    for b in data["oracle_bindings"]:
        b = dict(b)
        primary = dict(b["primary"])
        primary["expected"] = _oracle_verdict(primary["expected"])
        b["primary"] = OracleSpec(**primary)
        supporting = []
        for s in b["supporting"]:
            s = dict(s)
            s["expected"] = _oracle_verdict(s["expected"])
            supporting.append(OracleSpec(**s))
        b["supporting"] = tuple(supporting)
        bindings.append(OracleBinding(**b))
    data["oracle_bindings"] = tuple(bindings)
    return TestCaseVersion(**data)


def load_case(path: Path) -> TestCaseVersion:
    """Load one ``TestCaseVersion.to_dict()`` JSON file and validate it."""
    version = _version_from_dict(json.loads(path.read_text(encoding="utf-8")))
    version.validate()
    return version


def load_cases_dir(directory: Path) -> tuple[TestCaseVersion, ...]:
    """Load all ``*.json`` cases in a directory, sorted by file name.

    Config files such as ``locators.json`` live beside the cases but are not
    cases; they are skipped by checking for the ``case_id`` discriminator.
    """
    cases: list[TestCaseVersion] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(raw, dict) or "case_id" not in raw or "oracle_bindings" not in raw:
            continue
        cases.append(load_case(path))
    return tuple(cases)


def next_version(previous: TestCaseVersion, **changes: Any) -> TestCaseVersion:
    """Create v+1 from an existing version without mutating the original."""
    return replace(previous, version=previous.version + 1, **changes)
