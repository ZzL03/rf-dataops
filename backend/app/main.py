import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.api.assets import router as assets_router
from app.api.auth import router as auth_router
from app.api.datasets import router as datasets_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.labeling import router as labeling_router
from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import build_engine, build_session_factory


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    """Build an isolated app; no database access or DDL occurs at import time."""
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=config.log_level, format="%(levelname)s %(name)s %(message)s")
        database = engine if engine is not None else build_engine(config)
        application.state.engine = database
        application.state.settings = config
        application.state.session_factory = build_session_factory(database)
        try:
            yield
        finally:
            if engine is None:
                database.dispose()

    application = FastAPI(
        title="RF-DataOps",
        version="0.1.0",
        description="Stage 5: reliable processing, human review and immutable dataset publication.",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    application.include_router(assets_router)
    application.include_router(jobs_router)
    application.include_router(auth_router)
    application.include_router(labeling_router)
    application.include_router(datasets_router)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI defaults can echo input, including a malformed password.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()
                ]
            },
        )

    @application.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": {}}},
            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
        )

    @application.exception_handler(OperationalError)
    async def database_unavailable(_request: Request, exc: OperationalError) -> JSONResponse:
        logging.getLogger(__name__).warning("Database request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "Database is temporarily unavailable",
                    "details": {},
                }
            },
        )

    return application
