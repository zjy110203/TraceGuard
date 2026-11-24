"""C04 additive-field compatibility contract (decision D-R5-411-1).

R5-4.10 added ``RunReport.cleanup`` so resource-teardown failures are not
reported as business verdicts. C04's schema uses ``additionalProperties: false``
and registers ``cleanup`` explicitly, which keeps existing payloads valid but
means a *stale* strict validator (one holding the pre-R5-4.10 schema) would
reject a new payload.

These tests pin the exact compatibility envelope instead of asserting "it
works": the new payload validates, the legacy payload still validates, and the
one thing that cannot be fixed without a version bump (a cached old schema) is
proven and documented rather than hoped away.
"""

from __future__ import annotations

import copy
from typing import Any

import jsonschema
import pytest

from packages.contracts.loader import ContractRegistry
from packages.execution.runner import ExecutionStatus, RunReport, StepReport

C04 = "C04"


def _payload() -> dict[str, Any]:
    return RunReport(
        run_id="run_compat_0001",
        case_id="TC-COMPAT-001",
        case_version=1,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        execution_status=ExecutionStatus.COMPLETED,
        steps=[StepReport(step_id="s1", action="goto", ok=True, detail="/login")],
        cleanup={"is_clean": True, "browser_closed": True},
    ).to_dict()


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load()


def test_current_payload_with_cleanup_validates(registry: ContractRegistry) -> None:
    outcome = registry.validate(C04, _payload())

    assert outcome.ok, (outcome.structural_errors, [i.to_dict() for i in outcome.semantic_issues])


def test_legacy_payload_without_cleanup_still_validates(registry: ContractRegistry) -> None:
    """Older producers emitted no ``cleanup`` key; they must keep working."""
    legacy = copy.deepcopy(_payload())
    legacy.pop("cleanup")

    outcome = registry.validate(C04, legacy)

    assert outcome.ok, outcome.structural_errors


def test_cleanup_is_optional_and_never_required(registry: ContractRegistry) -> None:
    schema = registry.schema(C04)
    run_report = schema["$defs"]["RunReport"]

    assert "cleanup" in run_report["properties"]
    assert "cleanup" not in run_report["required"]
    assert registry.entry(C04)["version"] == "0.2.0-draft"
    assert registry.status(C04) == "draft", "a draft may not be treated as frozen"


def test_unknown_top_level_field_is_still_rejected(registry: ContractRegistry) -> None:
    """Proves the strictness is real — and therefore why the decision matters."""
    payload = _payload()
    payload["not_a_contract_field"] = 1

    outcome = registry.validate(C04, payload)

    assert not outcome.ok
    assert any("Additional properties" in err for err in outcome.structural_errors)


def test_stale_validator_rejects_the_new_payload(registry: ContractRegistry) -> None:
    """The documented residual risk: a cached 0.2.0 schema without ``cleanup``.

    This is not a defect in the current contract — it is the reason the change is
    registered as a decision and why producers must treat ``cleanup`` as a
    strictly additive, ignorable field.
    """
    stale = copy.deepcopy(registry.schema(C04))
    del stale["$defs"]["RunReport"]["properties"]["cleanup"]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(stale).validate(_payload())


def test_c05_event_contract_is_untouched(registry: ContractRegistry) -> None:
    """Cleanup status travels in C04 only; the event shape must not grow."""
    c05 = registry.schema("C05")

    def _walk(node: Any) -> list[str]:
        keys: list[str] = []
        if isinstance(node, dict):
            keys.extend(node.get("properties", {}).keys())
            for value in node.values():
                keys.extend(_walk(value))
        elif isinstance(node, list):
            for item in node:
                keys.extend(_walk(item))
        return keys

    assert "cleanup" not in _walk(c05)
    assert registry.entry("C05")["version"] == "0.2.0-draft"


def test_cleanup_does_not_carry_business_verdict(registry: ContractRegistry) -> None:
    """Cleanup and business verdict must never be merged into one axis."""
    payload = _payload()
    cleanup = payload["cleanup"]

    assert payload["test_verdict"] in ("", "PASSED", "FAILED", "BLOCKED", "INCONCLUSIVE")
    for forbidden in ("test_verdict", "verdict", "passed", "failed"):
        assert forbidden not in cleanup
