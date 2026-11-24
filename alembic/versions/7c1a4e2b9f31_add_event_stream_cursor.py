"""add durable event stream cursor tables (R5-5.1)

Why a migration and not just ``create_all``: the platform schema is defined by
the migration chain (``alembic check`` enforces it). If the cursor tables only
existed because the app created them at startup, a database upgraded with
``alembic upgrade head`` would silently differ from a freshly created one, and
event replay would fail only in the upgraded environment.

Both tables are additive and hold no business data: ``ev_stream_cursor`` maps an
event to its durable order, ``ev_stream_state`` holds the retention watermark.
A downgrade drops the ordering index, not the events themselves; the positions
can be rebuilt from ``ev_event`` (``EventStore.backfill_stream_cursors``).

Revision ID: 7c1a4e2b9f31
Revises: 1d906be60a54
Create Date: 2026-11-24 19:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c1a4e2b9f31"
down_revision: str | None = "1d906be60a54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ev_stream_cursor",
        sa.Column("stream_seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("stream_seq"),
    )
    with op.batch_alter_table("ev_stream_cursor", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_ev_stream_cursor_event_id"), ["event_id"], unique=True
        )

    op.create_table(
        "ev_stream_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pruned_through", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ev_stream_state")
    with op.batch_alter_table("ev_stream_cursor", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ev_stream_cursor_event_id"))
    op.drop_table("ev_stream_cursor")
