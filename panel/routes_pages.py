"""Dashboard pages: the admin's control surface.

`GET /` and `GET /dashboard` render the same server-side shell (users, title,
xray banner state); all interactions then run through the Task 8 JSON API via
the inline script in dashboard.html. `GET /inbounds` (A3) is the inbound
editor page: server-rendered cards + the modal markup its script drives.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from panel import auth, db

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)

router = APIRouter()


def _dashboard(request: Request) -> HTMLResponse:
    manager = request.app.state.manager
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "users": db.list_users(),
            "title": settings.DASHBOARD_TITLE,
            "xray_up": manager.is_running(),
            "xray_error": manager.last_error,
        },
    )


def _inbounds(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    rows = db.list_inbounds()
    for row in rows:
        row["settings"] = json.loads(row["settings"] or "{}")
    return templates.TemplateResponse(
        request,
        "inbounds.html",
        {
            "inbounds": rows,
            "user_counts": db.count_users_per_inbound(),
            "title": settings.DASHBOARD_TITLE,
        },
    )


@router.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(auth.require_admin)],
)
async def dashboard_root(request: Request):
    return _dashboard(request)


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    dependencies=[Depends(auth.require_admin)],
)
async def dashboard_page(request: Request):
    return _dashboard(request)


@router.get(
    "/inbounds",
    response_class=HTMLResponse,
    dependencies=[Depends(auth.require_admin)],
)
async def inbounds_page(request: Request):
    return _inbounds(request)
