"""SQLAlchemy 2.0 database base — SQLite default, PostgreSQL switchable.

Design rationale (see docs/架构说明.md): v1.1 targets PostgreSQL, but stage 0
acceptance requires a reproducible, startable environment. We keep the ORM layer
dialect-neutral so DATABASE_URL can switch to PostgreSQL without code changes;
SQLite foreign keys are explicitly enabled to preserve integrity locally.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Declarative base shared by platform and TeamFlow models."""


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def create_db_engine(database_url: str) -> Engine:
    # Ensure the parent directory for a file-based SQLite DB exists.
    if _is_sqlite(database_url) and ":///" in database_url:
        db_path = database_url.split(":///", 1)[1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if _is_sqlite(database_url) else {}
    # In-memory SQLite: every connection would otherwise get its own private DB,
    # so tables created at startup vanish for request sessions. StaticPool makes
    # all connections share one in-memory database (used in tests).
    pool_kwargs = {}
    if _is_sqlite(database_url) and ":memory:" in database_url:
        pool_kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, connect_args=connect_args, future=True, **pool_kwargs)

    if _is_sqlite(database_url):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session(engine: Engine) -> Generator[Session, None, None]:
    """FastAPI dependency factory; caller binds the engine."""
    factory = make_session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
