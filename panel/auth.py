"""Admin/user authentication: credential check and session-cookie dependencies."""

import secrets

import bcrypt
from fastapi import HTTPException, Request

from panel import db
from panel.config import Settings

ADMIN_COOKIE = "fakhra_admin"
USER_COOKIE = "fakhra_user"


def hash_password(plain: str) -> str:
    """Bcrypt-hash a password (cost=12 default); returns the `$2b$...` string."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str | None) -> bool:
    """False for wrong passwords and for malformed/None hashes (never raises)."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def authenticate_admin(username: str, password: str, settings: Settings) -> bool:
    """Constant-time comparison of both fields against the env-sourced secrets."""
    username_ok = secrets.compare_digest(
        username.encode("utf-8"), settings.ADMIN_USERNAME.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        password.encode("utf-8"), settings.ADMIN_PASSWORD.encode("utf-8")
    )
    return username_ok and password_ok


def require_admin(request: Request) -> dict:
    """FastAPI dependency: 401 unless the admin cookie session is valid."""
    token = request.cookies.get(ADMIN_COOKIE)
    session = db.get_session("admin", token) if token else None
    if session is None:
        raise HTTPException(status_code=401)
    return session


def require_user(request: Request) -> dict:
    """FastAPI dependency: 401 unless the user cookie session is valid."""
    token = request.cookies.get(USER_COOKIE)
    session = db.get_session("user", token) if token else None
    if session is None:
        raise HTTPException(status_code=401)
    return session
