"""Initialise the database schema and seed baseline data.

Usage: python -m scripts.init_db
Creates all tables for both business and eval planes and seeds the default
workspace + test account. Idempotent.
"""

from __future__ import annotations

from apps.teamflow import service
from apps.teamflow.eval_models import Base as EvalBase
from apps.teamflow.models import Base as BusinessBase
from packages.core import database
from packages.core.config import get_settings


def main() -> None:
    settings = get_settings()
    engine = database.create_db_engine(settings.database_url)
    BusinessBase.metadata.create_all(engine)
    EvalBase.metadata.create_all(engine)
    factory = database.make_session_factory(engine)
    with factory() as session:
        service.seed_default(session, settings.teamflow_test_user, settings.teamflow_test_password)
    engine.dispose()
    print(f"OK: schema created and seeded -> {settings.database_url}")


if __name__ == "__main__":
    main()
