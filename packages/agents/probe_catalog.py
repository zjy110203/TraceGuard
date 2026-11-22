"""Draft catalogue for the seven read-only probes whitelisted by the gateway.

``READ_ONLY_PROBES`` in ``probes.py`` already names these seven probe ids — the
permission gateway refuses anything outside the set — but until now they had no
``ProbeSpec``, so the registry could not be inspected or audited. This module
supplies the missing specifications.

Every specification is read-only and LOW/MEDIUM risk. Nothing here can mutate the
product: the allowed DB access is one whitelisted aggregate template, and the
console/network readers only read what the browser already produced.

Status: DRAFT. Thresholds (cost/duration) are developer defaults, not frozen.
"""

from __future__ import annotations

from typing import Any

from packages.agents.probes import (
    READ_ONLY_PROBES,
    PermissionGateway,
    ProbeEngine,
    ProbeSpec,
)

_WORKSPACE_INPUT: dict[str, Any] = {
    "type": "object",
    "required": ["workspace_id"],
    "properties": {"workspace_id": {"type": "string"}},
}


def _spec(
    probe_id: str,
    *,
    scope: str,
    permission: str,
    risk: str,
    cost: float,
    duration: float,
    output: dict[str, Any],
    input_schema: dict[str, Any] | None = None,
) -> ProbeSpec:
    return ProbeSpec(
        probe_id=probe_id,
        input_schema=input_schema or _WORKSPACE_INPUT,
        target_scope=scope,
        permission_required=permission,
        read_only=True,
        side_effects="none",
        estimated_cost=cost,
        estimated_duration=duration,
        risk_level=risk,
        timeout=5.0,
        retry_policy="none",
        output_schema=output,
        cleanup_policy="none",
    )


_SPECS: tuple[ProbeSpec, ...] = (
    _spec(
        "DOM_SNAPSHOT",
        scope="run",
        permission="diagnosis.probe.readonly.dom",
        risk="LOW",
        cost=0.1,
        duration=0.3,
        output={"type": "object", "properties": {"html_len": {"type": "integer"}}},
    ),
    _spec(
        "SCREENSHOT",
        scope="run",
        permission="diagnosis.probe.readonly.screenshot",
        risk="LOW",
        cost=0.2,
        duration=0.8,
        output={"type": "object", "properties": {"evidence_id": {"type": "string"}}},
    ),
    _spec(
        "NETWORK_READ",
        scope="run",
        permission="diagnosis.probe.readonly.network",
        risk="LOW",
        cost=0.1,
        duration=0.2,
        output={"type": "object", "properties": {"entries": {"type": "array"}}},
    ),
    _spec(
        "CONSOLE_READ",
        scope="run",
        permission="diagnosis.probe.readonly.console",
        risk="LOW",
        cost=0.1,
        duration=0.2,
        output={"type": "object", "properties": {"messages": {"type": "array"}}},
    ),
    _spec(
        "API_READ",
        scope="project",
        permission="diagnosis.probe.readonly.api",
        risk="MEDIUM",
        cost=0.3,
        duration=0.5,
        output={"type": "object", "properties": {"status": {"type": "integer"}}},
    ),
    _spec(
        "BACKEND_LOG_READ",
        scope="workspace",
        permission="diagnosis.probe.readonly.logs",
        risk="MEDIUM",
        cost=0.3,
        duration=0.6,
        output={"type": "object", "properties": {"lines": {"type": "array"}}},
    ),
    _spec(
        "DATABASE_READ",
        scope="workspace",
        permission="diagnosis.probe.readonly.db",
        risk="MEDIUM",
        cost=0.4,
        duration=0.4,
        output={"type": "object", "properties": {"rows": {"type": "array"}}},
        input_schema={
            "type": "object",
            "required": ["workspace_id", "template"],
            "properties": {
                "workspace_id": {"type": "string"},
                "template": {"type": "string", "enum": ["project_count_by_workspace"]},
            },
        },
    ),
)


def default_probe_specs() -> dict[str, ProbeSpec]:
    """Registry keyed by probe_id. Covers exactly the gateway whitelist."""
    specs = {s.probe_id: s for s in _SPECS}
    missing = READ_ONLY_PROBES - set(specs)
    assert not missing, f"whitelisted probes without a spec: {sorted(missing)}"
    return specs


def build_default_engine() -> ProbeEngine:
    """Engine with a real registry; handlers must be registered by the caller."""
    registry = default_probe_specs()
    return ProbeEngine(PermissionGateway(registry), registry)


def specs_as_dicts() -> list[dict[str, Any]]:
    return [
        {
            "probe_id": s.probe_id,
            "input_schema": s.input_schema,
            "target_scope": s.target_scope,
            "permission_required": s.permission_required,
            "read_only": s.read_only,
            "side_effects": s.side_effects,
            "estimated_cost": s.estimated_cost,
            "estimated_duration": s.estimated_duration,
            "risk_level": s.risk_level,
            "timeout": s.timeout,
            "retry_policy": s.retry_policy,
            "output_schema": s.output_schema,
            "cleanup_policy": s.cleanup_policy,
        }
        for s in _SPECS
    ]
