from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings

SCHEMA_REVISION = "0005_datasets"


def build_engine(settings: Settings) -> Engine:
    """Each API/Worker process creates its own bounded connection pool."""
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=3,
        isolation_level="READ COMMITTED",
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.database_connect_timeout,
            "options": (
                f"-c statement_timeout={settings.database_statement_timeout_ms} -c timezone=UTC"
            ),
            "application_name": "rf-dataops",
        },
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Use factory.begin() per business transaction; never share sessions across threads."""
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)
