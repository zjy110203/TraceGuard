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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel

from packages.agents.exploration import Budget, ScriptedPolicy
from packages.agents.explorer import Explorer
from packages.core.errors import ConflictError, NotFoundError, ValidationError

router = APIRouter(prefix="/api/v1", tags=["explorer"])

_LOCAL_HOSTS = {"127.0.0.1", "localhost"}

# Never persist or display credential-shaped argument values. The trace is
# evidence, not a credential store; replay does not need the plaintext.
_SENSITIVE_ARG = re.compile(r"pass(word)?|secret|token|api[-_]?key|credential", re.IGNORECASE)
REDACTED = "«redacted»"


def redact_actions(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Mask sensitive values inside action args; return (actions, masked_count).

    Two detection paths, because a credential can hide in either place:
      * the arg *key* looks sensitive (``token``, ``password``, ``api_key``);
      * the arg is a plain ``value`` whose *context* (selector/target) names a
        password field — ``[data-testid=password]`` filled with ``hunter2``.
    The trace keeps its shape (tool, selector) so it stays useful evidence.
    """
    masked = 0
    out: list[dict[str, Any]] = []
    for action in actions:
        args = action.get("args")
        if not isinstance(args, dict):
            out.append(action)
            continue
        context = " ".join(
            str(v) for k, v in args.items() if k != "value" and isinstance(v, (str, int, float))
        )
        context_sensitive = bool(_SENSITIVE_ARG.search(context))
        clean_args: dict[str, Any] = {}
        for key, value in args.items():
            key_sensitive = bool(_SENSITIVE_ARG.search(str(key)))
            value_sensitive = key == "value" and context_sensitive
            if (key_sensitive or value_sensitive) and value not in (None, ""):
                clean_args[key] = REDACTED
                masked += 1
            else:
                clean_args[key] = value
        out.append({**action, "args": clean_args})
    return out, masked


POLICIES: list[dict[str, Any]] = [
    {
        "id": "scripted",
        "label": "Scripted Policy",
        "kind": "deterministic",
        "is_llm": False,
        "verified": True,
        "agent_version": "scripted-policy-1.0.0-NOT-LLM",
        "note": "确定性导航脚本；不调用任何模型，动作仍经安全网关校验。",
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


# ----------------------------------------------------------------- storage


class ExplorationStore:
    """Tiny append-only JSON store, one file per exploration."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

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
            except (json.JSONDecodeError, OSError):
                continue  # a corrupt file must not blank the whole list
        out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return out


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    """List projection: enough for a table, without the full action sequence."""
    return {
        "exploration_id": record.get("exploration_id"),
        "test_intent_ref": record.get("test_intent_ref") or "",
        "target": record.get("target") or "",
        "policy": record.get("policy") or "scripted",
        "policy_label": record.get("policy_label") or "",
        "is_llm": bool(record.get("is_llm")),
        "status": record.get("status") or "UNKNOWN",
        "stop_reason": record.get("stop_reason") or "",
        "step_count": record.get("step_count", 0),
        "started_at": record.get("started_at") or "",
        "finished_at": record.get("finished_at") or "",
        "source_trace_hash": record.get("source_trace_hash") or "",
        "error": record.get("error"),
    }


# ------------------------------------------------------------------ models


class RunExplorationRequest(BaseModel):
    intent_ref: str = ""
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
    return record


@router.post("/explorations", status_code=201)
def run_exploration(request: Request, body: RunExplorationRequest) -> dict[str, Any]:
    """Run one bounded exploration and persist its trace.

    Declared as a sync ``def`` on purpose: the Playwright sync API cannot run
    inside the event loop, and FastAPI executes sync handlers in a threadpool.
    """
    if body.policy != "scripted":
        raise ConflictError(
            "LLM policy is not available in this build",
            details={
                "policy": body.policy,
                "reason": "真实 LLM 调用未获批（D8）；请使用 scripted 策略。",
                "available": ["scripted"],
            },
        )

    parsed = urlsplit(body.base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _LOCAL_HOSTS:
        raise ValidationError(
            "exploration target must be a local URL",
            details={"base_url": body.base_url, "allowed_hosts": sorted(_LOCAL_HOSTS)},
        )
    if not 1 <= body.max_steps <= 30:
        raise ValidationError("max_steps must be between 1 and 30")

    title = next((p["label"] for p in POLICIES if p["id"] == "scripted"), "Scripted Policy")
    started = datetime.now(UTC).isoformat()

    explorer = Explorer(
        policy=ScriptedPolicy(),
        budget=Budget(
            max_steps=body.max_steps,
            max_tool_calls=body.max_steps * 2 + 4,
            max_duration_s=body.max_duration_s,
            allowed_domains=tuple(_LOCAL_HOSTS),
        ),
    )

    status = "COMPLETED"
    error: str | None = None
    try:
        trace = explorer.explore(body.intent_ref, body.base_url)
        payload = trace.to_dict()
    except Exception as exc:  # noqa: BLE001 - surface target/execution failure honestly
        status = "FAILED"
        error = f"{type(exc).__name__}: {exc}"
        # Keep whatever the loop captured before it broke.
        explorer.trace.stop_reason = explorer.trace.stop_reason or "ERROR"
        explorer.trace.finalize()
        payload = explorer.trace.to_dict()

    record: dict[str, Any] = {
        **payload,
        "policy": "scripted",
        "policy_label": title,
        "is_llm": False,
        "target": body.base_url,
        "status": status,
        "error": error,
        "step_count": len([a for a in payload.get("action_sequence", []) if not a.get("rejected")]),
        "requested_at": started,
    }
    # Redact before saving: a persisted trace must never hold a plaintext
    # password, and every reader (get/list/UI) then gets the safe version.
    record["action_sequence"], masked = redact_actions(record.get("action_sequence", []))
    record["redacted_fields"] = masked
    store: ExplorationStore = request.app.state.exploration_store
    store.save(record)
    return record


def register_explorer(app: Any, *, exploration_dir: Path) -> None:
    app.state.exploration_store = ExplorationStore(exploration_dir)
    app.include_router(router)
