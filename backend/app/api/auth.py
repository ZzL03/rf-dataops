from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, func, select

from app.core.errors import DomainError
from app.models import AuthSession, User
from app.schemas.labeling import LoginRequest
from app.services.authentication import login, token_hash

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def current_user(request: Request, credentials: Credentials) -> User:
    if credentials is None or len(credentials.credentials) > 200:
        raise DomainError("UNAUTHORIZED", "Login required", 401)
    with request.app.state.session_factory() as session:
        user = session.scalar(
            select(User)
            .join(AuthSession)
            .where(
                AuthSession.token_hash == token_hash(credentials.credentials),
                AuthSession.expires_at > func.clock_timestamp(),
                User.is_active.is_(True),
            )
        )
        if user is None:
            raise DomainError("UNAUTHORIZED", "Session is invalid or expired", 401)
        return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != "ADMIN":
        raise DomainError("FORBIDDEN", "Administrator role required", 403)
    return user


def require_reviewer(user: CurrentUser) -> User:
    if user.role not in {"ADMIN", "REVIEWER"}:
        raise DomainError("FORBIDDEN", "Reviewer role required", 403)
    return user


def require_publisher(user: CurrentUser) -> User:
    if user.role not in {"ADMIN", "RESEARCHER"}:
        raise DomainError("FORBIDDEN", "Researcher or administrator role required", 403)
    return user


@router.post("/token")
def access_token(body: LoginRequest, request: Request, response: Response):
    token = login(
        request.app.state.session_factory,
        body.username,
        body.password.get_secret_value(),
        request.app.state.settings.session_minutes,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": request.app.state.settings.session_minutes * 60,
    }


@router.get("/me")
def me(user: CurrentUser):
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/logout", status_code=204)
def logout(request: Request, credentials: Credentials, user: CurrentUser):
    with request.app.state.session_factory.begin() as session:
        session.execute(
            delete(AuthSession).where(
                AuthSession.token_hash == token_hash(credentials.credentials),
                AuthSession.user_id == user.id,
            )
        )
    return Response(status_code=204)
