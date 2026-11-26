"""Explorer REST surface: bounded, guarded exploration with real traces.

Scope and honesty boundary
--------------------------
This endpoint runs the **ScriptedPolicy** — a deterministic navigation script —
against the local TeamFlow target. It is *not* a language model and the response
says so explicitly (``is_llm: false``, ``policy_label``). The LLM path exists in
``packages.agents.llm_policy`` but stays BLOCKED here: no real model call is made
because no spend has been approved (D8).

Safety invariants are enforced by ``packages.agents.exploration.ToolGateway``,
not by this route: allowed domains, whitelisted tools, step/time budgets. This
module only validates the request shape and persists the resulting trace.

Persistence: one append-only JSON file per exploration under
``settings.exploration_data_dir`` (default ``data/explorations``). Mirrors the
benchmark store's approach; no database schema change is introduced.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel

from apps.teamflow import service as teamflow_service
from apps.teamflow.models import Project
from packages.agents.data_lifecycle import DataLeaseManager
from packages.agents.exploration import (
    Budget,
    ScriptedPolicy,
    ToolCall,
    origin_of,
    sanitize_action,
    verify_trace_record,
)
from packages.agents.explorer import Explorer
from packages.agents.goal_assertions import evaluate_assertions
from packages.agents.scenarios import Scenario, default_registry
from packages.agents.scripted_agent import AgentStep, ScriptedAgentPolicy
from packages.core import database
from packages.core.errors import ConflictError, NotFoundError, ValidationError
from packages.core.logging import get_logger
from packages.execution.dataref import REDACTED_MARKER

logger = get_logger(__name__)


router = APIRouter(prefix="/api/v1", tags=["explorer"])

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# The one implementation lives with the trace primitives, so the live capture
# path and this defence-in-depth pass can never drift apart.
REDACTED = REDACTED_MARKER


def redact_actions(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Mask sensitive values inside action args; return (actions, masked_count).

    Idempotent: re-sanitising an already-masked action keeps the marker and does
    not change the meaning of the trace.
    """
    masked = 0
    out: list[dict[str, Any]] = []
    for action in actions:
        clean, count = sanitize_action(action)
        out.append(clean)
        masked += count
    return out, masked


POLICIES: list[dict[str, Any]] = [
    {
        "id": "scripted",
        "label": "Scripted Policy",
        "kind": "deterministic",
        "is_llm": False,
        "verified": True,
        "agent_version": "scripted-policy-1.0.0-NOT-LLM",
        "note": "固定导航脚本；不消费观察结果，不调用任何模型，动作仍经安全网关校验。",
    },
    {
        "id": "scripted-agent",
        "label": "Scripted Agent (simulated decisions)",
        "kind": "scripted-agent",
        "is_llm": False,
        "verified": True,
        "agent_version": "scripted-agent-1.0.0-NOT-REAL-INFERENCE",
        "note": (
            "模拟决策环：Observation → LLMPolicy → ScriptedProvider → ToolGateway → 真实浏览器。"
            "决策为预编排 JSON（NOT_REAL_INFERENCE），每步必须与当前观察匹配。"
        ),
    },
    {
        "id": "llm",
        "label": "LLM Policy",
        "kind": "llm",
        "is_llm": True,
        "verified": False,
        "agent_version": "n/a",
        "note": "接入接口已保留；本轮未执行真实模型调用（费用未获批），状态 BLOCKED。",
    },
]

# Scenario registry (R5-5.2 §14). The Explorer core never branches on a
# scenario id: it loads a definition, validates it, and runs its declared steps.
# Unimplemented scenarios stay registered (with a reason) so the API can answer
# "what would this take?" instead of pretending the capability exists.
SCENARIOS = default_registry()
SUPPORTED_SCENARIOS: dict[str, str] = SCENARIOS.public_view()


def _substitute(value: Any, mapping: dict[str, str]) -> Any:
    """Fill ``{base_url}`` / ``{username}`` / ``{password}`` in a scenario step."""
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, mapping) for v in value]
    return value


def scenario_steps(
    scenario: Scenario,
    *,
    base_url: str,
    username: str,
    password: str,
    seed_values: dict[str, str] | None = None,
) -> list[AgentStep]:
    """Turn a scenario's declared decisions into provider steps.

    The password is substituted here (from trusted settings) and redacted as it
    enters the trace, exactly as before; a scenario file never contains a
    credential.
    """
    import json

    mapping = {
        "base_url": base_url,
        "username": username,
        "password": password,
        "wrong_password": "invalid-password-for-negative-path",
        "boundary_username": "U" * 120,
        # A scenario may create data; a per-run suffix keeps it re-runnable
        # instead of failing on the second attempt with a duplicate name.
        "unique_suffix": uuid.uuid4().hex[:8],
        **(seed_values or {}),
    }
    steps: list[AgentStep] = []
    for step in scenario.steps:
        args = _substitute(dict(step.get("args") or {}), mapping)
        decision = json.dumps(
            {
                "action": step["tool"],
                "args": args,
                "rationale": str(step.get("rationale") or ""),
            }
        )
        expect = dict(step.get("expect") or {})
        steps.append(
            AgentStep(
                decision=decision,
                expect_url_path=str(expect.get("url_path") or ""),
                expect_title_contains=str(expect.get("title_contains") or ""),
                expect_testid_visible=str(expect.get("testid_visible") or ""),
            )
        )
    return steps


def _prepare_scenario_data(
    request: Request, scenario: Scenario, run_id: str, token: str
) -> dict[str, str]:
    """Create only package-declared, per-run fixtures and record exact ownership."""
    setup = scenario.data_setup or {}
    unknown = set(setup) - {"projects"}
    if unknown:
        raise ValidationError(f"unsupported scenario data_setup keys: {sorted(unknown)}")
    projects = setup.get("projects", [])
    if not isinstance(projects, list):
        raise ValidationError("scenario data_setup.projects must be a list")
    session_factory = database.make_session_factory(request.app.state.engine)
    values: dict[str, str] = {}
    run_prefix = f"TG_RUN_{run_id}_"
    values["run_prefix"] = run_prefix
    values["owner_marker"] = f"traceguard-run:{run_id}"
    values["boundary_name"] = f"{run_prefix}{'B' * (200 - len(run_prefix))}"
    values["overlong_name"] = f"{run_prefix}{'B' * (201 - len(run_prefix))}"
    with session_factory() as session:
        for index, spec in enumerate(projects):
            if not isinstance(spec, dict) or set(spec) - {"name", "description", "key"}:
                raise ValidationError("scenario project seed has an invalid structure")
            key = str(spec.get("key") or f"project_{index}")
            seed_name = str(spec.get("name") or "").strip()
            if not seed_name:
                raise ValidationError("scenario project seed name must not be empty")
            owned_name = f"{run_prefix}{seed_name}"[:200]
            marker = f"traceguard-run:{run_id}"
            project = Project(
                id=uuid.uuid4().hex,
                workspace_id=teamflow_service.WS_ID,
                name=owned_name,
                description=f"{marker}; {str(spec.get('description') or '')}",
            )
            session.add(project)
            session.flush()
            request.app.state.data_lease_manager.add_resource(
                run_id,
                token,
                {
                    "kind": "teamflow-project",
                    "id": project.id,
                    "name": owned_name,
                    "marker": marker,
                },
            )
            session.commit()
            values[key] = owned_name
            values[f"{key}_id"] = project.id
    return values


def _cleanup_scenario_data(request: Request, run_id: str, token: str) -> None:
    """Delete only exact rows created and recorded by this run's lease."""
    app = request.app
    _cleanup_app_scenario_data(app, run_id, token)


def _cleanup_app_scenario_data(app: Any, run_id: str, token: str) -> None:
    """App-level form used by both the request finalizer and startup recovery."""
    manager: DataLeaseManager = app.state.data_lease_manager
    resources = manager.resources(run_id, token)
    session_factory = database.make_session_factory(app.state.engine)
    with session_factory() as session:
        for resource in resources:
            if resource.get("kind") != "teamflow-project":
                raise RuntimeError("unknown run-owned resource kind")
            project = session.get(Project, str(resource.get("id") or ""))
            if project is None:
                continue
            marker = str(resource.get("marker") or "")
            if not project.name.startswith(f"TG_RUN_{run_id}_") or not (
                project.description or ""
            ).startswith(marker):
                raise RuntimeError("run-owned project identity changed; refusing cleanup")
            session.delete(project)
        session.commit()


def recover_expired_scenario_data(app: Any) -> None:
    """Recover data owned by runs whose process stopped heartbeating."""
    manager: DataLeaseManager = app.state.data_lease_manager
    for run_id in manager.expired_runs():
        token = ""
        try:
            lease = manager.lease(run_id)
            token = str(lease["token"])
            _cleanup_app_scenario_data(app, run_id, token)
            manager.cleanup(run_id, token)
        except Exception as exc:  # noqa: BLE001 - preserve failed cleanup for inspection
            if token:
                manager.mark_cleanup_failed(run_id, token, type(exc).__name__)


def build_policy(
    policy_id: str,
    settings: Any,
    base_url: str,
    scenario: Scenario,
    seed_values: dict[str, str] | None = None,
) -> tuple[Any, str]:
    """Build the named policy. Returns (policy, agent_version).

    ``scripted``      — fixed ToolCall script, decisions never read the page.
    ``scripted-agent``— Observation→LLMPolicy→ScriptedProvider loop; every
    decision is a structured JSON answer guarded by an expectation on the
    CURRENT page, so a drifted page stops the loop loudly.

    Both take their actions from the scenario, not from a login-only constant.
    """
    if policy_id == "scripted":
        script = tuple(
            ToolCall(
                str(step["tool"]),
                _substitute(
                    dict(step.get("args") or {}),
                    {
                        "base_url": base_url,
                        "username": str(settings.teamflow_test_user),
                        "password": str(settings.teamflow_test_password),
                        "wrong_password": "invalid-password-for-negative-path",
                        "boundary_username": "U" * 120,
                        "unique_suffix": uuid.uuid4().hex[:8],
                        **(seed_values or {}),
                    },
                ),
                str(step.get("rationale") or ""),
            )
            for step in scenario.steps
        )
        return ScriptedPolicy(script), "scripted-policy-1.0.0-NOT-LLM"
    if policy_id == "scripted-agent":
        steps = scenario_steps(
            scenario,
            base_url=base_url,
            username=str(settings.teamflow_test_user),
            password=str(settings.teamflow_test_password),
            seed_values=seed_values,
        )
        return (
            ScriptedAgentPolicy(steps, goal=scenario.description),
            "scripted-agent-1.0.0-NOT-REAL-INFERENCE",
        )
    raise ConflictError(
        "policy is not available in this build",
        details={
            "policy": policy_id,
            "available": ["scripted", "scripted-agent"],
            "llm_reason": "真实 LLM 调用未获批（D8）。",
        },
    )


# ----------------------------------------------------------------- storage


class ExplorationStore:
    """Tiny append-only JSON store, one file per exploration."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        # Files that exist but cannot be parsed. Kept visible instead of being
        # silently skipped: a corrupt trace must not look like "no traces".
        self.corrupt_files: list[str] = []

    def _dir(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def save(self, record: dict[str, Any]) -> None:
        eid = str(record["exploration_id"])
        if not re.fullmatch(r"exp_[A-Za-z0-9_]+", eid):
            raise ValidationError(f"invalid exploration id: {eid}")
        path = self._dir() / f"{eid}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, exploration_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"exp_[A-Za-z0-9_]+", exploration_id):
            return None
        path = self.root / f"{exploration_id}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in self.root.glob("exp_*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("exploration_record_corrupt: %s (%s)", path, exc)
                if str(path) not in self.corrupt_files:
                    self.corrupt_files.append(str(path))
                continue  # one corrupt file must not blank the whole list
        out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return out


def _login_script(settings: Any, base_url: str) -> tuple[ToolCall, ...]:
    """The approved TeamFlow login path, with credentials from config.

    The password is read from settings at call time and redacted the moment the
    action enters the trace, so neither source code nor the trace holds it.
    """
    return (
        ToolCall("navigate", {"url": base_url}, "open login page"),
        ToolCall(
            "fill",
            {"selector": "[data-testid=username]", "value": str(settings.teamflow_test_user)},
            "fill username",
        ),
        ToolCall(
            "fill",
            {
                "selector": "[data-testid=password]",
                "value": str(settings.teamflow_test_password),
            },
            "fill password",
        ),
        ToolCall("click", {"selector": "[data-testid=login-submit]"}, "submit login"),
        ToolCall("snapshot", {}, "observe the page reached after submit"),
    )


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    """List projection: enough for a table, without the full action sequence."""
    return {
        "exploration_id": record.get("exploration_id"),
        "test_intent_ref": record.get("test_intent_ref") or "",
        "goal": (record.get("test_intent") or {}).get("goal", ""),
        "target": record.get("target") or "",
        "scenario_version": record.get("scenario_version") or "",
        "policy": record.get("policy") or "scripted",
        "policy_label": record.get("policy_label") or "",
        "is_llm": bool(record.get("is_llm")),
        # execution status of the exploration process: COMPLETED/FAILED/CANCELLED
        "status": record.get("status") or "UNKNOWN",
        "stop_reason": record.get("stop_reason") or "",
        # business-goal verdict, deliberately separate from status/stop_reason
        "goal_status": record.get("goal_status") or "",
        "goal_reason": record.get("goal_reason") or "",
        "step_count": record.get("step_count", 0),
        "observation_count": len(record.get("observations") or []),
        "started_at": record.get("started_at") or "",
        "finished_at": record.get("finished_at") or "",
        "source_trace_hash": record.get("source_trace_hash") or "",
        "hash_version": record.get("hash_version"),
        "trace_integrity_verified": record.get("trace_integrity_verified"),
        "security_event_count": len(record.get("security_events") or []),
        "error": record.get("error"),
    }


# ------------------------------------------------------------------ models


class RunExplorationRequest(BaseModel):
    intent_ref: str = ""
    # R5-4.6: the FULL TestIntent travels with the request, is persisted with
    # the trace, covered by the trace hash, and is the compiler's input.
    goal: str = ""
    expected_outcomes: list[str] = []
    target: str = ""
    required_assertions: list[str] = []
    constraints: list[str] = []
    # Declared business scenario (§3.1.1). Only registered scenarios run; an
    # unknown one gets UNSUPPORTED_SCENARIO instead of a silent login replay.
    scenario: str = "teamflow-login"
    scenario_version: str | None = None
    path: str = "normal"
    base_url: str = "http://127.0.0.1:8001"
    policy: str = "scripted"
    max_steps: int = 8
    max_duration_s: float = 45.0


# ------------------------------------------------------------------ routes


@router.get("/explorations/policies")
def list_policies() -> dict[str, Any]:
    """What the Explorer can actually do right now — stated honestly."""
    return {
        "policies": POLICIES,
        "active_policy": "scripted",
        "llm_blocked_reason": "真实模型调用未获批（D8）；本轮仅验证受控调用链。",
        "supported_scenarios": SUPPORTED_SCENARIOS,
    }


@router.get("/explorations")
def list_explorations(request: Request) -> list[dict[str, Any]]:
    store: ExplorationStore = request.app.state.exploration_store
    return [_summary(r) for r in store.list()]


@router.get("/explorations/{exploration_id}")
def get_exploration(request: Request, exploration_id: str) -> dict[str, Any]:
    store: ExplorationStore = request.app.state.exploration_store
    record = store.get(exploration_id)
    if record is None:
        raise NotFoundError(f"exploration not found: {exploration_id}")
    # Defence in depth: traces written before redaction existed are masked here
    # too, so no reader can surface a credential from an old file.
    record["action_sequence"], masked = redact_actions(record.get("action_sequence", []))
    record["redacted_fields"] = masked
    # Integrity is re-checked on every read: a trace whose hash no longer
    # matches its own content is reported, not silently served as evidence.
    record["trace_integrity_verified"] = verify_trace_record(record)
    # Assertions are derived (intent + observations, both hashed), so they are
    # re-derived on every read instead of being taken on trust.
    scenario_id = str(record.get("scenario_id") or record.get("scenario") or "")
    scenario_version = record.get("scenario_version")
    scenario_for_record = SCENARIOS.get(
        scenario_id, str(scenario_version) if scenario_version else None
    )
    if record.get("digest_algorithm") and (
        record.get("digest_algorithm") != "sha256"
        or record.get("digest_algorithm_version") != "canonical-json-v1"
    ):
        raise ValidationError(
            "unsupported scenario digest algorithm",
            details={"code": "unsupported-digest-algorithm"},
        )
    if record.get("scenario_digest"):
        record["scenario_digest_verified"] = bool(
            scenario_for_record and scenario_for_record.digest == record["scenario_digest"]
        )
    if scenario_for_record and scenario_for_record.goal_definition:
        recomputed = evaluate_assertions(
            list(scenario_for_record.goal_definition),
            dict((record.get("observations") or [{}])[-1] or {}),
        )
        record["goal_assertions_recomputed"] = recomputed.to_dict()
        record["goal_assertions_verified"] = recomputed.to_dict() == record.get("goal_assertions")
    return record


@router.post("/explorations", status_code=201)
def run_exploration(request: Request, body: RunExplorationRequest) -> dict[str, Any]:
    """Run one bounded exploration and persist its trace.

    Declared as a sync ``def`` on purpose: the Playwright sync API cannot run
    inside the event loop, and FastAPI executes sync handlers in a threadpool.
    """
    if body.policy == "llm":
        raise ConflictError(
            "LLM policy is not available in this build",
            details={
                "policy": body.policy,
                "reason": "真实 LLM 调用未获批（D8）；请使用 scripted 或 scripted-agent 策略。",
                "available": ["scripted", "scripted-agent"],
            },
        )
    scenario = SCENARIOS.get(body.scenario, body.scenario_version)
    if scenario is None or not scenario.implemented:
        # §3.1.1: unsupported goals must be REJECTED, never silently executed
        # as some other scenario's script while claiming to satisfy the user's
        # business goal. A registered-but-unimplemented scenario says why.
        raise ValidationError(
            f"UNSUPPORTED_SCENARIO: scenario {body.scenario!r} is not implemented; "
            "the Explorer will not replay another scenario's script as a stand-in",
            details={
                "code": "UNSUPPORTED_SCENARIO",
                "requested_scenario": body.scenario,
                "supported_scenarios": {
                    s.scenario_id: s.description for s in SCENARIOS.implemented()
                },
                "blocked_reason": scenario.blocked_reason if scenario else "",
            },
        )

    path_steps = scenario.steps_for_path(body.path)
    if path_steps is None:
        raise ValidationError(
            f"scenario path {body.path!r} is not registered",
            details={"scenario_id": scenario.scenario_id, "scenario_version": scenario.version},
        )
    execution_scenario = replace(scenario, steps=path_steps)

    parsed = urlsplit(body.base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS:
        raise ValidationError(
            "exploration target must be a local URL",
            details={"base_url": body.base_url, "allowed_hosts": sorted(_LOCAL_HOSTS)},
        )
    if not 1 <= body.max_steps <= 30:
        raise ValidationError("max_steps must be between 1 and 30")

    title = next((p["label"] for p in POLICIES if p["id"] == body.policy), body.policy)
    started = datetime.now(UTC).isoformat()

    explorer = Explorer(
        sensitive_selectors=scenario.sensitive_selectors,
        budget=Budget(
            max_steps=body.max_steps,
            max_tool_calls=body.max_steps * 2 + 4,
            max_duration_s=body.max_duration_s,
            allowed_domains=tuple(_LOCAL_HOSTS),
            # The exact origin, not just the hostname: scheme and port are part
            # of the permission (R5-4.3 §5.3).
            allowed_origins=(origin_of(body.base_url),),
        ),
        application_version=f"teamflow-0.1.0 ({body.scenario})",
    )
    lease_manager: DataLeaseManager = request.app.state.data_lease_manager
    run_id = explorer.trace.exploration_id
    lease_token = lease_manager.setup(run_id, scenario.data_setup or {})
    lease_manager.start_heartbeat(run_id, lease_token)
    try:
        seed_values = _prepare_scenario_data(request, scenario, run_id, lease_token)
        policy, agent_version = build_policy(
            body.policy,
            request.app.state.settings,
            body.base_url,
            execution_scenario,
            seed_values=seed_values,
        )
        explorer.policy = policy
        explorer.trace.agent_version = agent_version
        explorer.trace.prompt_version = "none-scripted" if body.policy == "scripted" else "external"
    except Exception:
        lease_manager.stop_heartbeat(run_id)
        try:
            _cleanup_scenario_data(request, run_id, lease_token)
        finally:
            lease_manager.cleanup(run_id, lease_token)
        raise
    # The TestIntent is assembled ONCE here, embedded into the trace before
    # finalize (so the v2 hash covers it), and is the exact input the compiler
    # and the reviewer see later.
    test_intent: dict[str, Any] = {
        "intent_id": body.intent_ref or f"i-{explorer.trace.exploration_id}",
        "description": body.goal or body.intent_ref or scenario.description,
        "source_ref": body.intent_ref,
        "scenario": body.scenario,
        # R5-4.6 semantics preserved: the scenario's intent *template* is a
        # suggestion the caller can read from /explorations/policies and send
        # explicitly. It must NOT be injected here, or a goal-less request would
        # silently acquire a goal and "NOT_EVALUATED" could never happen again.
        "goal": body.goal,
        "expected_outcomes": [str(o) for o in body.expected_outcomes if str(o).strip()],
        # No silent fallback to base_url: an undeclared target stays undeclared,
        # so a goal-less exploration evaluates to NOT_EVALUATED instead of being
        # "verified" against the entry page it started on. A scenario may
        # *declare* a target; that is registered data, not an inference.
        "target": str(body.target or "").strip(),
        "required_assertions": [str(a) for a in body.required_assertions if str(a).strip()],
        "constraints": [
            str(c) for c in (body.constraints or scenario.constraints) if str(c).strip()
        ],
    }

    status = "COMPLETED"
    error: str | None = None
    try:
        trace = explorer.explore(body.intent_ref, body.base_url, intent=test_intent)
        payload = trace.to_dict()
    except Exception as exc:  # noqa: BLE001 - surface target/execution failure honestly
        status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
        # Keep whatever the loop captured before it broke.
        explorer.trace.stop_reason = explorer.trace.stop_reason or "ERROR"
        explorer.trace.finalize()
        payload = explorer.trace.to_dict()

    # R5-5.2 §15: structured goal assertions, evaluated against the LAST
    # observed page only. Every assertion gets its own verdict + evidence, and an
    # unobservable assertion is INCONCLUSIVE rather than a silent failure.
    final_observation = dict((payload.get("observations") or [{}])[-1] or {})
    assertion_report = evaluate_assertions(list(scenario.goal_definition), final_observation)
    payload["goal_assertions"] = assertion_report.to_dict()
    if assertion_report.verdict != "VERIFIED" and payload.get("goal_status") == "VERIFIED":
        # Tighten, never loosen: a goal cannot stay VERIFIED on the strength of a
        # text match when a declared assertion failed or could not be observed.
        payload["goal_status"] = (
            "NOT_VERIFIED" if assertion_report.verdict == "NOT_VERIFIED" else "INCONCLUSIVE"
        )
        payload["goal_reason"] = (
            f"{payload.get('goal_reason') or ''} | scenario assertions: {assertion_report.reason}"
        ).strip(" |")

    record: dict[str, Any] = {
        **payload,
        "policy": body.policy,
        "policy_label": title,
        "is_llm": False,
        "agent_version": payload.get("agent_version") or agent_version,
        "target": body.base_url,
        "scenario_id": scenario.scenario_id,
        "scenario_version": scenario.version,
        "scenario_path": body.path,
        "scenario_digest": scenario.digest,
        "digest_algorithm": "sha256",
        "digest_algorithm_version": "canonical-json-v1",
        "test_intent": payload.get("test_intent") or test_intent,
        "status": status,
        "error": error,
        "step_count": len([a for a in payload.get("action_sequence", []) if not a.get("rejected")]),
        "requested_at": started,
    }
    # Redact before saving: a persisted trace must never hold a plaintext
    # password, and every reader (get/list/UI) then gets the safe version.
    record["action_sequence"], masked = redact_actions(record.get("action_sequence", []))
    record["redacted_fields"] = masked
    lease_manager.stop_heartbeat(run_id)
    cleanup_error = ""
    try:
        _cleanup_scenario_data(request, run_id, lease_token)
        cleanup_ok = lease_manager.cleanup(run_id, lease_token)
    except Exception as exc:  # noqa: BLE001 - cleanup failure must be retained
        cleanup_ok = False
        cleanup_error = type(exc).__name__
        lease_manager.mark_cleanup_failed(run_id, lease_token, cleanup_error)
    record["data_lifecycle"] = {
        "run_id": run_id,
        "state": "CLEANED" if cleanup_ok else "CLEANUP_FAILED",
        "cleanup_error": cleanup_error,
    }
    store: ExplorationStore = request.app.state.exploration_store
    store.save(record)
    # Verify against what the store actually holds (post JSON round-trip): the
    # hash must describe the persisted bytes, not the object in memory.
    reloaded = store.get(str(record["exploration_id"])) or record
    verified = verify_trace_record(reloaded)
    if not verified:
        logger.error("trace_integrity_mismatch: %s", record.get("exploration_id"))
    record["trace_integrity_verified"] = verified
    return record


def register_explorer(app: Any, *, exploration_dir: Path) -> None:
    app.state.exploration_store = ExplorationStore(exploration_dir)
    settings = app.state.settings
    app.state.data_lease_manager = DataLeaseManager(
        exploration_dir / "_leases",
        heartbeat_seconds=settings.data_lease_heartbeat_seconds,
        timeout_seconds=settings.data_lease_timeout_seconds,
    )
    app.include_router(router)
