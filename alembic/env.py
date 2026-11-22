"""Alembic environment for the platform database.

URL resolution order (highest first):
1. ``TRACEGUARD_DATABASE_URL`` — lets tests and tooling point at a throwaway file;
2. ``Settings.database_url`` — the same value the app itself uses, so a migration
   can never silently target a different database than the running service.

The static ``sqlalchemy.url`` in ``alembic.ini`` is intentionally NOT consulted
at runtime; it exists so ``alembic history`` works without an app context. Tests
must therefore set ``TRACEGUARD_DATABASE_URL`` rather than editing the ini.

``render_as_batch`` is enabled for SQLite so future ALTER-style migrations work
without hand-written table rebuilds; PostgreSQL does not need it.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.core.config import get_settings  # noqa: E402
from packages.core.metadata import PLATFORM_METADATA  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = PLATFORM_METADATA


def database_url() -> str:
    override = os.environ.get("TRACEGUARD_DATABASE_URL")
    if override:
        return override
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
