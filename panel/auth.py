"""Admin/user authentication: credential check and session-cookie dependencies."""

import secrets

from fastapi import HTTPException, Request

from panel import db
from panel.config import Settings

ADMIN_COOKIE = "fakhra_admin"
USER_COOKIE = "fakhra_user"


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
