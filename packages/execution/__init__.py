"""TraceGuard execution package — stage 2 local synchronous kernel.

Exports the FROZEN verdict rules, immutable test-asset models, Oracle
evaluator, event/evidence records, and the synchronous runner.
"""

from packages.execution.verdict import (
    AssertionVerdict,
    ExecutionStatus,
    InvalidTestCaseError,
    OracleVerdict,
    TestVerdict,
    aggregate_verdict,
    evaluate_oracle_binding,
)

__all__ = [
    "AssertionVerdict",
    "ExecutionStatus",
    "InvalidTestCaseError",
    "OracleVerdict",
    "TestVerdict",
    "aggregate_verdict",
    "evaluate_oracle_binding",
]
