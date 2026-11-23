"""Persistence outcome classification (R5-4.4 §6.3).

The bug this replaces: every write exception was caught as "duplicate", so a
full disk, a schema mismatch or a corrupted row looked exactly like normal
de-duplication — the run reported success and the data was simply missing.

Classification is deliberately explicit. A caller that swallows an error must
still say which kind it swallowed, and a *partial* success gets its own status
instead of being rounded to success or failure.
"""

from __future__ import annotations

from enum import StrEnum


class PersistenceOutcome(StrEnum):
    WRITTEN = "WRITTEN"
    DUPLICATE = "DUPLICATE"  # already stored, same content — a no-op
    EXISTS = "EXISTS"  # conflicting record already present
    CORRUPT = "CORRUPT"  # stored bytes are unreadable / unparsable
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"  # db/blob backend down
    FAILED = "FAILED"  # genuine write failure


_DUPLICATE_MARKERS = (
    "duplicate",
    "unique constraint",
    "already exists",
    "already persisted",
    "integrityerror: unique",
)
_CORRUPT_MARKERS = ("jsondecodeerror", "unpickling", "invalid json", "checksum", "digest mismatch")
_DEPENDENCY_MARKERS = (
    "operationalerror",
    "connection refused",
    "timeout",
    "timed out",
    "no such file or directory",
    "disk is full",
    "database is locked",
)


def classify_error(exc: BaseException) -> PersistenceOutcome:
    """Map an exception to a persistence outcome. Never returns WRITTEN."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in _DUPLICATE_MARKERS):
        return PersistenceOutcome.DUPLICATE
    if any(marker in text for marker in _CORRUPT_MARKERS):
        return PersistenceOutcome.CORRUPT
    if any(marker in text for marker in _DEPENDENCY_MARKERS):
        return PersistenceOutcome.DEPENDENCY_UNAVAILABLE
    return PersistenceOutcome.FAILED


class OutcomeCounter:
    """Small accumulator that keeps every outcome visible in the report."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.failures: list[dict[str, str]] = []

    def add(self, outcome: PersistenceOutcome, ref: str = "", detail: str = "") -> None:
        self.counts[outcome.value] = self.counts.get(outcome.value, 0) + 1
        if outcome not in (PersistenceOutcome.WRITTEN, PersistenceOutcome.DUPLICATE):
            self.failures.append({"ref": ref, "outcome": outcome.value, "detail": detail[:300]})

    def summary(self) -> dict[str, object]:
        """Status is OK / PARTIAL / FAILED — partial success is not "fine"."""
        written = self.counts.get(PersistenceOutcome.WRITTEN.value, 0)
        problems = sum(v for k, v in self.counts.items() if k not in {"WRITTEN", "DUPLICATE"})
        if problems and written:
            status = "PARTIAL"
        elif problems:
            status = "FAILED"
        else:
            status = "OK"
        return {
            "status": status,
            "written": written,
            "duplicates": self.counts.get(PersistenceOutcome.DUPLICATE.value, 0),
            "counts": dict(self.counts),
            "failures": list(self.failures),
        }
