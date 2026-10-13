"""Application factory: lifespan wiring, app state, and the global 401 handler."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from panel import bootstrap, db, jobs
from panel.config import Settings
from panel.ratelimit import RateLimiter
from panel.routes_auth import router as auth_router
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
        app.state.settings = settings
        app.state.rate = RateLimiter()
        manager = XrayManager(
            settings,
            install_token=db.get_or_create_meta(
                "install_token", lambda: secrets.token_hex(16)
            ),
            public_host=settings.PUBLIC_HOST,
        )
        if manager.config_path.exists():
            manager.start()
        else:  # first boot: build a config from the (possibly empty) user list
            manager.apply_config(db.list_users())
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
    app.include_router(auth_router)
    return app
