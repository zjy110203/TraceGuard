"""LLM provider abstraction for the Explorer.

Scope, stated plainly: this module makes a real model *pluggable and traceable*.
It does not, and cannot, prove model quality here — no credentials exist in this
environment, so no real call is ever made in tests.

Three deliberate design points:

1. **No provider is fabricated.** ``provider_from_env()`` returns ``None`` when no
   key is configured. Callers must handle that explicitly (the Explorer then
   reports "LLM unavailable" instead of silently using a scripted stand-in).
2. **Scripted output is labelled.** ``ScriptedProvider`` is a deterministic test
   double; it exposes ``is_real_model = False`` and its responses carry
   ``provider="scripted"`` so nothing downstream can mistake it for inference.
3. **Every call is traced.** ``CallTrace`` records a prompt/response hash, token
   counts, latency and attempt number, so a decision can be audited after the fact
   even though the prompt itself may contain page text.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from jsonschema import Draft202012Validator


class LLMError(RuntimeError):
    """Base class for provider/structured-output failures."""


class ProviderUnavailable(LLMError):
    """No usable provider is configured (missing credentials or package)."""


class StructuredOutputError(LLMError):
    """The model did not return JSON matching the required schema."""


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    temperature: float = 0.0
    max_output_tokens: int = 512
    timeout_s: float = 30.0
    max_retries: int = 2


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    attempts: int
    may_contain_real_inference: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class CallTrace:
    trace_id: str
    provider: str
    model: str
    prompt_sha256: str
    response_sha256: str
    prompt_chars: int
    response_chars: int
    latency_ms: int
    attempts: int
    at: str
    schema_ok: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(Protocol):
    is_real_model: bool

    def complete(
        self,
        messages: list[Message],
        *,
        config: ModelConfig,
        schema: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


class ScriptedProvider:
    """Deterministic double. NEVER a language model — used only in tests/dev."""

    is_real_model = False

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self._calls = 0

    def complete(
        self,
        messages: list[Message],
        *,
        config: ModelConfig,
        schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        if self._calls >= len(self._script):
            raise ProviderUnavailable("scripted provider exhausted its script")
        text = self._script[self._calls]
        self._calls += 1
        return LLMResponse(
            text=text,
            provider="scripted",
            model="scripted-not-a-model",
            latency_ms=0,
            attempts=1,
            may_contain_real_inference=False,
        )


class OpenAICompatibleProvider:
    """Chat-completions client for any OpenAI-compatible endpoint.

    Wired, bounded and traceable, but **unverified**: it has never been pointed at
    a live model in this project. Construction without an API key raises
    ``ProviderUnavailable`` rather than degrading silently.
    """

    is_real_model = True

    def __init__(self, *, base_url: str, api_key: str | None, model: str) -> None:
        if not api_key:
            raise ProviderUnavailable("missing API key; refusing to fabricate a provider")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model

    def complete(
        self,
        messages: list[Message],
        *,
        config: ModelConfig,
        schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": config.temperature,
            "max_tokens": config.max_output_tokens,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        attempts = 0
        last_error: Exception | None = None
        while attempts <= config.max_retries:
            attempts += 1
            try:
                with httpx.Client(timeout=config.timeout_s, trust_env=False) as client:
                    resp = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=payload,
                    )
                if resp.status_code >= 400:
                    raise LLMError(f"provider HTTP {resp.status_code}")
                body = resp.json()
                text = body["choices"][0]["message"]["content"]
                usage = body.get("usage") or {}
                return LLMResponse(
                    text=text,
                    provider=config.provider,
                    model=config.model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts,
                    may_contain_real_inference=True,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                )
            except (httpx.HTTPError, KeyError, IndexError, LLMError) as exc:
                last_error = exc
                continue
        raise LLMError(f"provider failed after {attempts} attempts: {last_error}")


@dataclass
class TracedClient:
    """Wraps a provider, enforces structured output, and records a call trace."""

    provider: LLMProvider
    config: ModelConfig
    traces: list[CallTrace] = field(default_factory=list)

    @property
    def is_real_model(self) -> bool:
        return bool(getattr(self.provider, "is_real_model", False))

    def complete_structured(
        self,
        messages: list[Message],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 2):
            response = self.provider.complete(messages, config=self.config, schema=schema)
            ok = False
            try:
                parsed = _parse_json(response.text)
                Draft202012Validator.check_schema(schema)
                errors = sorted(Draft202012Validator(schema).iter_errors(parsed), key=str)
                if errors:
                    raise StructuredOutputError(
                        f"schema violation at {list(errors[0].path)}: {errors[0].message}"
                    )
                ok = True
                self._record(messages, response, attempt, ok)
                return parsed
            except (LLMError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                self._record(messages, response, attempt, ok)
                continue
        raise StructuredOutputError(f"no valid structured output: {last_error}")

    def _record(
        self, messages: list[Message], response: LLMResponse, attempt: int, ok: bool
    ) -> None:
        prompt = "\n".join(f"{m.role}:{m.content}" for m in messages)
        self.traces.append(
            CallTrace(
                trace_id=uuid.uuid4().hex[:12],
                provider=response.provider,
                model=response.model,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                response_sha256=hashlib.sha256(response.text.encode()).hexdigest(),
                prompt_chars=len(prompt),
                response_chars=len(response.text),
                latency_ms=response.latency_ms,
                attempts=attempt,
                at=_now(),
                schema_ok=ok,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
        )

    def as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "trace_id": t.trace_id,
                "provider": t.provider,
                "model": t.model,
                "prompt_sha256": t.prompt_sha256,
                "response_sha256": t.response_sha256,
                "prompt_chars": t.prompt_chars,
                "response_chars": t.response_chars,
                "latency_ms": t.latency_ms,
                "attempts": t.attempts,
                "at": t.at,
                "schema_ok": t.schema_ok,
                "prompt_tokens": t.prompt_tokens,
                "completion_tokens": t.completion_tokens,
            }
            for t in self.traces
        ]


# Keys are checked by NAME only; values are never read, logged or echoed.
PROVIDER_ENV: dict[str, tuple[str, str]] = {
    "DEEPSEEK_API_KEY": ("deepseek", "https://api.deepseek.com/v1"),
    "OPENAI_API_KEY": ("openai", "https://api.openai.com/v1"),
}
PROVIDER_MODELS: dict[str, frozenset[str]] = {
    "deepseek": frozenset({"deepseek-chat", "deepseek-reasoner"}),
    "openai": frozenset({"gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3", "o4-mini"}),
}
DEFAULT_MODELS = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}


def resolve_provider_config(
    environ: dict[str, str] | None = None, *, model: str | None = None
) -> tuple[str, str, str, str] | None:
    """Resolve an explicit Provider/Endpoint/Model/Key binding without a call."""
    values = os.environ if environ is None else environ
    configured_keys = {
        provider: values.get(key_name, "")
        for key_name, (provider, _) in PROVIDER_ENV.items()
        if values.get(key_name, "")
    }
    provider = str(values.get("LLM_PROVIDER") or "").strip().casefold()
    endpoint = str(values.get("LLM_ENDPOINT") or "").strip().rstrip("/")
    selected_model = str(model or values.get("LLM_MODEL") or "").strip()

    if not configured_keys:
        if provider or endpoint or selected_model:
            raise ProviderUnavailable(
                "provider, endpoint or model is configured without an API key"
            )
        return None
    if not provider:
        raise ProviderUnavailable("LLM_PROVIDER must explicitly select the configured provider")
    if provider not in PROVIDER_MODELS:
        raise ProviderUnavailable("unknown LLM_PROVIDER")
    if provider not in configured_keys:
        raise ProviderUnavailable("selected LLM_PROVIDER has no matching API key")
    expected_endpoint = next(url for name, url in PROVIDER_ENV.values() if name == provider)
    if endpoint and endpoint != expected_endpoint.rstrip("/"):
        raise ProviderUnavailable("LLM_ENDPOINT does not match the selected provider")
    endpoint = expected_endpoint.rstrip("/")
    selected_model = selected_model or DEFAULT_MODELS[provider]
    if selected_model not in PROVIDER_MODELS[provider]:
        raise ProviderUnavailable("LLM_MODEL is unknown or does not match the selected provider")
    return provider, endpoint, selected_model, configured_keys[provider]


def validate_provider_config(environ: dict[str, str] | None = None) -> None:
    """Fail closed on malformed config; this function never constructs a client."""
    resolve_provider_config(environ)


def provider_from_env(
    model: str | None = None,
) -> tuple[LLMProvider, ModelConfig] | None:
    """Return a provider only when a known key is present. Never invents one.

    Deliberately does NOT load ``.env`` here: tests assert the no-credential
    behaviour via ``os.environ`` and must stay hermetic. Entry points that
    want ``.env`` support call ``ensure_env_file_vars()`` explicitly first.
    """
    resolved = resolve_provider_config(model=model)
    if resolved is None:
        return None
    provider_name, base_url, model_name, key = resolved
    provider = OpenAICompatibleProvider(base_url=base_url, api_key=key, model=model_name)
    return provider, ModelConfig(provider=provider_name, model=model_name)


def _parse_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    return json.loads(stripped)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
