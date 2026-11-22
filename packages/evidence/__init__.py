"""Stage-4 Event/Evidence infrastructure (C05/C06 draft).

Implements at-least-once Event delivery with consumer-side idempotency, an
Outbox for atomic business-commit + event-publish, sequence-gap detection that
never fabricates a "lost event" conclusion, and an Evidence Manager that keeps
raw / normalized / derived strictly separated with SHA-256 integrity.
"""

from packages.evidence.events import (  # noqa: F401
    ConsumerInbox,
    EventStore,
    GapReport,
    InboxResult,
    Outbox,
    dedupe_events,
    detect_sequence_gaps,
)
from packages.evidence.graph import CorrelationGraph, GraphEdge, GraphNode  # noqa: F401
from packages.evidence.manager import EvidenceManager, IntegrityResult  # noqa: F401

__all__ = [
    "EventStore",
    "Outbox",
    "ConsumerInbox",
    "InboxResult",
    "GapReport",
    "detect_sequence_gaps",
    "dedupe_events",
    "EvidenceManager",
    "IntegrityResult",
    "CorrelationGraph",
    "GraphNode",
    "GraphEdge",
]
