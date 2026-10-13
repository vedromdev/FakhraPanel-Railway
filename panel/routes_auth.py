"""Admin auth routes: login, logout, and unauthenticated placeholders."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from panel import auth, db

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)

router = APIRouter()

SESSION_TTL_DAYS = 7
COOKIE_MAX_AGE = 604800  # 7 days, matches SESSION_TTL_DAYS
ERROR_INVALID = "Invalid username or password"
ERROR_RATE_LIMITED = "Too many attempts. Please try again later."


def _login_page(request: Request, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _login_page(request)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    settings = request.app.state.settings
    ip = request.client.host if request.client else "unknown"
    if not request.app.state.rate.allow(ip):
        return _login_page(request, ERROR_RATE_LIMITED, status_code=429)
    if not auth.authenticate_admin(username, password, settings):
        return _login_page(request, ERROR_INVALID)
    token = db.create_session("admin", None, ttl_days=SESSION_TTL_DAYS)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        auth.ADMIN_COOKIE,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.ADMIN_COOKIE)
    if token:
        db.delete_session("admin", token)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(auth.ADMIN_COOKIE, path="/")
    return response


@router.get("/", dependencies=[Depends(auth.require_admin)])
async def root():
    # Placeholder until the dashboard route lands.
    return JSONResponse({"detail": "Fakhra Panel"})


@router.get("/api/users", dependencies=[Depends(auth.require_admin)])
async def list_users_placeholder():
    # Placeholder until the real user API lands.
    return {"users": []}
