"""Contract registry: load C01~C12 schemas, validate structure then semantics.

Two-stage validation is deliberate. Structural JSON Schema answers "is this the
right shape?"; ``semantic.validate_semantics`` answers "does this instance make
sense as a TraceGuard object?" — e.g. every assertion having an Oracle binding,
a CONFIRMED diagnosis naming its rule, a NOT_RUN experiment carrying no metrics.

A contract is only "usable" when both stages agree. Files always carry
``status`` so a draft can never be mistaken for a frozen contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from packages.contracts.semantic import Issue, validate_semantics

CONTRACT_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = CONTRACT_DIR / "schemas"
SAMPLE_DIR = CONTRACT_DIR / "samples"
REGISTRY_FILE = CONTRACT_DIR / "registry.json"


@dataclass(frozen=True)
class ValidationOutcome:
    contract_id: str
    structural_errors: tuple[str, ...]
    semantic_issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return not self.structural_errors and not self.semantic_issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "ok": self.ok,
            "structural_errors": list(self.structural_errors),
            "semantic_issues": [i.to_dict() for i in self.semantic_issues],
        }


class ContractRegistry:
    """In-memory view over the on-disk schema set + registry manifest."""

    def __init__(self, schemas: dict[str, dict[str, Any]], manifest: dict[str, Any]) -> None:
        self._manifest = manifest
        self._schemas = schemas
        self._validators: dict[str, jsonschema.Draft202012Validator] = {}
        for cid, schema in schemas.items():
            # Raises SchemaError if the schema itself is malformed, which is what
            # we want: a broken contract must fail loudly at load time.
            jsonschema.Draft202012Validator.check_schema(schema)
            self._validators[cid] = jsonschema.Draft202012Validator(schema)

    @classmethod
    def load(
        cls,
        schema_dir: Path | None = None,
        registry_file: Path | None = None,
    ) -> ContractRegistry:
        schema_dir = schema_dir or SCHEMA_DIR
        registry_file = registry_file or REGISTRY_FILE
        manifest = json.loads(registry_file.read_text(encoding="utf-8"))
        schemas: dict[str, dict[str, Any]] = {}
        for entry in manifest.get("contracts", []):
            cid = entry["id"]
            fname = entry.get("schema_file")
            if not fname:
                continue
            path = schema_dir / fname
            if not path.exists():
                raise FileNotFoundError(f"schema declared but missing: {path}")
            schemas[cid] = json.loads(path.read_text(encoding="utf-8"))
        return cls(schemas, manifest)

    # ---------------------------------------------------------------- queries

    def contract_ids(self) -> tuple[str, ...]:
        return tuple(e["id"] for e in self._manifest.get("contracts", []))

    def entry(self, contract_id: str) -> dict[str, Any]:
        for e in self._manifest.get("contracts", []):
            if e["id"] == contract_id:
                return dict(e)
        raise KeyError(contract_id)

    def schema(self, contract_id: str) -> dict[str, Any]:
        return self._schemas[contract_id]

    def status(self, contract_id: str) -> str:
        return str(self.entry(contract_id).get("status", "unknown"))

    def missing_schemas(self) -> tuple[str, ...]:
        return tuple(cid for cid in self.contract_ids() if cid not in self._schemas)

    # ------------------------------------------------------------- validation

    def validate(self, contract_id: str, instance: Any) -> ValidationOutcome:
        validator = self._validators.get(contract_id)
        if validator is None:
            return ValidationOutcome(
                contract_id,
                (f"no schema registered for {contract_id}",),
                (),
            )
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        structural = tuple(
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        if structural:
            # Semantic rules assume a well-formed shape; running them on a broken
            # structure produces noise instead of signal.
            return ValidationOutcome(contract_id, structural, ())
        semantic = tuple(validate_semantics(contract_id, instance))
        return ValidationOutcome(contract_id, (), semantic)
