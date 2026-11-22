"""Single source of truth for platform migrations.

Alembic must see exactly the platform tables — no more. Two traps this module
avoids:

* The platform spans **four** declarative bases (core/tasks, evidence events,
  evidence records, evidence graph). An env.py that imports only one of them
  produces an incomplete initial migration.
* ``apps.teamflow`` models also register on the shared ``Base``, so a naive
  ``Base.metadata`` would drag TeamFlow's ``tf_*``/``eval_*`` tables into the
  platform migration and couple two independently deployed applications.

So the platform metadata is rebuilt explicitly from ``PLATFORM_TABLES``.

On foreign keys: the platform declares none, by design. ``tg_run`` is append-only
and evidence/graph rows reference runs that may live in another deployment, so a
hard FK would turn archival and cross-database migration into delete failures.
Referential integrity is enforced in the service layer instead; the migration
tests assert that the reference columns are indexed so those service-level joins
stay cheap. Introducing FKs is a schema-semantics decision that needs human
sign-off, not an autonomous refactor.
"""

from __future__ import annotations

from sqlalchemy import MetaData

from packages.core.database import Base
from packages.evidence.events import EventBase
from packages.evidence.graph import GraphBase
from packages.evidence.manager import EvidenceBase
from packages.execution.tasks import RunRecord, StepRecord, TaskRecord  # noqa: F401

# Tables this migration owns, in dependency order (parents first).
PLATFORM_TABLES: tuple[str, ...] = (
    "tg_task",
    "tg_run",
    "tg_step",
    "ev_producer_instance",
    "ev_event",
    "ev_inbox",
    "ev_audit",
    "evd_record",
    "gr_node",
    "gr_edge",
)


def _source_metadata() -> tuple[MetaData, ...]:
    return (Base.metadata, EventBase.metadata, EvidenceBase.metadata, GraphBase.metadata)


def build_platform_metadata() -> MetaData:
    """Copy only the owned tables into a dedicated MetaData."""
    target = MetaData()
    missing: list[str] = []
    for name in PLATFORM_TABLES:
        for source in _source_metadata():
            if name in source.tables:
                source.tables[name].to_metadata(target)
                break
        else:
            missing.append(name)
    if missing:  # importing this module should have registered them
        raise RuntimeError(f"declared platform tables not registered: {missing}")
    return target


PLATFORM_METADATA: list[MetaData] = [build_platform_metadata()]


def registered_table_names() -> set[str]:
    """Every table visible on the shared bases (platform + TeamFlow)."""
    names: set[str] = set()
    for meta in _source_metadata():
        names |= set(meta.tables)
    return names


def foreign_tables() -> set[str]:
    """Registered but NOT owned by the platform migration (TeamFlow's schema)."""
    return registered_table_names() - set(PLATFORM_TABLES)
