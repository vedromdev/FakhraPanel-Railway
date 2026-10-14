"""Dashboard pages: the admin's control surface.

`GET /` and `GET /dashboard` render the same server-side shell (users, title,
xray banner state); all interactions then run through the Task 8 JSON API via
the inline script in dashboard.html.
"""

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
