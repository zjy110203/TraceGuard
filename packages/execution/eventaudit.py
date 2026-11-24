"""Pure sequence auditing for the workbench event stream.

Why this module exists
----------------------
The in-process :class:`packages.execution.orchestrator.EventBus` can lose
events in three distinct ways, and before this module none of them was
detectable:

* a slow SSE subscriber's bounded queue overflows (``queue.Full`` was swallowed);
* a client reconnects with a cursor while events it never saw are already gone;
* duplicate delivery (at-least-once replay) reaches the consumer twice.

Auditing a *list of observed sequences* is pure, deterministic and testable, so
it lives here rather than inside the streaming endpoint. The endpoint uses the
same helper (:func:`count_dropped_in_range`) to explain a gap it sees on the
wire; the bus uses nothing from here, which keeps the dependency one-way.

Contract notes
--------------
* ``RunEvent`` is untouched (C05: event_id/event_type/run_id/task_id/sequence/
  occurred_at/payload). Auditing is derived observational data, not an event.
* ``monotonic`` means "strictly increasing" (no duplicates, no regression).
  A gap is *not* a monotonicity violation; ``contiguous`` covers that, so a
  caller can distinguish "out of order" from "missing in the middle".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def audit_event_sequences(sequences: Sequence[int]) -> dict[str, Any]:
    """Audit a sequence of observed event sequence numbers.

    The first element is the baseline: nothing is known about what came before
    it, so it never produces a gap. Everything after it is checked for gaps,
    duplicates and ordering regressions.

    Returns ``{"gaps", "duplicates", "out_of_order", "monotonic", "contiguous",
    "count"}`` where:

    * ``gaps``: ``[{"from": int, "to": int, "count": int}, ...]`` inclusive
      ranges that are missing between two observed values;
    * ``duplicates``: ascending list of sequence numbers observed more than once
      (each reported once);
    * ``out_of_order``: ``[{"index": int, "previous": int, "sequence": int}]``
      for each value smaller than the highest value seen so far;
    * ``monotonic``: ``True`` iff no duplicates and no regressions;
    * ``contiguous``: ``monotonic`` and no gaps — a perfectly dense stream;
    * ``count``: number of observations audited.
    """
    gaps: list[dict[str, int]] = []
    duplicates: list[int] = []
    out_of_order: list[dict[str, int]] = []
    seen: set[int] = set()
    highest: int | None = None

    for index, raw in enumerate(sequences):
        sequence = int(raw)
        if highest is not None:
            if sequence < highest:
                out_of_order.append({"index": index, "previous": highest, "sequence": sequence})
            elif sequence > highest + 1:
                gaps.append(
                    {"from": highest + 1, "to": sequence - 1, "count": sequence - highest - 1}
                )
            highest = max(highest, sequence)
        else:
            highest = sequence
        if sequence in seen and sequence not in duplicates:
            duplicates.append(sequence)
        seen.add(sequence)

    duplicates.sort()
    monotonic = not duplicates and not out_of_order
    return {
        "gaps": gaps,
        "duplicates": duplicates,
        "out_of_order": out_of_order,
        "monotonic": monotonic,
        "contiguous": monotonic and not gaps,
        "count": len(sequences),
    }


def count_dropped_in_range(ranges: Sequence[tuple[int, int]], start: int, end: int) -> int:
    """How many sequences in ``[start, end]`` are covered by ``ranges``.

    Used to cross-check an observed gap against the drops the bus actually
    recorded for that subscriber: a gap fully covered by recorded drops is
    queue overflow, an uncovered gap came from somewhere else (restart, lost
    history, a producer that never published). ``ranges`` must be disjoint and
    sorted; :meth:`EventBus.dropped_ranges` guarantees that, and a single
    subscriber's ranges are disjoint by construction.
    """
    if end < start:
        return 0
    total = 0
    for low, high in ranges:
        overlap = min(high, end) - max(low, start) + 1
        if overlap > 0:
            total += overlap
    return total
