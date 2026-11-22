"""TeamFlow ORM models — the minimal被测应用 data domain.

Only the objects needed for the '创建项目' vertical slice exist:
Workspace, TestUser, Project. Ground Truth and fault configuration live in a
SEPARATE eval schema (apps/teamflow/eval_models.py) so the business/Agent plane
cannot read them via business queries.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Workspace(Base):
    __tablename__ = "tf_workspace"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    projects: Mapped[list[Project]] = relationship(back_populates="workspace")


class TestUser(Base):
    __tablename__ = "tf_test_user"
    __table_args__ = (UniqueConstraint("workspace_id", "username", name="uq_ws_username"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tf_workspace.id"), nullable=False
    )
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    # Demo only: plaintext marker. prod must store a hash (documented, not simulated).
    password: Mapped[str] = mapped_column(String(256), nullable=False)


class Project(Base):
    __tablename__ = "tf_project"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_ws_project_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tf_workspace.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    workspace: Mapped[Workspace] = relationship(back_populates="projects")
