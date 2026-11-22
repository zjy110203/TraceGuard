"""Contract tests for C01~C12.

Three things are proven here, and none of them can be satisfied by hand-waving:
1. every schema is a legal Draft 2020-12 schema;
2. every declared valid sample passes structure AND semantics;
3. every declared invalid/semantic sample actually fails, for the right reason.

Plus two drift guards: the on-disk schemas must equal what the live dataclasses
generate right now, and the human-readable rule index must not silently diverge
from the codes the implementation can actually raise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from packages.contracts import semantic as semantic_module
from packages.contracts.loader import SAMPLE_DIR, SCHEMA_DIR, ContractRegistry
from scripts.build_contracts import build_all_schemas

REGISTRY = ContractRegistry.load()
CONTRACT_IDS = [f"C{i:02d}" for i in range(1, 13)]


def _samples(cid: str, kind: str) -> list[Path]:
    return sorted((SAMPLE_DIR / cid / kind).glob("*.json"))


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


ALL_VALID = [(cid, p) for cid in CONTRACT_IDS for p in _samples(cid, "valid")]
ALL_INVALID = [(cid, p) for cid in CONTRACT_IDS for p in _samples(cid, "invalid")]
ALL_SEMANTIC = [(cid, p) for cid in CONTRACT_IDS for p in _samples(cid, "semantic")]


# ------------------------------------------------------------------ inventory


def test_registry_covers_all_twelve_contracts() -> None:
    assert REGISTRY.contract_ids() == tuple(CONTRACT_IDS)
    assert REGISTRY.missing_schemas() == ()


def test_every_contract_is_draft_and_none_claims_frozen() -> None:
    for cid in CONTRACT_IDS:
        entry = REGISTRY.entry(cid)
        assert entry["status"] == "draft", f"{cid} must stay draft until human approval"
        assert entry["version"].endswith("-draft"), f"{cid} version must be suffixed -draft"
        assert entry["schema_file"], f"{cid} must declare a schema file"


def test_no_sample_directory_is_empty() -> None:
    for cid in CONTRACT_IDS:
        assert _samples(cid, "valid"), f"{cid} has no valid sample"
        assert _samples(cid, "invalid"), f"{cid} has no invalid sample"
        assert _samples(cid, "semantic"), f"{cid} has no semantic counter-example"


def test_registry_file_lists_exist_on_disk() -> None:
    for cid in CONTRACT_IDS:
        entry = REGISTRY.entry(cid)
        for name in entry["valid_samples"]:
            assert (SAMPLE_DIR / cid / "valid" / name).exists()
        for name in entry["invalid_samples"]:
            assert (SAMPLE_DIR / cid / "invalid" / name).exists()
        for name in entry["semantic_samples"]:
            assert (SAMPLE_DIR / cid / "semantic" / name).exists()


# ------------------------------------------------------------------- schemas


@pytest.mark.parametrize("cid", CONTRACT_IDS)
def test_schema_is_valid_json_schema(cid: str) -> None:
    schema = REGISTRY.schema(cid)
    Draft202012Validator.check_schema(schema)
    assert schema.get("status") == "draft"
    assert "$defs" in schema or "properties" in schema or "oneOf" in schema


def test_on_disk_schemas_match_current_code() -> None:
    """Drift guard: if a dataclass changes, the checked-in schema must be rebuilt."""
    fresh = build_all_schemas()
    on_disk = {p.name.split("_")[0]: p for p in SCHEMA_DIR.glob("C*_*.schema.json")}
    assert set(on_disk) == set(fresh), "schema files do not cover exactly C01..C12"
    for cid, schema in fresh.items():
        stored = json.loads(on_disk[cid].read_text(encoding="utf-8"))
        assert stored == schema, (
            f"{cid} schema is stale relative to the dataclasses; "
            f"run `python -m scripts.build_contracts`"
        )


# ------------------------------------------------------------------- samples


@pytest.mark.parametrize(("cid", "path"), ALL_VALID, ids=[f"{c}-{p.name}" for c, p in ALL_VALID])
def test_valid_sample_passes_everything(cid: str, path: Path) -> None:
    outcome = REGISTRY.validate(cid, _load(path))
    assert outcome.ok, f"{path.name}: {outcome.to_dict()}"


@pytest.mark.parametrize(
    ("cid", "path"), ALL_INVALID, ids=[f"{c}-{p.name}" for c, p in ALL_INVALID]
)
def test_invalid_sample_fails_structure(cid: str, path: Path) -> None:
    outcome = REGISTRY.validate(cid, _load(path))
    assert not outcome.ok
    assert outcome.structural_errors, f"{path.name} should fail JSON Schema, not semantics"


@pytest.mark.parametrize(
    ("cid", "path"), ALL_SEMANTIC, ids=[f"{c}-{p.name}" for c, p in ALL_SEMANTIC]
)
def test_semantic_sample_is_well_formed_but_rejected(cid: str, path: Path) -> None:
    outcome = REGISTRY.validate(cid, _load(path))
    assert not outcome.structural_errors, (
        f"{path.name} was meant to be structurally valid; got {outcome.structural_errors}"
    )
    assert outcome.semantic_issues, f"{path.name} should be rejected by a semantic rule"


# ------------------------------------------------------------- named rules


def _issue_codes(cid: str, path: Path) -> set[str]:
    outcome = REGISTRY.validate(cid, _load(path))
    return {i.code for i in outcome.semantic_issues}


def test_assertion_without_oracle_is_rejected() -> None:
    path = SAMPLE_DIR / "C02" / "semantic" / "oracle_binding_missing.json"
    assert "C02_ORACLE_BINDING_MISSING" in _issue_codes("C02", path)


def test_oracle_change_repair_is_rejected() -> None:
    path = SAMPLE_DIR / "C11" / "semantic" / "forbidden_oracle_change.json"
    assert "C11_FORBIDDEN_CHANGE" in _issue_codes("C11", path)


def test_not_run_experiment_cannot_carry_metrics() -> None:
    path = SAMPLE_DIR / "C12" / "semantic" / "not_run_with_metrics.json"
    assert "C12_NOT_RUN_HAS_METRICS" in _issue_codes("C12", path)


def test_confirmed_diagnosis_must_name_its_rule() -> None:
    path = SAMPLE_DIR / "C08" / "semantic" / "confirmed_without_rule.json"
    assert "C08_CONFIRMED_WITHOUT_RULE" in _issue_codes("C08", path)


def test_inconclusive_must_not_blame_a_component() -> None:
    doc = {
        "diagnosis_id": "dg_x",
        "run_id": "run_x",
        "diagnosis_status": "INCONCLUSIVE",
        "disposition": "PRODUCT_DEFECT",
    }
    outcome = REGISTRY.validate("C10", doc)
    assert not outcome.structural_errors
    assert "C10_INCONCLUSIVE_BLAMES" in {i.code for i in outcome.semantic_issues}


def test_causal_edge_needs_basis() -> None:
    doc = {
        "edge_id": "ge_x",
        "src": "a",
        "dst": "b",
        "relation": "direct_causal",
        "basis_fields": "",
    }
    outcome = REGISTRY.validate("C07", doc)
    assert "C07_CAUSAL_NEEDS_BASIS" in {i.code for i in outcome.semantic_issues}


def test_raw_evidence_cannot_have_parents() -> None:
    doc = {
        "evidence_id": "evd_x",
        "evidence_type": "screenshot",
        "raw": True,
        "source": "browser",
        "run_id": "run_x",
        "step_id": None,
        "recorded_at": "2026-11-22T00:00:00+00:00",
        "summary": "x",
        "derived_from": ["evd_parent"],
        "retention": "run_scoped",
        "payload": {},
    }
    outcome = REGISTRY.validate("C06", doc)
    assert "C06_RAW_HAS_PARENTS" in {i.code for i in outcome.semantic_issues}


# ------------------------------------------------------------- rule index


def test_rule_index_matches_implementation() -> None:
    """The documented rule list must equal the codes the code can raise."""
    source = Path(semantic_module.__file__).read_text(encoding="utf-8")
    raised = set(re.findall(r'"(C\d{2}_[A-Z0-9_]+)"', source))
    documented = {code for rules in semantic_module.SEMANTIC_RULES.values() for code, _ in rules}
    assert raised == documented, (
        f"undocumented codes: {sorted(raised - documented)}; "
        f"declared but unreachable: {sorted(documented - raised)}"
    )


def test_rule_index_covers_every_contract() -> None:
    for cid in CONTRACT_IDS:
        assert semantic_module.SEMANTIC_RULES.get(cid), f"{cid} has no documented rules"


def test_unknown_contract_is_an_error_not_a_pass() -> None:
    outcome = REGISTRY.validate("C99", {"x": 1})
    assert not outcome.ok
    assert "no schema registered" in outcome.structural_errors[0]
