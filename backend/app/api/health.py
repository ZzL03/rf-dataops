import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SCHEMA_REVISION

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/live")
def live() -> dict[str, str]:
    """Liveness deliberately has no database dependency."""
    return {"status": "ok", "service": "rf-dataops", "version": "0.1.0"}


@router.get("/ready", responses={503: {"description": "Database or schema unavailable"}})
def ready(request: Request) -> JSONResponse:
    """Require both a working connection and the exact expected migration head."""
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            has_version = connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
            if has_version is None:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "code": "SCHEMA_NOT_READY"},
                )
            revisions = set(
                connection.scalars(text("SELECT version_num FROM public.alembic_version"))
            )
            if revisions != {SCHEMA_REVISION}:
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "code": "SCHEMA_NOT_READY"},
                )
    except SQLAlchemyError as exc:
        # Driver messages can contain connection details; log only the exception class.
        logger.warning("Readiness database check failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "code": "DATABASE_UNAVAILABLE"},
        )
    return JSONResponse(content={"status": "ok", "database": "ok", "schema": SCHEMA_REVISION})
