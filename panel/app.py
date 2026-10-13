"""Application factory: lifespan wiring, app state, and the global 401 handler."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from panel import bootstrap
from panel.config import Settings
from panel.ratelimit import RateLimiter
from panel.routes_auth import router as auth_router


async def handle_unauthenticated(
    request: Request, exc: HTTPException
) -> JSONResponse | RedirectResponse:
    """401s: JSON for /api, redirect for pages (/account back to /account)."""
    path = request.url.path
    if path.startswith("/api"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    if path.startswith("/account"):
        return RedirectResponse("/account", status_code=302)
    return RedirectResponse("/login", status_code=302)


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bootstrap.ensure_runtime(settings)
        app.state.settings = settings
        app.state.rate = RateLimiter()
        app.state.manager = None  # placeholder; wired up when task 5 lands
        yield

    app = FastAPI(lifespan=lifespan)
    app.add_exception_handler(401, handle_unauthenticated)
    app.include_router(auth_router)
    return app
