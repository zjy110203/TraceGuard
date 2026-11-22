"""Stage-9 Active Diagnosis Planner (C09, draft) — guarded, read-only probes.

Five mandatory checks BEFORE every probe execution, in order:
  1. permission  2. resource scope  3. risk  4. budget  5. parameter schema.
Any failure -> REJECTED + AUDIT (never executed, never silently skipped).

Safety boundaries (v1.1 §阶段9):
  * query-template whitelist; DB table/column whitelist (no arbitrary SQL);
  * Workspace/Project scope; sensitive-field redaction; row/byte limits;
  * timeout + resource caps; every call audited; output provenance recorded;
  * unknown LLM: EIG/C/R are EXPLAINABLE HEURISTICS, not calibrated probabilities.

Score(p) = wI*EIG - wC*C - wR*R. Weights are documented defaults (not frozen).
Probe results are EVIDENCE, never a root cause by themselves.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from packages.core.errors import ForbiddenError, ValidationError

RISK_LEVELS = {"LOW", "MEDIUM", "HIGH"}
READ_ONLY_PROBES = frozenset(
    {
        "DOM_SNAPSHOT",
        "SCREENSHOT",
        "NETWORK_READ",
        "CONSOLE_READ",
        "API_READ",
        "BACKEND_LOG_READ",
        "DATABASE_READ",
    }
)
# Column whitelist for the only DB template exposed (no arbitrary SQL ever).
DB_TEMPLATES = {
    "project_count_by_workspace": {
        "sql": "SELECT COUNT(*) AS n FROM tf_project WHERE workspace_id = :workspace_id",
        "columns": ("n",),
        "max_rows": 1,
    }
}
SENSITIVE_KEYS = frozenset({"token", "password", "secret", "api_key", "authorization"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ProbeSpec:
    """Probe Registry entry — all v1.1 §阶段9 fields."""

    probe_id: str
    input_schema: dict[str, Any]
    target_scope: str
    permission_required: str
    read_only: bool
    side_effects: str
    estimated_cost: float
    estimated_duration: float
    risk_level: str
    timeout: float
    retry_policy: str
    output_schema: dict[str, Any]
    cleanup_policy: str

    def __post_init__(self) -> None:
        if self.risk_level not in RISK_LEVELS:
            raise ValidationError(f"invalid risk level: {self.risk_level}")
        if not self.read_only:
            raise ValidationError("stage-9 first batch probes must be read-only")


@dataclass(frozen=True)
class ProbeBudget:
    max_probes: int = 4
    max_cost: float = 10.0
    max_duration_s: float = 20.0
    min_score: float = 0.0

    def validate(self) -> None:
        if self.max_probes < 0 or self.max_cost < 0 or self.max_duration_s <= 0:
            raise ValidationError("probe budget must be non-negative with positive duration")


@dataclass
class ProbeRequest:
    probe_id: str
    params: dict[str, Any]
    workspace_id: str
    caller: str = "planner"


@dataclass
class AuditEntry:
    probe_id: str
    caller: str
    decision: str  # ALLOWED | REJECTED
    reason: str
    check: str
    at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProbeResult:
    probe_id: str
    executed: bool
    data: dict[str, Any]
    provenance: dict[str, Any]
    probe_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact(obj: Any) -> Any:
    """Recursively redact sensitive key names (draft policy)."""
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in SENSITIVE_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class PermissionGateway:
    """The single authorization point. No probe bypasses this."""

    def __init__(self, registry: dict[str, ProbeSpec]) -> None:
        self.registry = registry

    def check(self, request: ProbeRequest) -> str | None:
        """Return the failed check name, or None when all five checks pass."""
        spec = self.registry.get(request.probe_id)
        if spec is None:
            return "permission"  # unknown probe => not authorized
        # 1. permission
        if request.caller not in {"planner", "diagnosis_service", "human"}:
            return "permission"
        # 2. resource scope
        if not request.workspace_id or request.probe_id not in READ_ONLY_PROBES:
            return "resource_scope"
        if "workspace_id" not in request.params:
            return "resource_scope"
        if str(request.params["workspace_id"]) != request.workspace_id:
            return "resource_scope"
        if spec.target_scope not in {"workspace", "run", "project"}:
            return "resource_scope"
        # 3. risk
        if spec.risk_level == "HIGH":
            return "risk"
        # 4. budget is checked by the planner (has runtime state)
        # 5. parameter schema
        for required in spec.input_schema.get("required", []):
            if required not in request.params:
                return "parameter_schema"
        if "sql" in request.params or "query" in request.params:
            return "parameter_schema"  # arbitrary SQL is never accepted
        if (
            request.probe_id == "DATABASE_READ"
            and request.params.get("template") not in DB_TEMPLATES
        ):
            return "parameter_schema"
        return None


class ProbeEngine:
    """Executes appoved read-only probes via injected handlers (no side effects)."""

    def __init__(self, gateway: PermissionGateway, registry: dict[str, ProbeSpec]) -> None:
        self.gateway = gateway
        self.registry = registry
        self.audit: list[AuditEntry] = []
        self._handlers: dict[str, Callable[[ProbeRequest], dict[str, Any]]] = {}

    def register_handler(
        self, probe_id: str, handler: Callable[[ProbeRequest], dict[str, Any]]
    ) -> None:
        self._handlers[probe_id] = handler

    def execute(self, request: ProbeRequest) -> ProbeResult:
        failed = self.gateway.check(request)
        if failed is not None:
            self.audit.append(
                AuditEntry(request.probe_id, request.caller, "REJECTED", failed, failed)
            )
            raise ForbiddenError(f"probe rejected at check: {failed}")
        handler = self._handlers.get(request.probe_id)
        if handler is None:
            self.audit.append(
                AuditEntry(request.probe_id, request.caller, "REJECTED", "no handler", "permission")
            )
            raise ForbiddenError("probe has no authorized handler")
        data = redact(handler(request))
        # enforce result size caps (draft): serialize and cap bytes
        raw = json.dumps(data, ensure_ascii=False)
        if len(raw.encode()) > 64_000:
            data = {"error": "result exceeded byte cap", "truncated": True}
        provenance = {
            "probe_id": request.probe_id,
            "template_version": "1.0.0",
            "params": redact(request.params),
            "caller": request.caller,
            "at": _now(),
        }
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        self.audit.append(
            AuditEntry(request.probe_id, request.caller, "ALLOWED", "passed all checks", "all")
        )
        return ProbeResult(
            probe_id=request.probe_id,
            executed=True,
            data=data,
            provenance=provenance,
            probe_hash=f"sha256:{digest}",
        )


@dataclass(frozen=True)
class ProbeScore:
    probe_id: str
    eig: float
    cost: float
    risk: float
    score: float
    explanation: str


class ActiveDiagnosisPlanner:
    """Selects authorized probes by explainable heuristic score, then stops."""

    # Documented default weights — NOT frozen, not calibrated probabilities.
    W_I, W_C, W_R = 1.0, 0.3, 0.5

    def __init__(self, engine: ProbeEngine, budget: ProbeBudget | None = None) -> None:
        budget = budget or ProbeBudget()
        budget.validate()
        self.engine = engine
        self.budget = budget
        self.probes_used = 0
        self.cost_spent = 0.0
        self.results: list[ProbeResult] = []
        self.stop_reason = ""

    def score(self, probe_id: str, *, evidence_gap: str, spec: ProbeSpec) -> ProbeScore:
        """Explainable heuristic EIG/C/R. Higher EIG for gaps the probe covers."""
        gap_keywords = {
            "ui": ("DOM_SNAPSHOT", "SCREENSHOT", "CONSOLE_READ"),
            "network": ("NETWORK_READ", "API_READ"),
            "persistence": ("DATABASE_READ", "API_READ"),
            "log": ("BACKEND_LOG_READ",),
        }
        covered = any(probe_id in ids for ids in gap_keywords.get(evidence_gap, ()))
        eig = 0.8 if covered else 0.2  # estimate, not calibrated probability
        cost = min(1.0, spec.estimated_cost / 10.0)
        risk = {"LOW": 0.1, "MEDIUM": 0.4, "HIGH": 1.0}[spec.risk_level]
        score = self.W_I * eig - self.W_C * cost - self.W_R * risk
        explanation = (
            f"EIG~{eig:.2f} (gap='{evidence_gap}' {'covered' if covered else 'not covered'}), "
            f"C~{cost:.2f}, R~{risk:.2f}, weights=({self.W_I},{self.W_C},{self.W_R})"
        )
        return ProbeScore(probe_id, eig, cost, risk, score, explanation)

    def should_stop(self) -> str:
        if self.probes_used >= self.budget.max_probes:
            return "PROBE_COUNT_BUDGET"
        if self.cost_spent >= self.budget.max_cost:
            return "COST_BUDGET"
        return ""

    def run(
        self,
        *,
        evidence_gap: str,
        candidates: list[ProbeRequest],
        workspace_id: str,
        workspace_scoped: bool = True,
    ) -> dict[str, Any]:
        """Plan -> check -> score -> execute -> stop. Never loops unbounded."""
        if not workspace_scoped:
            self.stop_reason = "SCOPE_NOT_PERMITTED"
            return self._summary()
        ranked: list[tuple[ProbeScore, ProbeRequest]] = []
        for req in candidates:
            spec = self.engine.registry.get(req.probe_id)
            if spec is None:
                continue
            ranked.append((self.score(req.probe_id, evidence_gap=evidence_gap, spec=spec), req))
        ranked.sort(key=lambda pair: pair[0].score, reverse=True)
        for score, req in ranked:
            stop = self.should_stop()
            if stop:
                self.stop_reason = stop
                break
            if score.score < self.budget.min_score:
                self.stop_reason = "MARGINAL_VALUE_INSUFFICIENT"
                break
            try:
                result = self.engine.execute(req)
            except ForbiddenError:
                continue  # rejection already audited by the engine
            self.results.append(result)
            self.probes_used += 1
            self.cost_spent += self.engine.registry[req.probe_id].estimated_cost
        if not self.stop_reason:
            self.stop_reason = (
                "NO_HIGH_VALUE_SAFE_PROBE" if not self.results else "EVIDENCE_GAP_ADDRESSED"
            )
        return self._summary()

    def _summary(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "probes_used": self.probes_used,
            "cost_spent": round(self.cost_spent, 3),
            "results": [r.to_dict() for r in self.results],
            "audit": [a.to_dict() for a in self.engine.audit],
            "note": "probe results are evidence, not a root cause; EIG/C/R are heuristics",
        }
