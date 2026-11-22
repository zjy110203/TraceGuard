"""Broker semantics: at-least-once, visibility timeout, dedupe, dead letters.

These are the guarantees the orchestrator now relies on. They are tested at the
broker level (deterministic, fast) and at the dispatcher level (threading), not
through the whole HTTP stack.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from packages.execution.broker import (
    BrokerError,
    Dispatcher,
    ExecutionJob,
    InProcessBroker,
    RedisBroker,
)


def _job(job_id: str = "run_1", *, attempt: int = 1, deliveries: int = 0) -> ExecutionJob:
    return ExecutionJob(
        job_id=job_id,
        run_id=job_id,
        task_id="task_1",
        case_id="TC-CREATE-001",
        case_file="TC-CREATE-001.json",
        timeout_s=30.0,
        attempt=attempt,
        created_at=datetime.now(UTC).isoformat(),
        delivery_count=deliveries,
    )


def test_enqueue_is_idempotent_per_job_id() -> None:
    broker = InProcessBroker()
    assert broker.enqueue(_job()) is True
    assert broker.enqueue(_job()) is False
    assert broker.depth() == {"ready": 1, "inflight": 0, "dead": 0}


def test_reserve_hides_job_until_ack() -> None:
    broker = InProcessBroker()
    broker.enqueue(_job())
    receipt = broker.reserve(timeout=0.2)
    assert receipt is not None
    assert broker.depth()["inflight"] == 1
    assert broker.reserve(timeout=0.05) is None  # hidden while in flight
    broker.ack(receipt)
    assert broker.depth() == {"ready": 0, "inflight": 0, "dead": 0}


def test_visibility_timeout_redelivers_the_same_job() -> None:
    broker = InProcessBroker(visibility_timeout=0.05)
    broker.enqueue(_job())
    first = broker.reserve(timeout=0.2)
    assert first is not None
    time.sleep(0.08)  # never ack: the reservation expires
    second = broker.reserve(timeout=0.2)
    assert second is not None
    assert second.job.job_id == first.job.job_id
    assert second.job.delivery_count == 1
    assert second.receipt_id != first.receipt_id


def test_nack_with_requeue_delivers_again() -> None:
    broker = InProcessBroker()
    broker.enqueue(_job())
    receipt = broker.reserve(timeout=0.2)
    assert receipt is not None
    broker.nack(receipt, "transient", requeue=True)
    again = broker.reserve(timeout=0.2)
    assert again is not None
    assert again.job.delivery_count == 1
    assert broker.dead_letters() == []


def test_nack_without_requeue_creates_a_dead_letter() -> None:
    broker = InProcessBroker()
    broker.enqueue(_job())
    receipt = broker.reserve(timeout=0.2)
    assert receipt is not None
    broker.nack(receipt, "handler exploded", requeue=False)
    dead = broker.dead_letters()
    assert len(dead) == 1
    assert dead[0].reason == "handler exploded"
    assert dead[0].job.job_id == "run_1"
    assert broker.depth() == {"ready": 0, "inflight": 0, "dead": 1}


def test_max_deliveries_moves_job_to_dead_letter() -> None:
    broker = InProcessBroker(visibility_timeout=0.01, max_deliveries=2)
    broker.enqueue(_job())
    for _ in range(2):  # two deliveries are allowed; the second expiry dead-letters it
        receipt = broker.reserve(timeout=0.1)
        assert receipt is not None
        time.sleep(0.03)  # let it expire instead of acking
    assert broker.reserve(timeout=0.05) is None
    assert broker.depth()["dead"] == 1
    assert "max deliveries" in broker.dead_letters()[0].reason


def test_dead_letter_can_be_requeued_by_an_operator() -> None:
    broker = InProcessBroker()
    broker.enqueue(_job())
    receipt = broker.reserve(timeout=0.2)
    assert receipt is not None
    broker.nack(receipt, "bad case", requeue=False)
    assert broker.requeue_dead_letter("run_1") is True
    assert broker.dead_letters() == []
    assert broker.reserve(timeout=0.2) is not None
    assert broker.requeue_dead_letter("nope") is False


def test_dispatcher_runs_each_job_once_and_skips_settled_duplicates() -> None:
    broker = InProcessBroker()
    broker.enqueue(_job("run_a"))
    broker.enqueue(_job("run_b"))
    seen: list[str] = []

    def record(job: ExecutionJob) -> None:
        seen.append(job.job_id)

    dispatcher = Dispatcher(
        broker,
        record,
        is_settled=lambda job: job.job_id == "run_b",  # pretend b already finished
        workers=2,
    )
    dispatcher.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and (broker.depth()["ready"] or broker.depth()["inflight"]):
            time.sleep(0.02)
    finally:
        dispatcher.stop()
    assert seen == ["run_a"]
    stats = dispatcher.stats()
    assert stats["handled"] == 1
    assert stats["skipped_duplicates"] == 1
    assert broker.depth() == {"ready": 0, "inflight": 0, "dead": 0}


def test_dispatcher_dead_letters_a_failing_handler() -> None:
    broker = InProcessBroker()
    broker.enqueue(_job("run_boom"))

    def explode(job: ExecutionJob) -> None:
        raise RuntimeError("browser crashed")

    dispatcher = Dispatcher(broker, explode, workers=1)
    dispatcher.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not broker.dead_letters():
            time.sleep(0.02)
    finally:
        dispatcher.stop()
    dead = broker.dead_letters()
    assert len(dead) == 1
    assert "browser crashed" in dead[0].reason


def test_redis_broker_refuses_to_pretend() -> None:
    """Either redis is absent, or the adapter has never been verified — both must raise."""
    with pytest.raises(BrokerError):
        RedisBroker("redis://localhost:6379/0")
