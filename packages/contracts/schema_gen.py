"""JSON Schema generation derived from the real dataclasses.

Why generated rather than hand-written: the contracts must describe what the code
actually emits. Deriving them from the live annotations makes schema/code drift
detectable (a test regenerates and compares), instead of trusting a hand-copied
JSON file to stay in sync.

Honesty rules encoded here:
* Only fields that exist on the real dataclass are emitted. Nothing is invented.
* Optionality and required-ness come from dataclass defaults, not from guesses.
* Enum vocabularies come from the real Enum classes.
* Constraints that the code enforces (e.g. ``sequence >= 1``) are attached by the
  caller in ``CONSTRAINTS`` with a comment; anything not enforced stays absent.
"""

from __future__ import annotations

import enum
import operator
import sys
import types
import typing
from dataclasses import MISSING, fields, is_dataclass
from functools import reduce
from typing import Any

PY_TO_JSON: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
    tuple: "array",
    type(None): "null",
}

# (dataclass name, field name) -> extra JSON Schema keywords.
# Each entry must correspond to a real runtime check; see semantic.py for the
# cross-field rules that JSON Schema cannot express.
CONSTRAINTS: dict[tuple[str, str], dict[str, Any]] = {
    ("TestCaseVersion", "version"): {"minimum": 1},
    ("EventEnvelope", "sequence"): {"minimum": 1},
    ("RepairProposal", "revision"): {"minimum": 1},
    ("RepairProposal", "base_testcase_version"): {"minimum": 1},
    ("RepairProposal", "candidate_testcase_version"): {"minimum": 1},
    ("RepairProposal", "promotion_target_version"): {"minimum": 1},
    ("ProbeBudget", "max_probes"): {"minimum": 0},
    ("ProbeSpec", "estimated_cost"): {"minimum": 0},
    ("ProbeSpec", "estimated_duration"): {"minimum": 0},
    ("ProbeSpec", "timeout"): {"minimum": 0},
}


def _ann_globals(cls: type) -> dict[str, Any]:
    module = sys.modules.get(cls.__module__)
    return dict(vars(module)) if module else {}


def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls, globalns=_ann_globals(cls), include_extras=False)


def _is_optional(tp: Any) -> bool:
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        return type(None) in typing.get_args(tp)
    return False


def _unwrap_optional(tp: Any) -> Any:
    args = [a for a in typing.get_args(tp) if a is not type(None)]
    if len(args) == 1:
        return args[0]
    # Rebuild a non-optional union without resorting to typing.Union[...].
    return reduce(operator.or_, args)


class _Builder:
    """Recursively converts a dataclass tree into ``$defs`` + a root ``$ref``."""

    def __init__(self) -> None:
        self.defs: dict[str, Any] = {}

    def schema_for(self, tp: Any) -> dict[str, Any]:
        if _is_optional(tp):
            return {"anyOf": [self.schema_for(_unwrap_optional(tp)), {"type": "null"}]}

        origin = typing.get_origin(tp)
        args = typing.get_args(tp)

        if origin in (list, tuple, set, frozenset):
            inner = args[0] if args else str
            item_schema = self.schema_for(inner)
            return {"type": "array", "items": item_schema}
        if origin is dict:
            return {"type": "object", "additionalProperties": True}
        if origin is typing.Union or origin is types.UnionType:
            return {"anyOf": [self.schema_for(a) for a in args]}
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            return {"type": "string", "enum": [str(m.value) for m in tp]}
        if isinstance(tp, type) and is_dataclass(tp):
            name = tp.__name__
            if name not in self.defs:
                self.defs[name] = {}  # placeholder guards recursive cycles
                self.defs[name] = self._object_schema(tp)
            return {"$ref": f"#/$defs/{name}"}
        if tp is Any:
            return {}
        json_type = PY_TO_JSON.get(tp)
        if json_type:
            return {"type": json_type}
        return {}

    def _object_schema(self, cls: type) -> dict[str, Any]:
        props: dict[str, Any] = {}
        required: list[str] = []
        for f in fields(cls):
            prop = self.schema_for(_hints(cls).get(f.name, f.type))
            extra = CONSTRAINTS.get((cls.__name__, f.name))
            if extra:
                prop = {**prop, **extra}
            props[f.name] = prop
            if f.default is MISSING and f.default_factory is MISSING:  # type: ignore[misc]
                required.append(f.name)
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }


def build_object_schema(
    cls: type,
    *,
    schema_id: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    """Full JSON Schema (Draft 2020-12) for one dataclass tree."""
    b = _Builder()
    root = b.schema_for(cls)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "title": title,
        "description": description,
        "status": "draft",
        "$ref": root["$ref"],
        "$defs": b.defs,
    }


def build_oneof_schema(
    classes: list[type],
    *,
    schema_id: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    """Schema accepting exactly one of several real dataclass shapes."""
    b = _Builder()
    refs = [b.schema_for(c) for c in classes]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "title": title,
        "description": description,
        "status": "draft",
        "oneOf": refs,
        "$defs": b.defs,
    }
