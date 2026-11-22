"""Broker abstraction for execution dispatch (dev-safe, production-shaped).

Why this exists: the stage-3 orchestrator handed work straight to a
``ThreadPoolExecutor``. That is fine for one process but cannot express the
behaviours a real queue gives you — at-least-once delivery, visibility timeout,
idempotent consumption, dead letters, and depth metrics. Those behaviours are
implemented here so the orchestrator's semantics no longer depend on the
executor's quirks.

Deliberately honest about scope:
* ``InProcessBroker`` is a real, tested implementation (single process, memory).
* ``RedisBroker`` is a thin adapter written against redis-py but **it cannot be
  exercised here** — no Redis server and no ``redis`` package in this
  environment. It refuses to construct rather than silently degrade, and it is
  NOT claimed as verified integration.
* Automatic retry of a failed *execution* is intentionally not provided: a retry
  must create a NEW Run (append-only history), which is a user action, not a
  queue decision. Failures go to the dead-letter list with a reason.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ExecutionJob:
    """One attempt to execute one case. ``job_id`` equals the Run id (unique)."""

    job_id: str
    run_id: str
    task_id: str
    case_id: str
    case_file: str
    timeout_s: float
    attempt: int
    created_at: str
    delivery_count: int = 0


@dataclass
class Receipt:
    """Handle returned by ``reserve``; the caller must ack or nack it."""

    job: ExecutionJob
    receipt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    reserved_at: float = field(default_factory=time.monotonic)
    visible_after: float = 0.0


@dataclass(frozen=True)
class DeadLetter:
    job: ExecutionJob
    reason: str
    failed_at: str
    deliveries: int


class BrokerError(RuntimeError):
    """Raised when a broker cannot provide its guarantee."""


class Broker(Protocol):
    def enqueue(self, job: ExecutionJob) -> bool: ...

    def reserve(self, timeout: float = 1.0) -> Receipt | None: ...

    def ack(self, receipt: Receipt) -> None: ...

    def nack(self, receipt: Receipt, reason: str, requeue: bool = False) -> None: ...

    def dead_letters(self) -> list[DeadLetter]: ...

    def depth(self) -> dict[str, int]: ...


class InProcessBroker:
    """Thread-safe at-least-once queue with visibility timeout and dead letters.

    Guarantees (all covered by tests):
    * enqueue is idempotent per ``job_id`` (duplicate deliveries collapse);
    * ``reserve`` hides the job until ack, or until the visibility timeout
      elapses, after which it becomes reservable again (at-least-once);
    * a job whose delivery count exceeds ``max_deliveries`` becomes a dead letter
      instead of spinning forever.
    """

    def __init__(self, *, visibility_timeout: float = 30.0, max_deliveries: int = 5) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._ready: deque[ExecutionJob] = deque()
        self._inflight: dict[str, Receipt] = {}
        self._seen: set[str] = set()  # job_ids ever accepted (dedupe)
        self._dead: list[DeadLetter] = []
        self._visibility = visibility_timeout
        self._max_deliveries = max_deliveries

    # ------------------------------------------------------------- producer

    def enqueue(self, job: ExecutionJob) -> bool:
        """Return False when this job_id was already accepted (idempotent)."""
        with self._cond:
            if job.job_id in self._seen:
                return False
            self._seen.add(job.job_id)
            self._ready.append(job)
            self._cond.notify()
            return True

    # ------------------------------------------------------------- consumer

    def reserve(self, timeout: float = 1.0) -> Receipt | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                self._reclaim_expired()
                if self._ready:
                    job = self._ready.popleft()
                    receipt = Receipt(job=job, visible_after=time.monotonic() + self._visibility)
                    self._inflight[receipt.receipt_id] = receipt
                    return receipt
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.1))

    def _reclaim_expired(self) -> None:
        now = time.monotonic()
        expired = [r for r in self._inflight.values() if r.visible_after <= now]
        for receipt in expired:
            self._inflight.pop(receipt.receipt_id, None)
            job = receipt.job
            delivered = job.delivery_count + 1
            if delivered >= self._max_deliveries:
                self._dead.append(
                    DeadLetter(
                        job=job,
                        reason=f"visibility timeout x{delivered}; max deliveries reached",
                        failed_at=_now(),
                        deliveries=delivered,
                    )
                )
            else:
                self._ready.append(ExecutionJob(**{**job.__dict__, "delivery_count": delivered}))

    def ack(self, receipt: Receipt) -> None:
        with self._cond:
            self._inflight.pop(receipt.receipt_id, None)
            self._cond.notify()

    def nack(self, receipt: Receipt, reason: str, requeue: bool = False) -> None:
        with self._cond:
            self._inflight.pop(receipt.receipt_id, None)
            job = receipt.job
            delivered = job.delivery_count + 1
            if requeue and delivered < self._max_deliveries:
                self._ready.append(ExecutionJob(**{**job.__dict__, "delivery_count": delivered}))
            else:
                self._dead.append(
                    DeadLetter(job=job, reason=reason, failed_at=_now(), deliveries=delivered)
                )
            self._cond.notify()

    # -------------------------------------------------------------- observe

    def dead_letters(self) -> list[DeadLetter]:
        with self._lock:
            return list(self._dead)

    def depth(self) -> dict[str, int]:
        with self._lock:
            return {
                "ready": len(self._ready),
                "inflight": len(self._inflight),
                "dead": len(self._dead),
            }

    def requeue_dead_letter(self, job_id: str) -> bool:
        """Operator action: put a dead letter back on the ready queue."""
        with self._cond:
            for index, letter in enumerate(self._dead):
                if letter.job.job_id == job_id:
                    self._dead.pop(index)
                    self._ready.append(ExecutionJob(**{**letter.job.__dict__, "delivery_count": 0}))
                    self._cond.notify()
                    return True
            return False


class Dispatcher:
    """N worker threads that reserve → handle → ack/nack.

    ``is_settled(job)`` lets the caller tell the dispatcher that a redelivered job
    was already completed (the run is terminal), so it is acked instead of being
    executed twice. That is the idempotency guard for at-least-once delivery.
    """

    def __init__(
        self,
        broker: Broker,
        handler: Callable[[ExecutionJob], None],
        *,
        is_settled: Callable[[ExecutionJob], bool] | None = None,
        workers: int = 2,
    ) -> None:
        self.broker = broker
        self.handler = handler
        self.is_settled = is_settled or (lambda job: False)
        self.workers = workers
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._handled = 0
        self._skipped = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(target=self._loop, name=f"tg-dispatch-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _loop(self) -> None:
        while not self._stop.is_set():
            receipt = self.broker.reserve(timeout=0.5)
            if receipt is None:
                continue
            try:
                if self.is_settled(receipt.job):
                    with self._lock:
                        self._skipped += 1
                    self.broker.ack(receipt)
                    continue
                self.handler(receipt.job)
            except Exception as exc:  # noqa: BLE001 - any handler failure is a dead letter
                self.broker.nack(receipt, f"{type(exc).__name__}: {exc}", requeue=False)
            else:
                with self._lock:
                    self._handled += 1
                self.broker.ack(receipt)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"handled": self._handled, "skipped_duplicates": self._skipped}


class RedisBroker:
    """Adapter sketch for redis-py. NOT verified in this environment.

    Constructing raises unless ``redis`` is importable AND ``allow_unverified`` is
    passed, so a caller can never mistake this for a working production broker.
    """

    def __init__(self, url: str, *, allow_unverified: bool = False) -> None:
        try:
            import redis  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise BrokerError(
                "redis package is not installed; RedisBroker is unavailable. "
                "The in-process broker is the supported implementation here."
            ) from exc
        if not allow_unverified:
            raise BrokerError(
                "RedisBroker has never been verified against a live Redis server in "
                "this project; pass allow_unverified=True only for experiments."
            )
        self.url = url

    def enqueue(self, job: ExecutionJob) -> bool:  # pragma: no cover - unverified
        raise BrokerError("RedisBroker.enqueue is not implemented (unverified)")

    def reserve(self, timeout: float = 1.0) -> Receipt | None:  # pragma: no cover
        raise BrokerError("RedisBroker.reserve is not implemented (unverified)")

    def ack(self, receipt: Receipt) -> None:  # pragma: no cover
        raise BrokerError("RedisBroker.ack is not implemented (unverified)")

    def nack(
        self, receipt: Receipt, reason: str, requeue: bool = False
    ) -> None:  # pragma: no cover
        raise BrokerError("RedisBroker.nack is not implemented (unverified)")

    def dead_letters(self) -> list[DeadLetter]:  # pragma: no cover
        return []

    def depth(self) -> dict[str, int]:  # pragma: no cover
        return {"ready": 0, "inflight": 0, "dead": 0}


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
