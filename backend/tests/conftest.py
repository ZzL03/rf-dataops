import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.db.session import build_engine

BACKEND = Path(__file__).resolve().parents[1]


def migration_config() -> Config:
    return Config(str(BACKEND / "alembic.ini"))


@pytest.fixture(scope="session")
def postgres_database() -> Iterator[tuple[Settings, Engine]]:
    """Create/drop only a unique test database; never migrate the development database."""
    prefix = os.environ.get("RF_TEST_DATABASE_NAME")
    if prefix is None:
        pytest.skip("Set RF_TEST_DATABASE_NAME=rf_dataops_test to enable real PostgreSQL tests")
    if not re.fullmatch(r"rf_dataops_test[a-z0-9_]{0,12}", prefix):
        pytest.fail("RF_TEST_DATABASE_NAME must start with rf_dataops_test (max 27 characters)")
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    settings = Settings()
    maintenance = create_engine(
        settings.database_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
        hide_parameters=True,
    )
    created = False
    engine: Engine | None = None
    try:
        with maintenance.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("RF_DATABASE_NAME", name)
            isolated = Settings()
            command.upgrade(migration_config(), "head")
            engine = build_engine(isolated)
            yield isolated, engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with maintenance.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        maintenance.dispose()
