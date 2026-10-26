import uuid

from fastapi.testclient import TestClient

from app.db.session import build_session_factory
from app.main import create_app
from app.models import User
from app.services.authentication import issue_session


def session_header(engine, role="ADMIN"):
    with build_session_factory(engine).begin() as session:
        user = User(
            username="test_" + uuid.uuid4().hex, password_hash="not-for-password-login", role=role
        )
        session.add(user)
        session.flush()
        return {"Authorization": "Bearer " + issue_session(session, user.id)}


def user_client(settings, engine):
    # Earlier asset tests focus on IQ behavior; real DB sessions enforce auth without password cost.
    return TestClient(create_app(settings, engine=engine), headers=session_header(engine))
