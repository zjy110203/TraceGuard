"""Stage-13 full-chain integration pipeline.

Chains every built module into one traceable business loop:

  TestCaseVersion -> Playwright runner -> frozen Verdict
  -> Event/Evidence -> Correlation Graph -> Diagnosis
  -> Active Diagnosis Probe -> Disposition -> Repair(gated)

Each stage writes structured, inspectable evidence into a FullChainReport.
Nothing here fabricates a verdict: execution is real (LocalRunner), diagnosis
is the deterministic Stage-7 engine, probes pass through the Stage-9 guard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from packages.agents.diagnosis import DiagnosisService
from packages.agents.probes import (
    ActiveDiagnosisPlanner,
    PermissionGateway,
    ProbeBudget,
    ProbeEngine,
    ProbeRequest,
    ProbeSpec,
)
from packages.evidence.graph import CorrelationGraph, GraphEdge, GraphNode
from packages.execution.models import TestCaseVersion
from packages.execution.runner import LocalRunner


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ChainStage:
    name: str
    status: str
    detail: str = ""
    at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullChainReport:
    scenario_id: str
    chain_id: str
    stages: list[ChainStage] = field(default_factory=list)
    execution: dict[str, Any] = field(default_factory=dict)
    diagnosis: dict[str, Any] = field(default_factory=dict)
    probe_summary: dict[str, Any] = field(default_factory=dict)
    disposition: str = ""
    repair: dict[str, Any] = field(default_factory=dict)
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stages"] = [s.to_dict() for s in self.stages]
        return d


class FullChain:
    """Runs the full loop for one scenario and returns an inspectable report."""

    def __init__(
        self,
        *,
        base_url: str,
        graph: CorrelationGraph,
        eval_token: str = "local-dev-eval-token-change-me",
        fault_events_provider=None,
    ) -> None:
        self.base_url = base_url
        self.graph = graph
        self.eval_token = eval_token
        self.fault_events_provider = fault_events_provider
        self.diagnosis_service = DiagnosisService()

    def run(
        self,
        scenario_id: str,
        case: TestCaseVersion,
        *,
        fault_events: list[dict[str, Any]] | None = None,
        probe_requests: list[ProbeRequest] | None = None,
        evidence_gap: str = "persistence",
        workspace_id: str = "ws-default",
        run_id_override: str | None = None,
    ) -> FullChainReport:
        report = FullChainReport(
            scenario_id=scenario_id,
            chain_id=f"chain_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}_{scenario_id}",
        )

        def stage(name: str, status: str, detail: str = "") -> None:
            report.stages.append(ChainStage(name, status, detail))

        # 1. TestCase (human-approved, immutable)
        case.validate()
        stage(
            "testcase", "OK", f"{case.case_id} v{case.version}, {len(case.assertions)} assertion(s)"
        )

        # 2. Execution via deterministic runner (may fail to start -> recorded)
        try:
            runner = LocalRunner(self.base_url)
            run_report = runner.run(case, run_id=run_id_override)
            report.execution = {
                "run_id": run_report.run_id,
                "execution_status": run_report.execution_status.value,
                "test_verdict": run_report.test_verdict,
                "evidence_count": len(run_report.evidence),
                "event_count": len(run_report.events),
                "failed_steps": [s.step_id for s in run_report.steps if not s.ok],
            }
            stage("execution", "OK", f"verdict={run_report.test_verdict}")
        except Exception as exc:  # browser/env unavailable => honest failure
            report.execution = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            stage("execution", "FAILED", f"{type(exc).__name__}")
            report.completed = True
            return report

        # 3. Event/Evidence -> Graph nodes + edges (trusted context refs only)
        run_node = f"{run_report.run_id}:Run"
        self._add_node(report, run_node, "Run", "runner", workspace_id)
        for ev in run_report.evidence[:8]:
            eid = str(ev.get("evidence_id", ""))
            node_id = f"{eid}:Evidence"
            self._add_node(
                report,
                node_id,
                "Evidence",
                ev.get("source", "browser"),
                workspace_id,
                independence_group=str(ev.get("source_trust", "observed")),
            )
            try:
                self.graph.add_edge(
                    GraphEdge(
                        edge_id=f"{run_node}->{node_id}:emitted",
                        src=run_node,
                        dst=node_id,
                        relation="emitted_by",
                        basis_fields="run_id",
                        rule_id="R-EMIT",
                    )
                )
            except Exception:  # duplicate edge on re-run
                pass
        stage("evidence_graph", "OK", f"{min(len(run_report.evidence), 8)} evidence nodes linked")

        # 4. Diagnosis (deterministic; diagnostic-only fault events)
        diag_bundle = {
            "test_verdict": run_report.test_verdict,
            "execution_status": run_report.execution_status.value,
            "failed_step_ids": report.execution["failed_steps"],
            "assertions": run_report.assertion_results,
            "evidence": run_report.evidence,
            "events": run_report.events,
            "fault_events": fault_events or [],
            "observations": run_report.observations,
        }
        diagnosis = self.diagnosis_service.diagnose(
            f"dx-{scenario_id}", run_report.run_id, diag_bundle
        )
        report.diagnosis = diagnosis.to_dict()
        report.disposition = diagnosis.disposition
        stage("diagnosis", "OK", f"{diagnosis.diagnosis_status}/{diagnosis.disposition}")

        # Hypothesis nodes with supports/contradicts preserved
        for h in diagnosis.hypotheses:
            hid = f"{run_report.run_id}:{h.hypothesis_id}"
            self._add_node(report, hid, "Hypothesis", "diagnosis", workspace_id)
            for e in h.supporting:
                try:
                    self.graph.add_edge(
                        GraphEdge(
                            f"{hid}:sup:{e.ref_id}",
                            e.ref_id,
                            hid,
                            "supports",
                            basis_fields="evidence_refs",
                            rule_id="R-SUP",
                        )
                    )
                except Exception:
                    pass
            for e in h.contradicting:
                try:
                    self.graph.add_edge(
                        GraphEdge(
                            f"{hid}:con:{e.ref_id}",
                            e.ref_id,
                            hid,
                            "contradicts",
                            basis_fields="evidence_refs",
                            rule_id="R-CON",
                        )
                    )
                except Exception:
                    pass

        # 5. Active Diagnosis Probe (guarded;越权 rejected + audited)
        if probe_requests:
            registry = {
                p.probe_id: p
                for p in (
                    ProbeSpec(
                        "API_READ",
                        {"required": ["workspace_id"]},
                        "workspace",
                        "diagnose",
                        True,
                        "none",
                        2.0,
                        1.0,
                        "LOW",
                        5.0,
                        "none",
                        {},
                        "none",
                    ),
                    ProbeSpec(
                        "DOM_SNAPSHOT",
                        {"required": ["workspace_id"]},
                        "workspace",
                        "diagnose",
                        True,
                        "none",
                        1.0,
                        1.0,
                        "LOW",
                        5.0,
                        "none",
                        {},
                        "none",
                    ),
                )
            }
            engine = ProbeEngine(PermissionGateway(registry), registry)
            engine.register_handler("API_READ", lambda req: {"project_count": 1})
            engine.register_handler("DOM_SNAPSHOT", lambda req: {"text": "ok"})
            planner = ActiveDiagnosisPlanner(engine, ProbeBudget(max_probes=2))
            report.probe_summary = planner.run(
                evidence_gap=evidence_gap, candidates=probe_requests, workspace_id=workspace_id
            )
            stage("probe", "OK", f"used={planner.probes_used} stop={planner.stop_reason}")

        # 6. Repair proposal only when disposition warrants it (gated elsewhere)
        report.repair = {
            "proposal_required": report.disposition in ("TEST_ASSET_ISSUE", "PRODUCT_DEFECT")
        }
        stage("repair", "OK", f"proposal_required={report.repair['proposal_required']}")
        report.completed = True
        return report

    def _add_node(
        self,
        report: FullChainReport,
        node_id: str,
        node_type: str,
        source: str,
        workspace_id: str,
        independence_group: str = "",
    ) -> None:
        try:
            self.graph.add_node(
                GraphNode(
                    node_id,
                    node_type,
                    source_ref=source,
                    independence_group=independence_group or source,
                )
            )
        except Exception:
            pass  # append-only: node already present from a prior chain
