"""Password hashing and revocable opaque sessions; no credentials in logs."""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select

from app.core.errors import DomainError
from app.models import AuthSession, LoginBucket, User
from app.services.job_queue import advisory_lock

ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise DomainError("INVALID_PASSWORD", "Password must contain 12-128 characters")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


DUMMY_HASH = hash_password("dummy-password-not-an-account")


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(iterations) != ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(session, user_id, minutes=30) -> str:
    token = "rfu_" + secrets.token_urlsafe(32)
    now = session.scalar(select(func.clock_timestamp()))
    session.add(
        AuthSession(
            token_hash=token_hash(token),
            user_id=user_id,
            expires_at=now + timedelta(minutes=minutes),
        )
    )
    return token


def create_user(factory, username: str, password: str, role: str) -> User:
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", username) or role not in {
        "ADMIN",
        "RESEARCHER",
        "REVIEWER",
    }:
        raise DomainError("INVALID_USER", "Invalid username or role")
    encoded = hash_password(password)
    with factory.begin() as session:
        advisory_lock(session, "username:" + username)
        if session.scalar(select(User.id).where(User.username == username)):
            raise DomainError("USER_EXISTS", "Username already exists", 409)
        user = User(username=username, password_hash=encoded, role=role)
        session.add(user)
        session.flush()
        return user


def login(factory, username: str, password: str, minutes: int) -> str:
    username = username.strip().lower()
    key = token_hash("login:" + username)
    # Reserve a rate-limit attempt in a short DB transaction before expensive hashing.
    with factory.begin() as session:
        advisory_lock(session, "login:" + key)
        now = session.scalar(select(func.clock_timestamp()))
        session.execute(
            delete(LoginBucket).where(LoginBucket.window_start < now - timedelta(hours=1))
        )
        session.execute(delete(AuthSession).where(AuthSession.expires_at < now))
        bucket = session.get(LoginBucket, key)
        if bucket is None:
            bucket = LoginBucket(key=key, window_start=now, attempts=0)
            session.add(bucket)
        elif now >= bucket.window_start + timedelta(seconds=60):
            bucket.window_start, bucket.attempts = now, 0
        if bucket.attempts >= 5:
            raise DomainError("LOGIN_RATE_LIMIT", "Too many login attempts; try again later", 429)
        bucket.attempts += 1
        user = session.scalar(select(User).where(User.username == username))
    valid = verify_password(password, user.password_hash if user else DUMMY_HASH)
    if not valid or user is None or not user.is_active:
        raise DomainError("UNAUTHORIZED", "Invalid username or password", 401)
    with factory.begin() as session:
        # Recheck account state after password computation; disabling is immediate for reads too.
        current = session.get(User, user.id, with_for_update=True)
        if not current.is_active or current.password_hash != user.password_hash:
            raise DomainError("UNAUTHORIZED", "Invalid username or password", 401)
        return issue_session(session, user.id, minutes)
