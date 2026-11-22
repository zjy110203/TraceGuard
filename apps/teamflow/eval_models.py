"""Eval-plane data models: fault config, scenarios and Ground Truth.

ISOLATION CONTRACT (v1.1 stage 1):
- These tables are NEVER queried by business-plane endpoints or the Web UI.
- They are reachable only through /__eval__/* with the X-Eval-Token header.
- The Agent / Playwright must not hold the token; hence cannot read Ground Truth.
Keeping them in separate tables (not just separate routes) makes accidental
leakage through a broad "select all" business query structurally unlikely.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FaultConfig(Base):
    """Currently active fault for one mechanism (one active row per fault_id)."""

    __tablename__ = "eval_fault_config"

    fault_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    # JSON-encoded params (e.g. delay_ms, http_status, message_override).
    params_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Scenario(Base):
    """Benchmark scenario metadata (C12 base). Recorded from stage 1 onward."""

    __tablename__ = "eval_scenario"

    scenario_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    fault_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON: fault injection config, data seed, cleanup method, teamflow version.
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GroundTruth(Base):
    """Expected real business state for a scenario. Eval-plane only."""

    __tablename__ = "eval_ground_truth"

    scenario_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    # JSON: expected project count, expected status, expected verdict, notes.
    expected_json: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EventLog(Base):
    """Append-only eval event trace — evidence of what the fault engine did.

    Not a tamper-proof Evidence store (that is stage 4); a simple diagnostic log
    for verifying that fault injection actually fired during experiments.
    """

    __tablename__ = "eval_event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scenario_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    fault_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
