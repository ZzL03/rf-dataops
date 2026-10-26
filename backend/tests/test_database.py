import uuid

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.session import SCHEMA_REVISION, build_session_factory
from app.main import create_app
from app.models import User
from tests.conftest import migration_config

pytestmark = pytest.mark.integration


def test_migration_round_trip_and_schema_readiness(
    postgres_database: tuple[Settings, Engine],
) -> None:
    settings, engine = postgres_database
    config = migration_config()
    assert ScriptDirectory.from_config(config).get_heads() == [SCHEMA_REVISION]
    with TestClient(create_app(settings, engine=engine)) as client:
        assert client.get("/health/ready").status_code == 200
        command.downgrade(config, "base")
        assert "users" not in inspect(engine).get_table_names()
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "SCHEMA_NOT_READY"
        command.upgrade(config, "head")
        # Repeating upgrade must be safe and leave ORM metadata in sync.
        command.upgrade(config, "head")
        command.check(config)
        assert client.get("/health/ready").json()["schema"] == SCHEMA_REVISION


def test_session_commit_and_rollback(postgres_database: tuple[Settings, Engine]) -> None:
    _, engine = postgres_database
    factory = build_session_factory(engine)
    user_id = uuid.uuid4()
    with factory.begin() as session:
        session.add(
            User(
                id=user_id, username="transaction_user", password_hash="test_hash", role="REVIEWER"
            )
        )
    with factory() as session:
        user = session.scalar(select(User).where(User.id == user_id))
        assert user is not None
        assert user.created_at.utcoffset().total_seconds() == 0
    rollback_id = uuid.uuid4()
    with pytest.raises(RuntimeError), factory.begin() as session:
        session.add(
            User(id=rollback_id, username="rollback_user", password_hash="test_hash", role="ADMIN")
        )
        session.flush()
        raise RuntimeError("fault injected before commit")
    with factory() as session:
        assert session.get(User, rollback_id) is None


@pytest.mark.parametrize(
    ("username", "role"),
    [("reviewer_a", "REVIEWER"), ("new_user", "SUPERUSER"), ("MixedCase", "ADMIN")],
)
def test_database_enforces_identity_constraints(
    postgres_database: tuple[Settings, Engine], username: str, role: str
) -> None:
    _, engine = postgres_database
    # Seed the unique-name case in this test, independent of test ordering.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES (:id, 'reviewer_a', 'test_hash', 'REVIEWER') "
                "ON CONFLICT (username) DO NOTHING"
            ),
            {"id": uuid.uuid4()},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, username, password_hash, role) VALUES (:id, :u, 'x', :r)"),
            {"id": uuid.uuid4(), "u": username, "r": role},
        )
