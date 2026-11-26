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
import logging
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from packages.execution.dataref import is_placeholder
from packages.execution.verdict import OracleVerdict

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TestIntent:
    """Why this test exists — the goal a human approves before coding.

    ``description`` is the legacy free-text field (still the authority when
    ``goal`` is empty). The R5-4 fields make the *business* target explicit so a
    compiler can tell "the click happened" from "the user is logged in":

      * ``goal``                — what the user wants to achieve, in business terms
      * ``expected_outcomes``   — observable post-conditions that define success
      * ``target``              — the relevant page/route the goal lives on
      * ``required_assertions`` — assertions review demands; ``state.<key>=<json>``
        is the documented form for an independent business-state Oracle
      * ``constraints``         — optional limits (data, environment, timing)

    All new fields default to empty, so every previously persisted case and
    trace keeps loading unchanged.
    """

    intent_id: str
    description: str
    source_ref: str = ""
    goal: str = ""
    expected_outcomes: tuple[str, ...] = ()
    target: str = ""
    required_assertions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    @property
    def goal_text(self) -> str:
        """Goal for humans/agents: the explicit goal, else the legacy text."""
        return self.goal or self.description


@dataclass(frozen=True)
class TestDataRef:
    """A reference to seeded data, never the Ground Truth itself.

    ``value`` may be empty: an empty value means the value lives in a
    deployment-managed test-data provider (env/settings), never in the case
    file. A non-empty value is only legitimate for non-secret fixtures.
    """

    ref_id: str
    locator: str  # e.g. "teamflow.project.name"
    value: str = ""


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
    """One deterministic action in a TestCaseVersion.

    ``value_ref`` binds a sensitive input to a ``TestDataRef`` instead of
    carrying the plaintext. A step must never hold both: a plaintext value in a
    formal case is exactly what R5-4.1 removes. ``value`` stays for
    non-sensitive text (expected strings, wait durations, literals).
    """

    step_id: str
    action: str  # navigate | fill | click | assert_visible | assert_text | wait | api_call
    target: str  # selector or endpoint
    value: str = ""
    expected: str = ""
    value_ref: str = ""


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

        # R5-4.1 data-binding invariants: a secret is referenced, never inlined,
        # and a redaction placeholder is never a legitimate input value.
        declared_refs = {ref.ref_id for ref in self.data_refs}
        for step in self.steps:
            if step.value_ref:
                if step.value:
                    raise ValueError(
                        f"{self.case_id} v{self.version}: step {step.step_id} carries both "
                        f"value_ref {step.value_ref!r} and a plaintext value"
                    )
                if step.value_ref not in declared_refs:
                    raise ValueError(
                        f"{self.case_id} v{self.version}: step {step.step_id} references "
                        f"undeclared data ref {step.value_ref!r}"
                    )
            if is_placeholder(step.value):
                raise ValueError(
                    f"{self.case_id} v{self.version}: step {step.step_id} uses the redaction "
                    "placeholder as an input value"
                )

        # R5-4.7: a SENSITIVE TestDataRef must have an EMPTY value — the
        # plaintext lives in the deployment's TestDataProvider, never in the
        # case file. A credential-shaped ref with an inlined value is a defect
        # even when no step references it.
        from packages.execution.dataref import SENSITIVE_HINT

        for ref in self.data_refs:
            if ref.value and SENSITIVE_HINT.search(ref.ref_id):
                raise ValueError(
                    f"{self.case_id} v{self.version}: sensitive data ref {ref.ref_id!r} "
                    "carries an inline value; it must be resolved by the deployment-level "
                    "TestDataProvider at run time"
                )

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


def case_from_dict(item: dict[str, Any]) -> TestCaseVersion:
    """Strictly parse and validate one TestCaseVersion payload."""
    version = _version_from_dict(dict(item))
    version.validate()
    return version


def load_case(path: Path) -> TestCaseVersion:
    """Load one ``TestCaseVersion.to_dict()`` JSON file and validate it."""
    return case_from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_cases_dir_report(
    directory: Path,
) -> tuple[tuple[TestCaseVersion, ...], list[dict[str, str]]]:
    """Load every case in a directory, reporting the ones that fail to load.

    One broken file must not take the whole platform down: before this split a
    single invalid case made ``/overview``, ``/cases`` and ``/intents`` return
    500, which is how a stale compiled case (redaction placeholder as a fill
    value) hid every other case. Invalid files are skipped *and* returned so
    the UI can name them instead of silently pretending they do not exist.
    """
    cases: list[TestCaseVersion] = []
    invalid: list[dict[str, str]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            invalid.append({"file": path.name, "reason": f"unreadable: {exc}"})
            continue
        if not isinstance(raw, dict) or "case_id" not in raw or "oracle_bindings" not in raw:
            continue
        try:
            cases.append(load_case(path))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            invalid.append({"file": path.name, "reason": str(exc)})
    if invalid:
        LOGGER.warning(
            "%d case file(s) under %s could not be loaded: %s",
            len(invalid),
            directory,
            ", ".join(item["file"] for item in invalid),
        )
    latest: dict[str, TestCaseVersion] = {}
    for case in cases:
        current = latest.get(case.case_id)
        if current is None or case.version > current.version:
            latest[case.case_id] = case
    return tuple(sorted(latest.values(), key=lambda case: case.case_id)), invalid


def latest_case_path(directory: Path, case_id: str) -> Path:
    """Find the latest append-only case file for one case id."""
    found: list[tuple[int, Path]] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("case_id") == case_id:
                found.append((int(payload.get("version", 0)), path))
        except (ValueError, OSError, AttributeError, TypeError):
            continue
    if not found:
        raise FileNotFoundError(f"case not found: {case_id}")
    return max(found, key=lambda item: item[0])[1]


def load_cases_dir(directory: Path) -> tuple[TestCaseVersion, ...]:
    """Load all ``*.json`` cases in a directory, sorted by file name.

    Config files such as ``locators.json`` live beside the cases but are not
    cases; they are skipped by checking for the ``case_id`` discriminator.
    Files that fail validation are skipped here — use
    :func:`load_cases_dir_report` when the caller wants to surface them.
    """
    return load_cases_dir_report(directory)[0]


def next_version(previous: TestCaseVersion, **changes: Any) -> TestCaseVersion:
    """Create v+1 from an existing version without mutating the original."""
    return replace(previous, version=previous.version + 1, **changes)
