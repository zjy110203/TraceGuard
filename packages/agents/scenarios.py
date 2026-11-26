"""Registrable business scenarios for the Explorer (R5-5.2 §14).

The rule this module exists to enforce: **no scenario's business logic lives in
the Explorer core**. A scenario declares what it is allowed to do, what success
means, and which decisions the (currently scripted) policy will take. The core
loads and validates a scenario; it never branches on a scenario id.

Every definition is data, not code:

============  ===============================================================
field         meaning
============  ===============================================================
scenario_id   stable id used by the API and the trace
name/desc     human-readable, shown in the UI
allowed_origins  origins the Explorer may touch (scheme+host+port)
initial_url   path (or absolute URL) the exploration starts from
intent_templates  TestIntent fields the scenario fills in
goal_definition   structured assertions (see goal_assertions.py)
allowed_actions   tool names the policy may emit
constraints   hard rules recorded in the trace
success_conditions / stop_conditions  human-readable contract
timeout_budget_s / max_steps  budgets
evidence_requirements  what must be captured for the trace to be citable
implemented   False + blocked_reason when the SUT cannot support it yet
============  ===============================================================

A scenario whose ``implemented`` is False is still registered so the API can
answer "what would this take?" instead of pretending the capability exists.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any

from packages.agents.goal_assertions import expand_assertion_sugar, validate_assertion

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
REQUIRED_ACTIONS = ("navigate", "snapshot")
# Tools the Explorer's gateway knows. A scenario may only narrow this set.
KNOWN_ACTIONS: tuple[str, ...] = (
    "navigate",
    "fill",
    "click",
    "snapshot",
    "assert_text",
    "wait",
)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    description: str
    allowed_origins: tuple[str, ...]
    initial_url: str
    intent_templates: dict[str, Any]
    goal_definition: tuple[dict[str, Any], ...]
    allowed_actions: tuple[str, ...]
    constraints: tuple[str, ...]
    success_conditions: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    timeout_budget_s: float
    evidence_requirements: tuple[str, ...]
    version: str = "1.0.0"
    steps: tuple[dict[str, Any], ...] = ()
    max_steps: int = 8
    implemented: bool = True
    blocked_reason: str = ""
    preconditions: tuple[dict[str, Any], ...] = ()
    data_setup: dict[str, Any] | None = None
    cleanup: dict[str, Any] | None = None
    denied_actions: tuple[str, ...] = ()
    sensitive_selectors: tuple[str, ...] = ()
    paths: dict[str, tuple[dict[str, Any], ...]] | None = None

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> Scenario:
        """Strictly load a legacy or v2 package without accepting extensions silently."""
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown scenario fields: {sorted(unknown)}")
        normalized = dict(value)
        for key in (
            "allowed_origins",
            "goal_definition",
            "allowed_actions",
            "constraints",
            "success_conditions",
            "stop_conditions",
            "evidence_requirements",
            "steps",
            "preconditions",
            "denied_actions",
            "sensitive_selectors",
        ):
            if key in normalized:
                normalized[key] = tuple(normalized[key])
        if isinstance(normalized.get("paths"), dict):
            normalized["paths"] = {key: tuple(items) for key, items in normalized["paths"].items()}
        return cls(**normalized)

    def canonical_payload(self) -> dict[str, Any]:
        """Return the stable, credential-free Scenario Package v2 payload."""
        return {
            "scenario_id": self.scenario_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "allowed_origins": list(self.allowed_origins),
            "initial_url": self.initial_url,
            "intent_templates": self.intent_templates,
            "goal_definition": list(self.goal_definition),
            "allowed_actions": list(self.allowed_actions),
            "constraints": list(self.constraints),
            "success_conditions": list(self.success_conditions),
            "stop_conditions": list(self.stop_conditions),
            "timeout_budget_s": self.timeout_budget_s,
            "evidence_requirements": list(self.evidence_requirements),
            "steps": list(self.steps),
            "max_steps": self.max_steps,
            "implemented": self.implemented,
            "blocked_reason": self.blocked_reason,
            "preconditions": list(self.preconditions),
            "data_setup": self.data_setup or {},
            "cleanup": self.cleanup or {},
            "denied_actions": list(self.denied_actions),
            "sensitive_selectors": list(self.sensitive_selectors),
            "paths": {k: list(v) for k, v in (self.paths or {}).items()},
        }

    def steps_for_path(self, path_kind: str) -> tuple[dict[str, Any], ...] | None:
        if path_kind == "normal" and not (self.paths or {}).get(path_kind):
            return self.steps
        return (self.paths or {}).get(path_kind)

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "allowed_origins": list(self.allowed_origins),
            "initial_url": self.initial_url,
            "intent_templates": dict(self.intent_templates),
            "goal_definition": [dict(a) for a in self.goal_definition],
            "allowed_actions": list(self.allowed_actions),
            "constraints": list(self.constraints),
            "success_conditions": list(self.success_conditions),
            "stop_conditions": list(self.stop_conditions),
            "timeout_budget_s": self.timeout_budget_s,
            "evidence_requirements": list(self.evidence_requirements),
            "max_steps": self.max_steps,
            "implemented": self.implemented,
            "blocked_reason": self.blocked_reason,
            "step_count": len(self.steps),
            "digest_algorithm": "sha256",
            "digest_algorithm_version": "canonical-json-v1",
            "digest": self.digest,
        }

    def validate(self) -> list[str]:
        """Structural contract; an unusable scenario must fail loudly on load."""
        problems: list[str] = []
        if not ID_PATTERN.match(self.scenario_id):
            problems.append(f"scenario_id {self.scenario_id!r} is not a valid slug")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version):
            problems.append("version must use numeric semantic version MAJOR.MINOR.PATCH")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version):
            problems.append("version must use numeric semantic version MAJOR.MINOR.PATCH")
        for label, value in (
            ("name", self.name),
            ("description", self.description),
            ("initial_url", self.initial_url),
        ):
            if not str(value).strip():
                problems.append(f"{label} must be non-empty")
        if not self.allowed_origins:
            problems.append("allowed_origins must not be empty")
        for origin in self.allowed_origins:
            if not re.match(r"^https?://[^/]+$", origin):
                problems.append(f"allowed_origins entry {origin!r} must be scheme://host[:port]")
        if not self.intent_templates:
            problems.append("intent_templates must declare at least one intent field")
        if not self.goal_definition:
            problems.append("goal_definition must declare at least one assertion")
        for index, spec in enumerate(self.goal_definition):
            for issue in validate_assertion(dict(spec)):
                problems.append(f"goal_definition[{index}]: {issue}")
        if not self.allowed_actions:
            problems.append("allowed_actions must not be empty")
        unknown = [a for a in self.allowed_actions if a not in KNOWN_ACTIONS]
        if unknown:
            problems.append(f"allowed_actions contain unknown tools: {unknown}")
        for action in REQUIRED_ACTIONS:
            if action not in self.allowed_actions:
                problems.append(f"allowed_actions must include {action!r}")
        if not self.constraints:
            problems.append("constraints must not be empty (a scenario without limits is unsafe)")
        if not self.success_conditions or not self.stop_conditions:
            problems.append("success_conditions and stop_conditions must both be declared")
        if self.timeout_budget_s <= 0:
            problems.append("timeout_budget_s must be > 0")
        if not self.evidence_requirements:
            problems.append("evidence_requirements must not be empty")
        if self.max_steps < 1:
            problems.append("max_steps must be >= 1")
        if any(not isinstance(item, dict) for item in self.preconditions):
            problems.append("preconditions must be structured objects")
        for selector in self.sensitive_selectors:
            if len(selector) > 200 or not re.fullmatch(
                r"[A-Za-z0-9\s\[\]=\"'#._:\->~*+,()]+", selector
            ):
                problems.append("sensitive_selectors contains an unsafe selector")
        if not isinstance(self.data_setup or {}, dict) or not isinstance(self.cleanup or {}, dict):
            problems.append("data_setup and cleanup must be structured objects")
        setup = self.data_setup or {}
        if isinstance(setup, dict):
            if set(setup) - {"projects"}:
                problems.append("data_setup contains unknown fields")
            if "projects" in setup and not isinstance(setup["projects"], list):
                problems.append("data_setup.projects must be a list")
            for index, project in enumerate(setup.get("projects", [])):
                if not isinstance(project, dict) or set(project) - {
                    "key",
                    "name",
                    "description",
                }:
                    problems.append(f"data_setup.projects[{index}] has an invalid structure")
        cleanup = self.cleanup or {}
        if isinstance(cleanup, dict) and (
            set(cleanup) - {"owned_resources"}
            or cleanup.get("owned_resources", "lease-only") != "lease-only"
        ):
            problems.append("cleanup must use owned_resources='lease-only'")
        if set(self.denied_actions) - set(KNOWN_ACTIONS):
            problems.append("denied_actions contain unknown tools")
        conflicts = set(self.denied_actions) & set(self.allowed_actions)
        if conflicts:
            problems.append(f"denied_actions conflict with allowed_actions: {sorted(conflicts)}")
        valid_path_kinds = {"normal", "exception", "boundary", "recovery"}
        if self.paths is not None:
            paths = self.paths if isinstance(self.paths, dict) else {}
            if not isinstance(self.paths, dict):
                problems.append("paths must be an object")
            unknown_paths = set(paths) - valid_path_kinds
            if unknown_paths:
                problems.append(f"paths contain unknown path kinds: {sorted(unknown_paths)}")
            for path_kind, path in paths.items():
                if not isinstance(path, (tuple, list)) or any(
                    not isinstance(step, dict) for step in path
                ):
                    problems.append(f"paths[{path_kind!r}] must be a sequence of step objects")
                    continue
                for step_index, step in enumerate(path):
                    if set(step) - {"tool", "args", "rationale", "expect"}:
                        problems.append(f"paths[{path_kind!r}][{step_index}] has unknown fields")
                    if not isinstance(step.get("args", {}), dict):
                        problems.append(
                            f"paths[{path_kind!r}][{step_index}].args must be an object"
                        )
                    if str(step.get("tool") or "") not in self.allowed_actions:
                        problems.append(
                            f"paths[{path_kind!r}][{step_index}] uses a tool "
                            "outside allowed_actions"
                        )
                if len(path) > self.max_steps:
                    problems.append(f"paths[{path_kind!r}] exceeds max_steps")
        if self.implemented and not self.steps:
            problems.append("an implemented scenario must declare its decision steps")
        for index, step in enumerate(self.steps):
            if set(step) - {"tool", "args", "rationale", "expect"}:
                problems.append(f"steps[{index}] has unknown fields")
            if not isinstance(step.get("args", {}), dict):
                problems.append(f"steps[{index}].args must be an object")
            tool = str(step.get("tool") or "")
            if tool not in self.allowed_actions:
                problems.append(f"steps[{index}] uses {tool!r} outside allowed_actions")
        return problems


class ScenarioRegistry:
    """Register / look up scenarios. Duplicates are a load error, not a replace."""

    def __init__(self) -> None:
        self._scenarios: dict[tuple[str, str], Scenario] = {}

    def register(self, scenario: Scenario) -> Scenario:
        normalized_goal = tuple(
            expand_assertion_sugar(dict(spec)) for spec in scenario.goal_definition
        )
        if normalized_goal != scenario.goal_definition:
            scenario = replace(scenario, goal_definition=normalized_goal)
        problems = scenario.validate()
        if problems:
            raise ValueError(f"scenario {scenario.scenario_id!r} is invalid: {problems}")
        key = (scenario.scenario_id, scenario.version)
        if key in self._scenarios:
            raise ValueError(f"scenario {key!r} is already registered")
        self._scenarios[key] = scenario
        return scenario

    def get(self, scenario_id: str, version: str | None = None) -> Scenario | None:
        if version is not None:
            return self._scenarios.get((scenario_id, version))
        matches = [s for (sid, _), s in self._scenarios.items() if sid == scenario_id]
        return max(matches, key=lambda s: tuple(int(x) for x in s.version.split(".")), default=None)

    def require(self, scenario_id: str, version: str | None = None) -> Scenario:
        scenario = self.get(scenario_id, version)
        if scenario is None:
            raise KeyError(scenario_id)
        return scenario

    def all(self) -> tuple[Scenario, ...]:
        return tuple(self._scenarios.values())

    def implemented(self) -> tuple[Scenario, ...]:
        return tuple(s for s in self._scenarios.values() if s.implemented)

    def public_view(self) -> dict[str, str]:
        """id -> description, the shape older clients already consume."""
        return {
            s.scenario_id: s.description
            for s in self._scenarios.values()
            if self.get(s.scenario_id) is s
        }

    def validate_all(self) -> dict[str, list[str]]:
        return {s.scenario_id: s.validate() for s in self._scenarios.values() if s.validate()}


# --------------------------------------------------------------------------- #
# Shared fragments
# --------------------------------------------------------------------------- #

_LOGIN_STEPS: tuple[dict[str, Any], ...] = (
    {
        "tool": "navigate",
        "args": {"url": "{base_url}/"},
        "rationale": "open the login page",
    },
    {
        "tool": "fill",
        "args": {"selector": "[data-testid=username]", "value": "{username}"},
        "rationale": "fill the username from trusted config",
    },
    {
        "tool": "fill",
        "args": {"selector": "[data-testid=password]", "value": "{password}"},
        "rationale": "fill the password from trusted config (redacted in the trace)",
    },
    {
        "tool": "click",
        "args": {"selector": "[data-testid=login-submit]"},
        "rationale": "submit the login form",
    },
    {"tool": "snapshot", "args": {}, "rationale": "observe the page reached after login"},
)

_COMMON_CONSTRAINTS = (
    "只允许访问 base_url 的 origin，禁止跳转到外部站点",
    "凭据只从受信配置读取，动作记录必须脱敏",
    "每次探索必须有步数与时长上限，超限即停止",
)
_RUN_PROJECT_LINK = '[data-testid=project-item][data-project-id="{project_target_id}"] a'
_BOUNDARY_PROJECT_LINK = '[data-testid=project-item][data-project-id="{boundary_project_id}"] a'


def _goal(*assertions: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return assertions


def _login_with_password(password: str) -> tuple[dict[str, Any], ...]:
    steps = [dict(step) for step in _LOGIN_STEPS]
    steps[2] = {
        **steps[2],
        "args": {"selector": "[data-testid=password]", "value": password},
    }
    return tuple(steps)


def _create_project_steps(name: str, description: str) -> tuple[dict[str, Any], ...]:
    return (
        {
            "tool": "click",
            "args": {"selector": "[data-testid=new-project-link]"},
            "rationale": "open create form",
        },
        {
            "tool": "fill",
            "args": {"selector": "[data-testid=project-name]", "value": name},
            "rationale": "enter project name",
        },
        {
            "tool": "fill",
            "args": {"selector": "[data-testid=project-description]", "value": description},
            "rationale": "enter project description",
        },
        {
            "tool": "click",
            "args": {"selector": "[data-testid=create-submit]"},
            "rationale": "submit project form",
        },
        {"tool": "snapshot", "args": {}, "rationale": "observe create result"},
    )


def build_default_registry() -> ScenarioRegistry:
    """The scenarios this build actually knows about."""
    registry = ScenarioRegistry()

    registry.register(
        Scenario(
            scenario_id="teamflow-login",
            version="2.0.0",
            name="用户登录",
            description="登录 TeamFlow 并进入项目列表（已实现；动作仍经安全网关校验）",
            allowed_origins=("http://127.0.0.1:8001",),
            initial_url="/",
            intent_templates={
                "goal": "使用测试账号登录 TeamFlow，并进入项目列表页",
                "target": "/projects",
                "expected_outcomes": ["项目列表"],
                "required_assertions": ["state.current_user = tester"],
            },
            goal_definition=_goal(
                {
                    "name": "reached_project_list",
                    "kind": "url_exact",
                    "value": "/projects",
                    "required": True,
                },
                {
                    "name": "project_list_rendered",
                    "kind": "testid",
                    "value": "project-list",
                    "required": True,
                },
                {
                    "name": "new_project_entry_visible",
                    "kind": "element_visible",
                    "testid": "new-project-link",
                    "required": False,
                },
            ),
            allowed_actions=("navigate", "fill", "click", "snapshot", "wait"),
            constraints=_COMMON_CONSTRAINTS,
            success_conditions=("落在 /projects", "列表容器 project-list 存在"),
            stop_conditions=("步数/时长超限", "登录失败且页面无进展", "离开允许的 origin"),
            timeout_budget_s=45.0,
            evidence_requirements=("登录后页面快照", "动作序列（凭据已脱敏）"),
            steps=_LOGIN_STEPS,
            paths={
                "normal": _LOGIN_STEPS,
                "exception": _login_with_password("{wrong_password}"),
                "boundary": (
                    {"tool": "navigate", "args": {"url": "{base_url}/"}, "rationale": "open login"},
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=username]",
                            "value": "{boundary_username}",
                        },
                        "rationale": "use maximum-width username",
                    },
                    {
                        "tool": "fill",
                        "args": {"selector": "[data-testid=password]", "value": "{password}"},
                        "rationale": "fill password",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=login-submit]"},
                        "rationale": "submit boundary username",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "capture boundary response"},
                ),
                "recovery": _login_with_password("{wrong_password}")[:4]
                + (
                    {
                        "tool": "fill",
                        "args": {"selector": "[data-testid=username]", "value": "{username}"},
                        "rationale": "restore username after failed login",
                    },
                    {
                        "tool": "fill",
                        "args": {"selector": "[data-testid=password]", "value": "{password}"},
                        "rationale": "correct the password",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=login-submit]"},
                        "rationale": "retry login",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify recovery"},
                ),
            },
        )
    )

    registry.register(
        Scenario(
            scenario_id="project-create",
            version="2.0.0",
            name="项目创建",
            description="登录后新建一个项目，并确认列表页出现该项目的创建成功提示",
            allowed_origins=("http://127.0.0.1:8001",),
            initial_url="/",
            intent_templates={
                "goal": "登录后创建一个带唯一后缀的探索项目，并回到项目列表",
                "target": "/projects",
                "expected_outcomes": ["项目创建成功"],
                "required_assertions": ["state.project_exists = <本次创建的项目>"],
            },
            goal_definition=_goal(
                {
                    "name": "back_on_project_list",
                    "kind": "url_exact",
                    "value": "/projects",
                    "required": True,
                },
                {
                    "name": "success_banner",
                    "kind": "testid",
                    "value": "success-message",
                    "required": True,
                },
                {
                    "name": "project_row_present",
                    "kind": "testid",
                    "value": "project-item",
                    "required": True,
                },
            ),
            allowed_actions=("navigate", "fill", "click", "snapshot", "wait"),
            constraints=_COMMON_CONSTRAINTS + ("创建动作只允许一次，避免重复写入被测数据",),
            success_conditions=("/projects?created=1 渲染成功提示", "列表出现 project-item"),
            stop_conditions=("创建被拒绝（409/4xx）", "步数/时长超限"),
            timeout_budget_s=60.0,
            evidence_requirements=("创建表单提交前快照", "创建后列表页快照"),
            data_setup={
                "projects": [{"key": "duplicate_project_name", "name": "project-create-duplicate"}]
            },
            cleanup={"owned_resources": "lease-only"},
            preconditions=({"kind": "project_seeded", "key": "duplicate_project_name"},),
            steps=_LOGIN_STEPS
            + (
                {
                    "tool": "click",
                    "args": {"selector": "[data-testid=new-project-link]"},
                    "rationale": "open the create form",
                    "expect": {"url_path": "/projects"},
                },
                {
                    "tool": "fill",
                    "args": {
                        "selector": "[data-testid=project-name]",
                        "value": "R5-5 探索项目 {unique_suffix}",
                    },
                    "rationale": "type the project name",
                },
                {
                    "tool": "fill",
                    "args": {
                        "selector": "[data-testid=project-description]",
                        "value": "由 R5-5.2 场景化探索创建",
                    },
                    "rationale": "type a description",
                },
                {
                    "tool": "click",
                    "args": {"selector": "[data-testid=create-submit]"},
                    "rationale": "submit the create form",
                },
                {"tool": "snapshot", "args": {}, "rationale": "observe the list after creation"},
            ),
            paths={
                "normal": _LOGIN_STEPS
                + _create_project_steps(
                    "{run_prefix}created-{unique_suffix}", "{owner_marker}; normal"
                ),
                "exception": _LOGIN_STEPS
                + _create_project_steps("{duplicate_project_name}", "duplicate case"),
                "boundary": _LOGIN_STEPS
                + _create_project_steps("{boundary_name}", "{owner_marker}; boundary"),
                "recovery": _LOGIN_STEPS
                + _create_project_steps("{duplicate_project_name}", "duplicate case")
                + (
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-name]",
                            "value": "{run_prefix}recovered-{unique_suffix}",
                        },
                        "rationale": "correct duplicate name",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-description]",
                            "value": "{owner_marker}; recovered",
                        },
                        "rationale": "mark run ownership",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=create-submit]"},
                        "rationale": "submit recovered form",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify recovered creation"},
                ),
            },
            max_steps=15,
        )
    )

    registry.register(
        Scenario(
            scenario_id="project-query",
            version="2.0.0",
            name="项目查询",
            description="登录后从列表进入第一个项目的详情页，读取项目标题与状态",
            allowed_origins=("http://127.0.0.1:8001",),
            initial_url="/",
            intent_templates={
                "goal": "登录后查看项目列表中的第一个项目详情",
                # The detail route embeds a database id, so the anchor states the
                # route *shape*. `*` matches exactly one segment (see
                # `_path_matches`); the id itself is proven by the goal
                # assertions below, not by the path.
                "target": "/projects/*",
                # No text outcome: the detail page's only heading is the project
                # name (data), so a fixed phrase would either be satisfied by the
                # surrounding chrome or not at all. The structured assertions
                # below assert strictly more than a substring would.
                "expected_outcomes": [],
                "required_assertions": ["state.project_exists = <本次查询的项目>"],
            },
            goal_definition=_goal(
                {
                    "name": "on_project_detail",
                    "kind": "url_regex",
                    "value": r"^/projects/[A-Za-z0-9_-]+$",
                    "required": True,
                },
                {
                    "name": "detail_title_rendered",
                    "kind": "testid",
                    "value": "project-title",
                    "required": True,
                },
                {
                    "name": "status_visible",
                    "kind": "testid",
                    "value": "project-status",
                    "required": False,
                },
            ),
            allowed_actions=("navigate", "fill", "click", "snapshot", "wait"),
            constraints=_COMMON_CONSTRAINTS,
            success_conditions=("URL 匹配 /projects/<id>", "详情页 project-title 存在"),
            stop_conditions=("列表为空，无项目可查询", "步数/时长超限"),
            timeout_budget_s=60.0,
            evidence_requirements=("列表页快照", "详情页快照"),
            data_setup={
                "projects": [
                    {"key": "project_target", "name": "query-target", "description": "query seed"},
                    {
                        "key": "boundary_project",
                        "name": "query-boundary",
                        "description": "boundary seed",
                    },
                ]
            },
            cleanup={"owned_resources": "lease-only"},
            preconditions=(
                {"kind": "projects_seeded", "keys": ["project_target", "boundary_project"]},
            ),
            steps=_LOGIN_STEPS
            + (
                {
                    "tool": "click",
                    # The description says "the FIRST project", so the selector
                    # has to say it too. A bare ``[data-testid=project-item] a``
                    # matches every row, and Playwright's strict mode then
                    # refuses the click as soon as the list has two projects —
                    # a scenario that only works on a single-row database is not
                    # a scenario, it is a lucky fixture.
                    "args": {"selector": "[data-testid=project-item]:first-child a"},
                    "rationale": "open the first project from the list",
                    "expect": {"url_path": "/projects"},
                },
                {"tool": "snapshot", "args": {}, "rationale": "observe the detail page"},
            ),
            paths={
                "normal": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": _RUN_PROJECT_LINK},
                        "rationale": "open exact run-owned project",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "observe project detail"},
                ),
                "exception": _LOGIN_STEPS
                + (
                    {
                        "tool": "navigate",
                        "args": {"url": "{base_url}/projects/does-not-exist"},
                        "rationale": "request a nonexistent project",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "capture not-found result"},
                ),
                "boundary": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": _BOUNDARY_PROJECT_LINK},
                        "rationale": "open the second run-owned row",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify exact boundary row"},
                ),
                "recovery": _LOGIN_STEPS
                + (
                    {
                        "tool": "navigate",
                        "args": {"url": "{base_url}/projects/does-not-exist"},
                        "rationale": "observe missing project",
                    },
                    {
                        "tool": "navigate",
                        "args": {"url": "{base_url}/projects"},
                        "rationale": "return to list",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": _RUN_PROJECT_LINK},
                        "rationale": "open valid run-owned project",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify recovery"},
                ),
            },
            max_steps=10,
        )
    )

    registry.register(
        Scenario(
            scenario_id="page-navigation",
            version="2.0.0",
            name="页面导航",
            description="登录后从项目列表导航到新建项目页，验证页面切换与可交互元素",
            allowed_origins=("http://127.0.0.1:8001",),
            initial_url="/",
            intent_templates={
                "goal": "登录后通过页面导航进入新建项目页面",
                "target": "/projects/new",
                "expected_outcomes": ["新建项目"],
            },
            goal_definition=_goal(
                {
                    "name": "on_new_project_page",
                    "kind": "url_exact",
                    "value": "/projects/new",
                    "required": True,
                },
                {
                    "name": "form_field_present",
                    "kind": "element_exists",
                    "testid": "project-name",
                    "required": True,
                },
                {
                    "name": "title_mentions_new_project",
                    "kind": "page_state",
                    "field": "title",
                    "value": "新建项目",
                    "required": False,
                },
            ),
            allowed_actions=("navigate", "fill", "click", "snapshot", "wait"),
            constraints=_COMMON_CONSTRAINTS,
            success_conditions=("落在 /projects/new", "project-name 输入框存在"),
            stop_conditions=("导航被拒", "步数/时长超限"),
            timeout_budget_s=45.0,
            evidence_requirements=("导航前后两个页面快照",),
            steps=_LOGIN_STEPS
            + (
                {
                    "tool": "click",
                    "args": {"selector": "[data-testid=new-project-link]"},
                    "rationale": "navigate to the create page",
                    "expect": {"url_path": "/projects"},
                },
                {"tool": "snapshot", "args": {}, "rationale": "observe the new page"},
            ),
            paths={
                "normal": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=new-project-link]"},
                        "rationale": "navigate to create form",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify navigation"},
                ),
                "exception": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=missing-navigation-link]"},
                        "rationale": "attempt unavailable navigation",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "capture unavailable target"},
                ),
                "boundary": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=new-project-link]"},
                        "rationale": "open create form",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-name]",
                            "value": "{boundary_name}",
                        },
                        "rationale": "inspect maximum name length",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "capture boundary form state"},
                ),
                "recovery": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=missing-navigation-link]"},
                        "rationale": "record failed navigation attempt",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=new-project-link]"},
                        "rationale": "recover through valid navigation",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify valid destination"},
                ),
            },
            max_steps=10,
        )
    )

    registry.register(
        Scenario(
            scenario_id="project-edit",
            version="2.0.0",
            name="项目编辑",
            description="修改已存在项目的名称/描述并保存",
            allowed_origins=("http://127.0.0.1:8001",),
            initial_url="/projects",
            intent_templates={
                "goal": "编辑一个已存在的项目并保存",
                "target": "/projects/*",
                "expected_outcomes": [],
            },
            goal_definition=_goal(
                {
                    "name": "edit_url",
                    "kind": "url_regex",
                    "value": r"^/projects/[A-Za-z0-9_-]+$",
                    "required": True,
                },
                {
                    "name": "updated_project_title",
                    "kind": "testid",
                    "value": "project-title",
                    "required": True,
                },
            ),
            allowed_actions=("navigate", "fill", "click", "snapshot", "wait"),
            constraints=_COMMON_CONSTRAINTS,
            success_conditions=("编辑表单预填", "保存后详情页显示新名称"),
            stop_conditions=("项目种子不存在", "名称校验失败", "步数/时长超限"),
            timeout_budget_s=60.0,
            evidence_requirements=("编辑前表单快照", "提交后详情页快照"),
            data_setup={
                "projects": [
                    {"key": "project_target", "name": "edit-target", "description": "edit seed"}
                ]
            },
            cleanup={"owned_resources": "lease-only"},
            preconditions=({"kind": "project_seeded", "key": "project_target"},),
            denied_actions=("assert_text",),
            steps=_LOGIN_STEPS
            + (
                {
                    "tool": "click",
                    "args": {"selector": _RUN_PROJECT_LINK},
                    "rationale": "open the run-owned project",
                },
                {
                    "tool": "click",
                    "args": {"selector": "[data-testid=project-edit-link]"},
                    "rationale": "open the real project edit page",
                },
                {
                    "tool": "fill",
                    "args": {
                        "selector": "[data-testid=edit-project-name]",
                        "value": "{run_prefix}updated-{unique_suffix}",
                    },
                    "rationale": "enter the updated run-owned project name",
                },
                {
                    "tool": "fill",
                    "args": {
                        "selector": "[data-testid=edit-project-description]",
                        "value": "{owner_marker}; updated",
                    },
                    "rationale": "preserve the run ownership marker",
                },
                {
                    "tool": "click",
                    "args": {"selector": "[data-testid=edit-project-submit]"},
                    "rationale": "save the edit through the Web form",
                },
                {"tool": "snapshot", "args": {}, "rationale": "verify the updated detail page"},
            ),
            paths={
                "normal": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": _RUN_PROJECT_LINK},
                        "rationale": "open seeded project",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=project-edit-link]"},
                        "rationale": "open edit page",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=edit-project-name]",
                            "value": "{run_prefix}updated-{unique_suffix}",
                        },
                        "rationale": "enter new name",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=edit-project-description]",
                            "value": "{owner_marker}; updated",
                        },
                        "rationale": "retain run marker",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=edit-project-submit]"},
                        "rationale": "save project",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify updated page"},
                ),
                "exception": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": _RUN_PROJECT_LINK},
                        "rationale": "open seeded project",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=project-edit-link]"},
                        "rationale": "open edit page",
                    },
                    {
                        "tool": "fill",
                        "args": {"selector": "[data-testid=edit-project-name]", "value": ""},
                        "rationale": "submit an empty project name",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=edit-project-submit]"},
                        "rationale": "trigger server-side validation",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "capture validation feedback"},
                ),
                "boundary": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": _RUN_PROJECT_LINK},
                        "rationale": "open seeded project",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=project-edit-link]"},
                        "rationale": "open edit page",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=edit-project-name]",
                            "value": "{boundary_name}",
                        },
                        "rationale": "enter a 200 character boundary name",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=edit-project-description]",
                            "value": "{owner_marker}; boundary",
                        },
                        "rationale": "retain run marker",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=edit-project-submit]"},
                        "rationale": "save boundary length name",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify boundary save"},
                ),
                "recovery": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": _RUN_PROJECT_LINK},
                        "rationale": "open seeded project",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=project-edit-link]"},
                        "rationale": "open edit page",
                    },
                    {
                        "tool": "fill",
                        "args": {"selector": "[data-testid=edit-project-name]", "value": ""},
                        "rationale": "enter invalid empty name",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=edit-project-submit]"},
                        "rationale": "show validation error",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=edit-project-name]",
                            "value": "{run_prefix}recovered-{unique_suffix}",
                        },
                        "rationale": "correct the name",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=edit-project-description]",
                            "value": "{owner_marker}; recovered",
                        },
                        "rationale": "retain run marker",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=edit-project-submit]"},
                        "rationale": "save corrected data",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify recovery"},
                ),
            },
            max_steps=14,
        )
    )

    registry.register(
        Scenario(
            scenario_id="form-validation",
            name="表单验证",
            description="提交重复项目名称，验证服务端校验错误被正确展示",
            allowed_origins=("http://127.0.0.1:8001",),
            initial_url="/projects/new",
            intent_templates={
                "goal": "创建一个重名项目，确认页面展示校验错误",
                "target": "/projects/new",
                "expected_outcomes": ["project name already exists"],
            },
            goal_definition=_goal(
                {
                    "name": "error_banner",
                    "kind": "testid",
                    "value": "form-error",
                    "required": True,
                }
            ),
            allowed_actions=("navigate", "fill", "click", "snapshot"),
            constraints=_COMMON_CONSTRAINTS,
            success_conditions=("页面停留在 /projects/new", "form-error 可见"),
            stop_conditions=("名称未被拒绝（说明校验缺失）",),
            timeout_budget_s=45.0,
            evidence_requirements=("提交前快照", "错误页快照"),
            data_setup={
                "projects": [
                    {
                        "key": "duplicate_project_name",
                        "name": "form-validation-duplicate",
                        "description": "repeatable validation seed",
                    }
                ]
            },
            cleanup={"owned_resources": "lease-only"},
            preconditions=({"kind": "project_seeded", "key": "duplicate_project_name"},),
            steps=_LOGIN_STEPS
            + (
                {
                    "tool": "click",
                    "args": {"selector": "[data-testid=new-project-link]"},
                    "rationale": "open create form",
                },
                {
                    "tool": "fill",
                    "args": {
                        "selector": "[data-testid=project-name]",
                        "value": "{duplicate_project_name}",
                    },
                    "rationale": "enter the existing name",
                },
                {
                    "tool": "click",
                    "args": {"selector": "[data-testid=create-submit]"},
                    "rationale": "submit duplicate",
                },
                {"tool": "snapshot", "args": {}, "rationale": "observe duplicate validation"},
            ),
            paths={
                "normal": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=new-project-link]"},
                        "rationale": "open create form",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-name]",
                            "value": "{duplicate_project_name}",
                        },
                        "rationale": "enter the existing name",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=create-submit]"},
                        "rationale": "submit duplicate",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "observe duplicate validation"},
                ),
                "exception": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=new-project-link]"},
                        "rationale": "open create form",
                    },
                    {
                        "tool": "fill",
                        "args": {"selector": "[data-testid=project-name]", "value": ""},
                        "rationale": "trigger server-side empty-name validation",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=create-submit]"},
                        "rationale": "submit over-limit value",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "observe length validation"},
                ),
                "boundary": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=new-project-link]"},
                        "rationale": "open create form",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-name]",
                            "value": "{boundary_name}",
                        },
                        "rationale": "enter exactly 200 characters",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-description]",
                            "value": "{owner_marker}; boundary",
                        },
                        "rationale": "mark run ownership",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=create-submit]"},
                        "rationale": "submit boundary value",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify boundary success"},
                ),
                "recovery": _LOGIN_STEPS
                + (
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=new-project-link]"},
                        "rationale": "open create form",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-name]",
                            "value": "{duplicate_project_name}",
                        },
                        "rationale": "submit duplicate first",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=create-submit]"},
                        "rationale": "capture the validation error",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-name]",
                            "value": "{run_prefix}recovered-{unique_suffix}",
                        },
                        "rationale": "correct the name",
                    },
                    {
                        "tool": "fill",
                        "args": {
                            "selector": "[data-testid=project-description]",
                            "value": "{owner_marker}; recovered",
                        },
                        "rationale": "mark run ownership",
                    },
                    {
                        "tool": "click",
                        "args": {"selector": "[data-testid=create-submit]"},
                        "rationale": "submit corrected value",
                    },
                    {"tool": "snapshot", "args": {}, "rationale": "verify recovery"},
                ),
            },
            max_steps=14,
        )
    )

    return registry


_DEFAULT = build_default_registry()


def default_registry() -> ScenarioRegistry:
    return _DEFAULT
