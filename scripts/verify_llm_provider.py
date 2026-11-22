"""Verify a real LLM provider connection with structured output (R4).

Safety discipline:
* the API key never enters this file, its output, or any report — it comes
  from ``.env`` / environment via ``provider_from_env()``;
* bounded budget: one call, small max_output_tokens, no retries beyond the
  provider config default;
* on any failure the script reports NOT_RUN with the error kind, never a
  fabricated success.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.agents.llm import Message, TracedClient, provider_from_env  # noqa: E402
from packages.core.config import ensure_env_file_vars  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "app_name": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "reasons": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["app_name", "risk_level", "reasons"],
    "additionalProperties": False,
}

PROMPT = (
    "You are a test-risk assessor. The web app under test is a task-management "
    "application (create tasks, edit them, mark them done, filter by status). "
    "Assess the testing risk level and give one concrete reason. "
    'Respond as JSON: {"app_name": str, "risk_level": "low"|"medium"|"high", '
    '"reasons": [str, ...]}'
)


def main() -> int:
    ensure_env_file_vars()
    wired = provider_from_env()
    if wired is None:
        print("RESULT=NOT_RUN reason=no_provider_configured (no key in env/.env)")
        return 1
    provider, config = wired
    print(f"provider={config.provider} model={config.model} is_real_model={provider.is_real_model}")

    client = TracedClient(provider=provider, config=config)
    try:
        parsed = client.complete_structured([Message(role="user", content=PROMPT)], SCHEMA)
    except Exception as exc:  # noqa: BLE001 - report kind, not secrets
        print(f"RESULT=NOT_RUN reason={type(exc).__name__}: {exc}")
        return 1

    trace = client.traces[-1]
    print(
        f"RESULT=PASS latency_ms={trace.latency_ms} attempts={trace.attempts} "
        f"prompt_tokens={trace.prompt_tokens} completion_tokens={trace.completion_tokens} "
        f"schema_ok={trace.schema_ok}"
    )
    print(f"structured_output={parsed}")
    print(f"trace_id={trace.trace_id} response_sha256={trace.response_sha256[:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
