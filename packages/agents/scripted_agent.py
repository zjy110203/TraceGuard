"""Explorer-level scripted-agent decision loop (R5-4.6 §3.6).

What this is — and is not
-------------------------
``scripted`` is a FIXED navigation script: the same ToolCalls every time, no
observation is consulted. ``scripted-agent`` is different in kind: each decision
is a *structured JSON answer* produced by ``ScriptedProvider``, validated by
``LLMPolicy`` against the decision schema and the tool whitelist, and guarded by
an observation expectation that must match the CURRENT page before the decision
is issued. The full chain is real:

    Observation -> LLMPolicy -> ScriptedProvider -> ToolGateway
    -> PlaywrightAdapter -> Result -> next Observation -> next decision

Nothing in it is a language model: ``ScriptedProvider.is_real_model`` is False,
the provider is labelled ``scripted``, and every response carries
``may_contain_real_inference=False``. No credential is read, no API is called,
no cost is produced.

Why the expectations matter: they prove the policy *consumes* each fresh
observation. If the browser ended up somewhere the script did not predict, the
decision loop stops loudly (``ScenarioDriftError``) instead of replaying canned
steps against the wrong page — the exact failure mode that made "scripted"
unsuitable for asserting goal-directed behaviour.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from packages.agents.exploration import TOOL_SPECS, Observation, ToolCall
from packages.agents.llm import ModelConfig, ScriptedProvider, TracedClient
from packages.agents.llm_policy import LLMPolicy


class ScenarioDriftError(RuntimeError):
    """The observed page does not match what the decision script expected."""

    def __init__(self, expected: str, observed: str) -> None:
        super().__init__(
            f"scenario drift: expected {expected!r}, observed {observed!r}; "
            "refusing to apply the next canned decision to the wrong page"
        )
        self.expected = expected
        self.observed = observed


def _norm_path(raw: str) -> str:
    path = raw.strip() or "/"
    if "://" in path:
        path = urlparse(path).path or "/"
    return path.rstrip("/") or "/"


@dataclass(frozen=True)
class AgentStep:
    """One scripted decision plus the page state it is legitimate for.

    ``decision`` is the raw JSON text the scripted provider returns — the same
    shape a real model would have to produce (action / args / rationale).
    """

    decision: str
    expect_url_path: str = ""
    expect_title_contains: str = ""
    expect_testid_visible: str = ""

    def expectation_summary(self) -> str:
        parts: list[str] = []
        if self.expect_url_path:
            parts.append(f"url_path={_norm_path(self.expect_url_path)}")
        if self.expect_title_contains:
            parts.append(f"title~={self.expect_title_contains!r}")
        if self.expect_testid_visible:
            parts.append(f"testid={self.expect_testid_visible!r}")
        return "; ".join(parts) or "(no expectation)"


class ScriptedAgentPolicy:
    """A scripted decision loop that still consumes real observations.

    NOT an LLM and never advertised as one: ``agent_version`` carries a
    ``NOT-REAL-INFERENCE`` marker and the provider is ``scripted``.
    """

    def __init__(
        self,
        steps: Sequence[AgentStep],
        *,
        goal: str,
        allowed_tools: frozenset[str] = frozenset(TOOL_SPECS),
    ) -> None:
        if not steps:
            raise ValueError("scripted-agent needs at least one decision")
        self._steps: list[AgentStep] = list(steps)
        self._index = 0
        provider = ScriptedProvider([s.decision for s in self._steps])
        self.client = TracedClient(
            provider=provider,
            config=ModelConfig(provider="scripted", model="scripted-agent", max_retries=0),
        )
        self._policy = LLMPolicy(client=self.client, goal=goal, allowed_tools=allowed_tools)
        self.drift: ScenarioDriftError | None = None

    @property
    def agent_version(self) -> str:
        return "scripted-agent-1.0.0-NOT-REAL-INFERENCE"

    @property
    def prompt_version(self) -> str:
        return "scripted-agent-v1"

    def call_traces(self) -> list[dict[str, Any]]:
        """Provider call traces: prompt/response hashes, schema validity."""
        return self.client.as_dicts()

    def _check_expectations(self, step: AgentStep, observation: Observation) -> None:
        if step.expect_url_path:
            expected = _norm_path(step.expect_url_path)
            observed = _norm_path(observation.url_path or "/")
            if expected != observed:
                raise ScenarioDriftError(f"url_path={expected}", f"url_path={observed}")
        if step.expect_title_contains:
            if step.expect_title_contains.lower() not in (observation.title or "").lower():
                raise ScenarioDriftError(
                    f"title~={step.expect_title_contains!r}", f"title={observation.title!r}"
                )
        if step.expect_testid_visible:
            if step.expect_testid_visible not in set(observation.visible_testids):
                raise ScenarioDriftError(
                    f"testid={step.expect_testid_visible!r}",
                    f"testids={sorted(observation.visible_testids)[:10]!r}",
                )

    def next_action(self, observation: Observation) -> ToolCall | None:
        if self._index >= len(self._steps):
            return None  # plan exhausted; the loop records PLAN_EXHAUSTED
        step = self._steps[self._index]
        self._index += 1
        self._check_expectations(step, observation)
        # The decision goes through LLMPolicy: schema validation, tool
        # whitelist, arg-shape checks. The ToolGateway re-checks all of it in
        # code afterwards — defence in depth, not trust by construction.
        return self._policy.next_action(observation)


def _decision_json(action: str, rationale: str, **args: Any) -> str:
    import json

    return json.dumps({"action": action, "args": args, "rationale": rationale})


def teamflow_login_agent_steps(*, base_url: str, username: str, password: str) -> list[AgentStep]:
    """The approved TeamFlow login scenario as an agent decision script.

    Values come from the deployment's test-data configuration at call time;
    the password is redacted the moment the action enters the trace, exactly as
    in the fixed script — plaintext never reaches a persisted artifact.
    """
    return [
        # No expectation on the first step: the decision is made BEFORE the
        # action runs, and the loop observes *before* the first action too, so
        # the page is still about:blank — there is nothing meaningful to match.
        AgentStep(
            decision=_decision_json("navigate", "open the login page", url=base_url),
        ),
        AgentStep(
            decision=_decision_json(
                "fill",
                "fill the username field",
                selector="[data-testid=username]",
                value=username,
            ),
            expect_url_path="/",
            expect_testid_visible="username",
        ),
        AgentStep(
            decision=_decision_json(
                "fill",
                "fill the password field",
                selector="[data-testid=password]",
                value=password,
            ),
            expect_url_path="/",
            expect_testid_visible="password",
        ),
        AgentStep(
            decision=_decision_json(
                "click", "submit the login form", selector="[data-testid=login-submit]"
            ),
            expect_url_path="/",
            expect_testid_visible="login-submit",
        ),
        AgentStep(
            decision=_decision_json("snapshot", "observe the page reached after submitting"),
            expect_url_path="/projects",
        ),
    ]
