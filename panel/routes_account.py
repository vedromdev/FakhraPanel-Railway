"""Self-service account area: user login/logout and the own-data page.

The only user id in play comes from the validated `kind="user"` session —
no route accepts one from the URL or body. `fakhra_user` and `fakhra_admin`
are separate namespaces (panel/db.py): an admin cookie never opens these
routes, and a user cookie never opens the admin API.
"""

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from panel import auth, db, jobs, links
from panel.ratelimit import rate_limit_key
from panel.routes_auth import (
    COOKIE_MAX_AGE,
    ERROR_INVALID,
    ERROR_RATE_LIMITED,
    SESSION_TTL_DAYS,
)

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)

router = APIRouter()


def _user_session(request: Request) -> dict | None:
    """Same lookup as auth.require_user, but None instead of a 401: the
    account page renders its own login form rather than redirecting."""
    try:
        return auth.require_user(request)
    except HTTPException:
        return None


def _login_page(request: Request, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "account_login.html",
        {"error": error},
        status_code=status_code,
    )


def _find_user(username: str) -> dict | None:
    for user in db.list_users():
        if user["username"] == username:
            return user
    return None


def _status(user: dict) -> str:
    """Computed account state, mirroring the jobs.evaluate precedence."""
    if user.get("enabled", 1):
        hits = jobs.evaluate([user], datetime.now(timezone.utc))
        reason = hits.get(user["id"])
    else:
        reason = user.get("disable_reason") or "disabled"
    if reason == "quota":
        return "limited"
    return reason or "active"


def _account_page(request: Request, session: dict) -> HTMLResponse:
    user = db.get_user(session["user_id"])
    if user is None:  # deleted since login: the session is worthless
        return _login_page(request)
    settings = request.app.state.settings
    link = links.build_link(user, settings.PUBLIC_HOST, db.read_meta("install_token"))
    # Exactly one context row (the brief's context rule): the caller's own,
    # carrying a computed status; the bcrypt hash is never referenced.
    user = {**user, "status": _status(user)}
    return templates.TemplateResponse(
        request,
        "account.html",
        {"user": user, "link": link, "qr": links.build_qr_data_uri(link),
         "title": "My Account"},
    )


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    session = _user_session(request)
    if session is None:
        return _login_page(request)
    return _account_page(request, session)


@router.post("/account/login", response_class=HTMLResponse)
async def account_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    ip = rate_limit_key(request)
    # Namespaced key: the account surface gets its own per-IP budget so failed
    # user logins can never lock the operator out of admin /login (and vice
    # versa); each surface stays throttled at the RateLimiter default.
    if not request.app.state.rate.allow(f"account-login:{ip}"):
        return _login_page(request, ERROR_RATE_LIMITED, status_code=429)
    user = _find_user(username)
    if user is None or not auth.verify_password(password, user["password"]):
        return _login_page(request, ERROR_INVALID)
    token = db.create_session("user", user["id"], ttl_days=SESSION_TTL_DAYS)
    response = RedirectResponse("/account", status_code=302)
    response.set_cookie(
        auth.USER_COOKIE,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.COOKIE_SECURE,
        path="/",
    )
    return response


@router.get("/account/logout")
async def account_logout(request: Request):
    token = request.cookies.get(auth.USER_COOKIE)
    if token:
        db.delete_session("user", token)
    response = RedirectResponse("/account", status_code=302)
    response.delete_cookie(auth.USER_COOKIE, path="/")
    return response
