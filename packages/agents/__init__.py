"""TraceGuard Agent orchestration package (stage 5-7).

Exposes the guarded Explorer, deterministic TestCase Compiler and Diagnosis v1.
The LLM decision loop is an injected Policy interface; no LLM is bundled.
"""

from packages.agents.diagnosis import Diagnosis, DiagnosisService, Hypothesis  # noqa: F401

__all__ = ["DiagnosisService", "Diagnosis", "Hypothesis"]
