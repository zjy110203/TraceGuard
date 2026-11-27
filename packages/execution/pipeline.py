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

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from packages.agents.diagnosis import DiagnosisService
from packages.agents.probes import (
    ActiveDiagnosisPlanner,
    PermissionGateway,
    ProbeBudget,
    ProbeEngine,
    ProbeRequest,
    ProbeSpec,
)
from packages.core.errors import ConflictError
from packages.core.logging import get_logger
from packages.evidence.graph import CorrelationGraph, GraphEdge, GraphNode
from packages.execution.locators import LocatorRegistry
from packages.execution.models import TestCaseVersion
from packages.execution.runner import ExecutionAborted, LocalRunner

logger = get_logger("tg.chain")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ChainProgress:
    """Stage-level progress vocabulary (R5-4.2 §4.3).

    Run status answers "where is the run in its lifecycle"; a stage answers
    "which part of the chain is executing". They are separate on purpose: a run
    can be RUNNING while the diagnosis stage is still pending.
    """

    TESTCASE = "testcase"
    EXECUTION = "execution"
    VERDICT = "verdict"
    EVENT_PERSISTENCE = "event_persistence"
    EVIDENCE_PERSISTENCE = "evidence_persistence"
    GRAPH = "graph"
    DIAGNOSIS = "diagnosis"
    CLEANUP = "cleanup"

    ORDER = (
        TESTCASE,
        EXECUTION,
        VERDICT,
        EVENT_PERSISTENCE,
        EVIDENCE_PERSISTENCE,
        GRAPH,
        DIAGNOSIS,
        CLEANUP,
    )


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
    # A cancelled chain is not a failed one, and it is never a passing one.
    cancelled: bool = False
    # What happened to the environment afterwards (R5-4.2 CleanupPolicy).
    cleanup: dict[str, Any] = field(default_factory=dict)
    # Captured artifacts, carried through so a caller can persist them into the
    # evidence plane. Without these the graph page has nodes but no evidence
    # rows, and the only way to inspect a chain was to re-run it.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    # Execution detail, so a caller can register this run alongside ordinary
    # workbench runs (steps list, assertion results, UI observations). The
    # diagnosis endpoint reads these to re-derive a diagnosis for the run id.
    steps: list[dict[str, Any]] = field(default_factory=list)
    assertion_results: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    # What the caller managed to persist (events/evidence). Empty means the
    # chain has not been persisted yet — never "nothing to persist".
    persisted: dict[str, Any] = field(default_factory=dict)
    # Graph writes that failed for a reason other than "already present". An
    # empty list is the only proof the graph stage really succeeded; before this
    # existed every write error was swallowed and the Graph page looked empty.
    graph_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stages"] = [s.to_dict() for s in self.stages]
        return d


class FullChain:
    """Runs the full loop for one scenario and returns an inspectable report.

    The chain owns its *stages*, not its Run row. A caller (the Orchestrator)
    registers the Run before calling ``run`` and passes ``run_id_override`` so
    the same id appears in the report, in every event, and in every graph node.
    ``progress`` and ``cancel_check`` are the two hooks that make the lifecycle
    observable and interruptible without this module knowing about databases.
    """

    def __init__(
        self,
        *,
        base_url: str,
        graph: CorrelationGraph,
        eval_token: str = "local-dev-eval-token-change-me",
        fault_events_provider=None,
        locators: LocatorRegistry | None = None,
        data_provider: Any | None = None,
        teamflow_test_user: str = "tester",
        progress: Callable[[str, str, str], None] | None = None,
        allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost"),
    ) -> None:
        self.base_url = base_url
        self.graph = graph
        self.eval_token = eval_token
        self.fault_events_provider = fault_events_provider
        # Cases are written against logical targets (``loc:login.username``). The
        # registry must reach the runner or every fill/click sends a literal
        # "loc:..." string to Playwright and fails with an invalid selector.
        self.locators = locators
        # Resolves value_ref secrets (R5-4.1). Same provider the workbench uses,
        # so a chain and a workbench run bind identical credentials.
        self.data_provider = data_provider
        self.teamflow_test_user = teamflow_test_user
        self.progress = progress
        self.allowed_hosts = allowed_hosts
        self.diagnosis_service = DiagnosisService()

    # -------------------------------------------------------------- lifecycle

    def _stage(self, report: FullChainReport, name: str, status: str, detail: str = "") -> None:
        """Record a stage and publish it, so progress is observable live."""
        report.stages.append(ChainStage(name, status, detail))
        if self.progress is not None:
            try:
                self.progress(name, status, detail)
            except Exception:  # noqa: BLE001 - progress must never break a chain
                pass

    def _check_cancel(
        self,
        cancel_check: Callable[[], bool] | None,
        report: FullChainReport,
        where: str,
    ) -> bool:
        """Stop between stages when a cancellation was requested.

        Returning a flag (instead of raising) keeps the partial report — the
        evidence captured before the cancel is exactly what a reviewer needs.
        """
        if cancel_check is not None and cancel_check():
            report.cancelled = True
            report.completed = False
            self._stage(report, "cancel", "CANCELLED", f"cancel requested before {where}")
            return True
        return False

    # ---------------------------------------------------------------- cleanup

    def plan_cleanup(self, case: TestCaseVersion) -> dict[str, Any]:
        """Decide *before* running whether this environment may be reset.

        A reset is a destructive operation on a live database. It is only
        allowed against the approved local test instance whose eval control
        plane answers with the eval token — never against an arbitrary host.
        """
        policy = case.cleanup
        host = (urlparse(self.base_url).hostname or "").lower()
        plan: dict[str, Any] = {
            "strategy": policy.strategy,
            "endpoint": policy.reset_endpoint,
            "allowed": False,
            "reason": "",
        }
        if policy.strategy == "none":
            plan["reason"] = "policy=none (no reset requested)"
            return plan
        if host not in self.allowed_hosts:
            plan["reason"] = f"host {host!r} is not an approved local test instance; reset refused"
            return plan
        try:
            resp = httpx.get(
                f"{self.base_url}/__eval__/faults",
                headers={"X-Eval-Token": self.eval_token},
                timeout=5,
                trust_env=False,
            )
            plan["allowed"] = resp.status_code == 200
            plan["reason"] = (
                "eval control plane reachable"
                if plan["allowed"]
                else f"eval control plane returned {resp.status_code}; reset skipped"
            )
        except httpx.HTTPError as exc:
            plan["reason"] = f"eval control plane unreachable ({type(exc).__name__}); reset skipped"
        return plan

    def apply_cleanup(self, report: FullChainReport, plan: dict[str, Any]) -> None:
        """Execute the plan and record the outcome separately from the verdict."""
        if report.cleanup.get("done"):
            return  # idempotent: a cancel path may reach this twice
        report.cleanup = {**plan, "done": True}
        if not plan.get("allowed"):
            self._stage(report, "cleanup", "SKIPPED", str(plan.get("reason") or ""))
            return
        try:
            resp = httpx.post(
                f"{self.base_url}{plan['endpoint']}",
                headers={"X-Eval-Token": self.eval_token},
                timeout=10,
                trust_env=False,
            )
            ok = resp.status_code == 200
            report.cleanup["applied"] = ok
            report.cleanup["status_code"] = resp.status_code
            self._stage(
                report,
                "cleanup",
                "OK" if ok else "FAILED",
                f"{plan['endpoint']} -> {resp.status_code}",
            )
        except httpx.HTTPError as exc:
            report.cleanup["applied"] = False
            report.cleanup["error"] = f"{type(exc).__name__}: {exc}"[:200]
            # A failed cleanup is its own finding; it never rewrites the verdict.
            self._stage(report, "cleanup", "FAILED", report.cleanup["error"])

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
        cancel_check: Callable[[], bool] | None = None,
    ) -> FullChainReport:
        report = FullChainReport(
            scenario_id=scenario_id,
            chain_id=f"chain_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}_{scenario_id}",
        )

        def stage(name: str, status: str, detail: str = "") -> None:
            self._stage(report, name, status, detail)

        # 1. TestCase (human-approved, immutable)
        case.validate()
        stage(
            "testcase", "OK", f"{case.case_id} v{case.version}, {len(case.assertions)} assertion(s)"
        )

        # 1b. Environment pre-check: decide whether a reset is even permitted
        #     before anything runs, so cleanup can never be improvised later.
        cleanup_plan = self.plan_cleanup(case)
        stage(
            "cleanup_pre",
            "OK" if cleanup_plan["allowed"] else "SKIPPED",
            f"strategy={cleanup_plan['strategy']}: {cleanup_plan['reason']}",
        )

        # 2. Execution via deterministic runner (may fail to start -> recorded)
        runner = LocalRunner(
            self.base_url,
            eval_token=self.eval_token,
            locators=self.locators,
            data_provider=self.data_provider,
            session_cookie=("tf_session", self.teamflow_test_user),
        )
        try:
            if self._check_cancel(cancel_check, report, "execution"):
                self.apply_cleanup(report, cleanup_plan)
                return report
            run_report = runner.run(
                case,
                run_id=run_id_override,
                abort_hook=self._abort_hook(cancel_check),
            )
        except Exception as exc:  # browser/env unavailable => honest failure
            report.execution = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            stage("execution", "FAILED", f"{type(exc).__name__}")
            self.apply_cleanup(report, cleanup_plan)
            report.completed = False
            return report

        report.execution = {
            "run_id": run_report.run_id,
            "execution_status": run_report.execution_status.value,
            "test_verdict": run_report.test_verdict,
            "evidence_count": len(run_report.evidence),
            "event_count": len(run_report.events),
            "failed_steps": [s.step_id for s in run_report.steps if not s.ok],
            "blocked": run_report.blocked,
            "blocked_reason": run_report.blocked_reason,
        }
        report.evidence = list(run_report.evidence)
        report.events = list(run_report.events)
        report.steps = [s.to_dict() for s in run_report.steps]
        report.assertion_results = list(run_report.assertion_results)
        report.observations = dict(run_report.observations)

        if run_report.aborted_reason:
            # A cancelled / timed-out run stops here: no graph, no diagnosis, no
            # verdict rewrite. Evidence captured so far is kept and returned.
            cancelled = "cancel" in run_report.aborted_reason.lower()
            report.cancelled = cancelled
            stage(
                "execution",
                "CANCELLED" if cancelled else "FAILED",
                run_report.aborted_reason,
            )
            report.disposition = "CANCELLED" if cancelled else "EXECUTION_FAILED"
            self.apply_cleanup(report, cleanup_plan)
            report.completed = False
            return report

        stage("execution", "OK", f"verdict={run_report.test_verdict}")
        stage(
            "verdict",
            "OK" if run_report.test_verdict else "FAILED",
            f"{run_report.test_verdict}/{run_report.execution_status.value}",
        )

        # 3. Event/Evidence -> Graph nodes + edges (trusted context refs only)
        if self._check_cancel(cancel_check, report, "graph"):
            self.apply_cleanup(report, cleanup_plan)
            return report
        run_node = f"{run_report.run_id}:Run"
        self._add_node(report, run_node, "Run", "runner", workspace_id)
        linked = 0
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
            if self._add_edge(
                report,
                GraphEdge(
                    edge_id=f"{run_node}->{node_id}:emitted",
                    src=run_node,
                    dst=node_id,
                    relation="emitted_by",
                    basis_fields="run_id",
                    rule_id="R-EMIT",
                ),
            ):
                linked += 1
        stage(
            "graph",
            # A partial graph is reported as PARTIAL, not OK: the Graph page is
            # a product surface, so a silent partial write is a defect.
            "OK" if not report.graph_errors else "PARTIAL",
            f"{linked} evidence nodes linked"
            + (f", {len(report.graph_errors)} writes failed" if report.graph_errors else ""),
        )

        # 4. Diagnosis (deterministic; diagnostic-only fault events)
        if self._check_cancel(cancel_check, report, "diagnosis"):
            self.apply_cleanup(report, cleanup_plan)
            return report
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
                self._add_edge(
                    report,
                    GraphEdge(
                        f"{hid}:sup:{e.ref_id}",
                        e.ref_id,
                        hid,
                        "supports",
                        basis_fields="evidence_refs",
                        rule_id="R-SUP",
                    ),
                )
            for e in h.contradicting:
                self._add_edge(
                    report,
                    GraphEdge(
                        f"{hid}:con:{e.ref_id}",
                        e.ref_id,
                        hid,
                        "contradicts",
                        basis_fields="evidence_refs",
                        rule_id="R-CON",
                    ),
                )

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

        # 7. Cleanup per the case's CleanupPolicy (recorded as its own stage;
        #    a cleanup failure is never allowed to rewrite the verdict).
        self.apply_cleanup(report, cleanup_plan)
        report.completed = True
        return report

    @staticmethod
    def _abort_hook(cancel_check: Callable[[], bool] | None) -> Callable[[], None] | None:
        """Bridge the chain's cancel flag into the runner's between-step hook."""
        if cancel_check is None:
            return None

        def check() -> None:
            if cancel_check():
                raise ExecutionAborted("cancel requested")

        return check

    def _add_node(
        self,
        report: FullChainReport,
        node_id: str,
        node_type: str,
        source: str,
        workspace_id: str,
        independence_group: str = "",
    ) -> bool:
        """Write one node, distinguishing "already there" from "it broke"."""
        try:
            self.graph.add_node(
                GraphNode(
                    node_id,
                    node_type,
                    source_ref=source,
                    independence_group=independence_group or source,
                )
            )
            return True
        except ConflictError:
            # Append-only: the node already exists from a prior chain/retry.
            return True
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            detail = f"node {node_id}: {type(exc).__name__}: {exc}"[:200]
            report.graph_errors.append(detail)
            logger.warning("chain_graph_node_failed", extra={"extra_fields": {"detail": detail}})
            return False

    def _add_edge(self, report: FullChainReport, edge: GraphEdge) -> bool:
        """Same contract as ``_add_node``: duplicates are fine, failures surface."""
        try:
            self.graph.add_edge(edge)
            return True
        except ConflictError:
            return True
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            detail = f"edge {edge.edge_id}: {type(exc).__name__}: {exc}"[:200]
            report.graph_errors.append(detail)
            logger.warning("chain_graph_edge_failed", extra={"extra_fields": {"detail": detail}})
            return False
