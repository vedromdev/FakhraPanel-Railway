"""Application factory: lifespan wiring, app state, and the global 401 handler."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from panel import bootstrap, db, jobs
from panel.config import Settings
from panel.ratelimit import RateLimiter
from panel.routes_account import router as account_router
from panel.routes_admin import health_router, router as admin_router
from panel.routes_auth import router as auth_router
from panel.routes_inbounds import router as inbounds_router
from panel.routes_pages import router as pages_router
from panel.routes_sub import router as sub_router
from panel.xray.manager import XrayManager
from panel.xray.stats import StatsClient

logger = logging.getLogger(__name__)


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
        if not settings.PUBLIC_HOST.strip():
            # Fail-loud at boot (links.build_link still refuses at generation):
            # an unset host otherwise looks healthy until the first share
            # link or account-page request 500s.
            logger.error(
                "PUBLIC_HOST is empty — share links and the account page will "
                "fail until PUBLIC_HOST is set"
            )
        app.state.settings = settings
        app.state.rate = RateLimiter()
        # /sub/{token} pulls: 30 per 5 min per client — long-lived interval
        # clients re-fetch at human-ish cadences; scrapers get nowhere.
        app.state.sub_rate = RateLimiter(window_s=300, max_calls=30)
        manager = XrayManager(
            settings,
            public_host=settings.PUBLIC_HOST,
        )
        if manager.config_path.exists():
            manager.start()
        else:  # first boot: build a config from the (possibly empty) tables
            manager.apply_config(db.list_users(), db.list_inbounds())
        manager.start_monitor()
        app.state.manager = manager

        # One long-lived stats client for every tick (never per-tick).
        stats_client = StatsClient()
        app.state.stats = stats_client

        async def jobs_loop() -> None:
            while True:
                await asyncio.sleep(jobs.TICK_SECONDS)
                try:
                    # Off the event loop: a tick makes blocking gRPC calls
                    # (bounded, but the panel UI must stay responsive).
                    # CancelledError derives from BaseException, so shutdown
                    # cancellation passes through this catch untouched.
                    await asyncio.to_thread(
                        jobs.run_cycle, settings, manager, stats_client
                    )
                except Exception:  # a failing tick must never kill the loop
                    logger.exception("jobs tick failed; loop continues")

        jobs_task = asyncio.create_task(jobs_loop())
        app.state.jobs_task = jobs_task
        yield
        jobs_task.cancel()
        with suppress(asyncio.CancelledError):
            await jobs_task
        manager.stop()

    app = FastAPI(lifespan=lifespan)
    app.add_exception_handler(401, handle_unauthenticated)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "templates" / "static"),
        name="static",
    )
    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(pages_router)
    app.include_router(admin_router)
    app.include_router(inbounds_router)
    app.include_router(sub_router)
    app.include_router(health_router)
    return app
