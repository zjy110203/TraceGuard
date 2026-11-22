"""Stage-3 Task/Run/Step domain model (C04 Execution).

Core rules from v1.1 (kept literal):
  * Task is a LOGICAL job; Run is ONE actual execution of it.
  * Every retry creates a NEW Run with ``attempt = previous+1``; a Run row is
    append-only and is NEVER overwritten by a later attempt.
  * Execution Status / Test Verdict / Diagnosis Status / Diagnosis Outcome are
    FOUR INDEPENDENT dimensions. Diagnosis stays NOT_STARTED here (stage 7).
  * State transitions are validated; illegal transitions are rejected.

Persistence uses the platform SQLAlchemy Base, so the same code runs on SQLite
(dev/test) and PostgreSQL (switch DATABASE_URL) without changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import (
    JSON,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base
from packages.core.errors import ConflictError, NotFoundError


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStatus:
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


# Run status vocabulary mirrors ExecutionStatus but is stored as a plain string
# to keep the ORM layer independent of the verdict module's enum.
class RunStatus:
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"

    TERMINAL = frozenset({COMPLETED, FAILED, TIMED_OUT, CANCELLED})


# Allowed forward transitions. Cancellation may jump from any non-terminal
# state to CANCELLED; this table is the single source of truth for the rest.
_ALLOWED: dict[str, frozenset[str]] = {
    RunStatus.QUEUED: frozenset({RunStatus.PREPARING, RunStatus.CANCELLED}),
    RunStatus.PREPARING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.FINALIZING, RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.CANCELLED}
    ),
    RunStatus.FINALIZING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}),
}
for _terminal in RunStatus.TERMINAL:
    _ALLOWED[_terminal] = frozenset()  # immutable once terminal


class TaskRecord(Base):
    __tablename__ = "tg_task"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default=TaskStatus.ACTIVE, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)


class RunRecord(Base):
    __tablename__ = "tg_run"
    __table_args__ = (UniqueConstraint("task_id", "attempt", name="uq_run_task_attempt"),)

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tg_task.task_id"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(String, nullable=False)
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default=RunStatus.QUEUED, nullable=False)
    execution_status: Mapped[str] = mapped_column(String, default="RUNNING", nullable=False)
    test_verdict: Mapped[str] = mapped_column(String, default="", nullable=False)
    diagnosis_status: Mapped[str] = mapped_column(String, default="NOT_STARTED", nullable=False)
    diagnosis_outcome: Mapped[str] = mapped_column(String, default="NOT_STARTED", nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)
    started_at: Mapped[str] = mapped_column(String, default="", nullable=False)
    finished_at: Mapped[str] = mapped_column(String, default="", nullable=False)
    heartbeat_at: Mapped[str] = mapped_column(String, default="", nullable=False)
    worker_id: Mapped[str] = mapped_column(String, default="", nullable=False)
    timeout_s: Mapped[float] = mapped_column(nullable=False, default=60.0)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str] = mapped_column(String, default="", nullable=False)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class StepRecord(Base):
    __tablename__ = "tg_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("tg_run.run_id"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    ok: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[str] = mapped_column(String, default="", nullable=False)


@dataclass(frozen=True)
class RunView:
    """Read model; verdict/diagnosis dimensions kept separate."""

    run_id: str
    task_id: str
    case_id: str
    attempt: int
    status: str
    execution_status: str
    test_verdict: str
    diagnosis_status: str
    diagnosis_outcome: str
    created_at: str
    started_at: str
    finished_at: str
    heartbeat_at: str
    worker_id: str
    timeout_s: float
    cancel_requested: bool
    error: str
    has_report: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_view(row: RunRecord) -> RunView:
    return RunView(
        run_id=row.run_id,
        task_id=row.task_id,
        case_id=row.case_id,
        attempt=row.attempt,
        status=row.status,
        execution_status=row.execution_status,
        test_verdict=row.test_verdict,
        diagnosis_status=row.diagnosis_status,
        diagnosis_outcome=row.diagnosis_outcome,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        heartbeat_at=row.heartbeat_at,
        worker_id=row.worker_id,
        timeout_s=row.timeout_s,
        cancel_requested=bool(row.cancel_requested),
        error=row.error,
        has_report=row.report is not None,
    )


class TaskService:
    """Logical task registry."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    def create(self, task_id: str, case_id: str, title: str) -> dict[str, Any]:
        with self._sessions() as session:
            if session.get(TaskRecord, task_id):
                raise ConflictError(f"task already exists: {task_id}")
            row = TaskRecord(task_id=task_id, case_id=case_id, title=title)
            session.add(row)
            session.commit()
            return {"task_id": row.task_id, "case_id": row.case_id, "title": row.title}

    def get(self, task_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            row = session.get(TaskRecord, task_id)
            if not row:
                raise NotFoundError(f"unknown task: {task_id}")
            return {
                "task_id": row.task_id,
                "case_id": row.case_id,
                "title": row.title,
                "status": row.status,
                "created_at": row.created_at,
            }

    def list(self) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = (
                session.execute(select(TaskRecord).order_by(TaskRecord.created_at)).scalars().all()
            )
            return [
                {
                    "task_id": r.task_id,
                    "case_id": r.case_id,
                    "title": r.title,
                    "status": r.status,
                    "created_at": r.created_at,
                }
                for r in rows
            ]


class RunService:
    """Append-only Run store with a validated status machine."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    def create_run(
        self,
        *,
        run_id: str,
        task_id: str,
        case_id: str,
        case_version: int,
        timeout_s: float = 60.0,
        worker_id: str = "",
    ) -> RunView:
        with self._sessions() as session:
            task = session.get(TaskRecord, task_id)
            if not task:
                raise NotFoundError(f"unknown task: {task_id}")
            attempt = session.execute(
                select(RunRecord.attempt)
                .where(RunRecord.task_id == task_id)
                .order_by(RunRecord.attempt.desc())
                .limit(1)
            ).scalar_one_or_none()
            next_attempt = 1 if attempt is None else int(attempt) + 1
            row = RunRecord(
                run_id=run_id,
                task_id=task_id,
                case_id=case_id,
                case_version=case_version,
                attempt=next_attempt,
                status=RunStatus.QUEUED,
                timeout_s=timeout_s,
                worker_id=worker_id,
            )
            session.add(row)
            session.commit()
            return _run_view(row)

    def transition(self, run_id: str, target: str, **fields: Any) -> RunView:
        """Validate and apply a state transition. Rejects illegal moves."""
        with self._sessions() as session:
            row = session.get(RunRecord, run_id)
            if not row:
                raise NotFoundError(f"unknown run: {run_id}")
            allowed = _ALLOWED.get(row.status, frozenset())
            if target != row.status and target not in allowed:
                raise ConflictError(
                    f"illegal run transition {row.status} -> {target}",
                    details={"run_id": run_id, "from": row.status, "to": target},
                )
            row.status = target
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            session.commit()
            return _run_view(row)

    def request_cancel(self, run_id: str) -> RunView:
        with self._sessions() as session:
            row = session.get(RunRecord, run_id)
            if not row:
                raise NotFoundError(f"unknown run: {run_id}")
            if row.status in RunStatus.TERMINAL:
                return _run_view(row)  # idempotent: no-op on terminal run
            row.cancel_requested = 1
            session.commit()
            return _run_view(row)

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._sessions() as session:
            row = session.get(RunRecord, run_id)
            return bool(row and row.cancel_requested)

    def heartbeat(self, run_id: str, worker_id: str = "") -> None:
        with self._sessions() as session:
            row = session.get(RunRecord, run_id)
            if row and row.status not in RunStatus.TERMINAL:
                row.heartbeat_at = _now()
                if worker_id:
                    row.worker_id = worker_id

    def finalize(
        self,
        run_id: str,
        *,
        status: str,
        execution_status: str,
        test_verdict: str,
        report: dict[str, Any],
        error: str = "",
    ) -> RunView:
        """Terminal write. Runs are immutable after this point."""
        with self._sessions() as session:
            row = session.get(RunRecord, run_id)
            if not row:
                raise NotFoundError(f"unknown run: {run_id}")
            if row.status in RunStatus.TERMINAL:
                raise ConflictError(f"run already finalized: {run_id}")
            row.status = status
            row.execution_status = execution_status
            row.test_verdict = test_verdict
            row.report = report
            row.error = error
            row.finished_at = _now()
            row.heartbeat_at = row.finished_at
            session.commit()
            return _run_view(row)

    def append_steps(self, run_id: str, steps: list[dict[str, Any]]) -> None:
        with self._sessions() as session:
            for i, step in enumerate(steps):
                session.add(
                    StepRecord(
                        run_id=run_id,
                        index=i,
                        step_id=str(step.get("step_id", "")),
                        action=str(step.get("action", "")),
                        ok=int(bool(step.get("ok"))),
                        detail=str(step.get("detail", ""))[:1000],
                    )
                )
            session.commit()

    def get(self, run_id: str) -> RunView:
        with self._sessions() as session:
            row = session.get(RunRecord, run_id)
            if not row:
                raise NotFoundError(f"unknown run: {run_id}")
            return _run_view(row)

    def report(self, run_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            row = session.get(RunRecord, run_id)
            if not row:
                raise NotFoundError(f"unknown run: {run_id}")
            if row.report is None:
                raise NotFoundError(f"run has no report yet: {run_id}")
            return row.report

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = (
                session.execute(
                    select(StepRecord).where(StepRecord.run_id == run_id).order_by(StepRecord.index)
                )
                .scalars()
                .all()
            )
            return [
                {
                    "index": r.index,
                    "step_id": r.step_id,
                    "action": r.action,
                    "ok": bool(r.ok),
                    "detail": r.detail,
                }
                for r in rows
            ]

    def history(self, task_id: str) -> list[RunView]:
        with self._sessions() as session:
            rows = (
                session.execute(
                    select(RunRecord)
                    .where(RunRecord.task_id == task_id)
                    .order_by(RunRecord.attempt)
                )
                .scalars()
                .all()
            )
            return [_run_view(r) for r in rows]


def create_task_tables(engine: Any) -> None:
    """Create only the task-domain tables on the given engine."""
    Base.metadata.create_all(
        engine,
        tables=[
            cast(Table[Any], TaskRecord.__table__),
            cast(Table[Any], RunRecord.__table__),
            cast(Table[Any], StepRecord.__table__),
        ],
        checkfirst=True,
    )
