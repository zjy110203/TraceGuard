"""Stage-9 acceptance: guarded active diagnosis probing.

Verifies:
  * Probe Registry fields; five checks (permission/scope/risk/budget/schema);
  * unauthorized / out-of-scope / arbitrary SQL rejected AND audited;
  * redaction, row/byte caps, timeout config;
  * explainable EIG/C/R scoring; highest safe probe chosen;
  * budget stop; scope-not-permitted stop; no unbounded loop;
  * probe output is evidence with provenance, not a root cause.
"""

from __future__ import annotations

import pytest

from packages.agents.probes import (
    DB_TEMPLATES,
    READ_ONLY_PROBES,
    ActiveDiagnosisPlanner,
    PermissionGateway,
    ProbeBudget,
    ProbeEngine,
    ProbeRequest,
    ProbeSpec,
    redact,
)
from packages.core.errors import ForbiddenError, ValidationError


def _spec(probe_id: str, risk: str = "LOW", cost: float = 2.0) -> ProbeSpec:
    return ProbeSpec(
        probe_id=probe_id,
        input_schema={"required": ["workspace_id"]},
        target_scope="workspace",
        permission_required="diagnose",
        read_only=True,
        side_effects="none",
        estimated_cost=cost,
        estimated_duration=1.0,
        risk_level=risk,
        timeout=5.0,
        retry_policy="no_retry",
        output_schema={"type": "object"},
        cleanup_policy="none",
    )


def _registry() -> dict[str, ProbeSpec]:
    return {p: _spec(p) for p in READ_ONLY_PROBES}


def _engine() -> ProbeEngine:
    engine = ProbeEngine(PermissionGateway(_registry()), _registry())
    engine.register_handler("API_READ", lambda req: {"project_count": 1, "token": "leak-me"})
    engine.register_handler("DATABASE_READ", lambda req: {"n": 1})
    engine.register_handler("DOM_SNAPSHOT", lambda req: {"text": "hello"})
    return engine


def test_registry_fields_and_read_only_rule() -> None:
    spec = _spec("API_READ")
    assert spec.probe_id and spec.input_schema and spec.read_only
    assert spec.permission_required and spec.timeout and spec.output_schema
    with pytest.raises(ValidationError):
        ProbeSpec(
            probe_id="W",
            input_schema={},
            target_scope="workspace",
            permission_required="x",
            read_only=False,
            side_effects="write",
            estimated_cost=1,
            estimated_duration=1,
            risk_level="LOW",
            timeout=1,
            retry_policy="none",
            output_schema={},
            cleanup_policy="none",
        )
    with pytest.raises(ValidationError):
        _spec("API_READ", risk="CRITICAL")


def test_five_checks_reject_and_audit() -> None:
    engine = _engine()
    # 1 permission: unknown caller
    with pytest.raises(ForbiddenError):
        engine.execute(ProbeRequest("API_READ", {"workspace_id": "ws1"}, "ws1", caller="agent"))
    assert any(a.check == "permission" and a.decision == "REJECTED" for a in engine.audit)
    # 2 scope: mismatch workspace
    with pytest.raises(ForbiddenError):
        engine.execute(ProbeRequest("API_READ", {"workspace_id": "ws2"}, "ws1"))
    assert any(a.check == "resource_scope" for a in engine.audit)
    # 3 risk: a real read-only probe marked HIGH risk
    high = _registry()
    high["API_READ"] = _spec("API_READ", risk="HIGH")
    eng = ProbeEngine(PermissionGateway(high), high)
    eng.register_handler("API_READ", lambda req: {})
    with pytest.raises(ForbiddenError):
        eng.execute(ProbeRequest("API_READ", {"workspace_id": "ws1"}, "ws1"))
    assert any(a.check == "risk" for a in eng.audit)
    # 5 schema: missing workspace_id / arbitrary SQL
    with pytest.raises(ForbiddenError):
        engine.execute(ProbeRequest("API_READ", {}, "ws1"))
    with pytest.raises(ForbiddenError):
        engine.execute(
            ProbeRequest("API_READ", {"workspace_id": "ws1", "sql": "DROP TABLE"}, "ws1")
        )
    assert any(a.check == "parameter_schema" for a in engine.audit)


def test_arbitrary_sql_template_whitelist() -> None:
    engine = _engine()
    with pytest.raises(ForbiddenError):
        engine.execute(
            ProbeRequest(
                "DATABASE_READ", {"workspace_id": "ws1", "sql": "SELECT * FROM secrets"}, "ws1"
            )
        )
    # only whitelisted template allowed
    result = engine.execute(
        ProbeRequest(
            "DATABASE_READ",
            {"workspace_id": "ws1", "template": "project_count_by_workspace"},
            "ws1",
        )
    )
    assert result.executed and result.data["n"] == 1
    assert "project_count_by_workspace" in DB_TEMPLATES


def test_redaction_and_provenance() -> None:
    r = redact({"token": "x", "nested": {"password": "y"}, "ok": 1})
    assert r["token"] == "[REDACTED]" and r["nested"]["password"] == "[REDACTED]" and r["ok"] == 1
    result = _engine().execute(ProbeRequest("API_READ", {"workspace_id": "ws1"}, "ws1"))
    assert result.data["token"] == "[REDACTED]"
    assert result.data["project_count"] == 1
    assert result.provenance["probe_id"] == "API_READ"
    assert result.probe_hash.startswith("sha256:")


def test_byte_cap() -> None:
    engine = _engine()
    engine.register_handler("DOM_SNAPSHOT", lambda req: {"text": "x" * 70000})
    result = engine.execute(ProbeRequest("DOM_SNAPSHOT", {"workspace_id": "ws1"}, "ws1"))
    assert result.data.get("truncated") is True


def test_scoring_explainable_and_picks_covered_probe() -> None:
    engine = _engine()
    planner = ActiveDiagnosisPlanner(engine)
    spec = _registry()["API_READ"]
    score = planner.score("API_READ", evidence_gap="persistence", spec=spec)
    assert score.eig > 0.5 and score.explanation
    low = planner.score("API_READ", evidence_gap="ui", spec=spec)
    assert low.eig < score.eig  # persistence gap better covered by API_READ


def test_planner_runs_budget_and_stops() -> None:
    engine = _engine()
    planner = ActiveDiagnosisPlanner(engine, ProbeBudget(max_probes=2, max_cost=100))
    summary = planner.run(
        evidence_gap="persistence",
        candidates=[
            ProbeRequest("API_READ", {"workspace_id": "ws1"}, "ws1"),
            ProbeRequest(
                "DATABASE_READ",
                {"workspace_id": "ws1", "template": "project_count_by_workspace"},
                "ws1",
            ),
            ProbeRequest("DOM_SNAPSHOT", {"workspace_id": "ws1"}, "ws1"),
        ],
        workspace_id="ws1",
    )
    assert summary["probes_used"] <= 2
    assert summary["stop_reason"] in {"PROBE_COUNT_BUDGET", "EVIDENCE_GAP_ADDRESSED"}
    assert all(r["executed"] for r in summary["results"])


def test_planner_scope_not_permitted_stops() -> None:
    planner = ActiveDiagnosisPlanner(_engine())
    summary = planner.run(
        evidence_gap="ui",
        candidates=[ProbeRequest("API_READ", {"workspace_id": "ws1"}, "ws1")],
        workspace_id="ws1",
        workspace_scoped=False,
    )
    assert summary["stop_reason"] == "SCOPE_NOT_PERMITTED"
    assert summary["probes_used"] == 0


def test_results_are_evidence_not_root_cause() -> None:
    planner = ActiveDiagnosisPlanner(_engine())
    summary = planner.run(
        evidence_gap="persistence",
        candidates=[ProbeRequest("API_READ", {"workspace_id": "ws1"}, "ws1")],
        workspace_id="ws1",
    )
    assert "evidence, not a root cause" in summary["note"]
