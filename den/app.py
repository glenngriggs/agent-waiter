"""The FastAPI application: startup/shutdown wiring, CSRF check, routers, static UI.

Everything stateful (database, event bus, runtime, running turns, scheduler,
the model proxy's HTTP client) is built in the lifespan and hung on one
AppContext, so tests can run several independent apps in one process.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import account, agents, anthropic_bridge, auth, chat, files, jobs, lifecycle, mcp_server, model_proxy
from . import codex_bridge
from .codex_bridge import TurnRegistry
from .config import Settings, load_settings
from .context import AppContext
from .db import Database
from .events import EventBus
from .runtime import make_runtime
from .runtime.base import AgentRuntimeError
from .scheduler import Scheduler
from .security import load_or_create_secret

logger = logging.getLogger("den.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_EXECUTOR_WORKERS = 64
SHUTDOWN_GRACE_SECONDS = 10.0
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SETUP_CUT_OFF_DETAIL = "setup: the platform restarted during setup; press Retry"


class CSRFMiddleware:
    """Reject state-changing /api requests that lack `X-Den-CSRF: 1`.

    A cross-site form or image tag cannot set a custom header, so together
    with the SameSite=Lax cookie this blocks cross-site request forgery.
    Written as plain ASGI (not BaseHTTPMiddleware) so it never wraps or
    buffers the SSE stream.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"].startswith("/api/")
            and scope["method"] not in SAFE_METHODS
            and dict(scope["headers"]).get(b"x-den-csrf") != b"1"
        ):
            response = JSONResponse({"detail": "Missing CSRF header."}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _RevalidatingStatic(StaticFiles):
    """StaticFiles with `Cache-Control: no-cache`, so browsers revalidate (via
    ETag) on every load instead of running a stale app.js after an update."""

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _validation_message(exc: RequestValidationError) -> str:
    """Turn pydantic's error list into one sentence for `{"detail": "..."}`."""
    errors = exc.errors()
    if not errors:
        return "The request is not valid."
    first = errors[0]
    # loc is like ("body", "name") or ("query", "limit"); drop the section name.
    where = ".".join(str(p) for p in first.get("loc", ())[1:]) or "request"
    return f"Invalid {where}: {first.get('msg', 'not valid')}."


async def _prepare_runtime(ctx: AppContext) -> None:
    """Build the image / network ahead of the first agent, if possible.

    A failure here is not fatal: ensure_running calls prepare() again on
    first use and reports the error to the user then.
    """
    try:
        await ctx.runtime.prepare()
    except AgentRuntimeError as exc:
        logger.warning(
            "runtime prepare failed (will retry on first use): %s | %s", exc.message, exc.detail
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Each streaming Codex turn holds a worker thread in the SDK, and the
        # stock default executor is small, so give the loop a bigger one. The
        # loop's owner (asyncio.run / uvicorn) shuts it down when the loop ends.
        loop = asyncio.get_running_loop()
        loop.set_default_executor(
            ThreadPoolExecutor(max_workers=DEFAULT_EXECUTOR_WORKERS, thread_name_prefix="den")
        )

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.data_dir / "den.db")
        db.migrate()
        secret = load_or_create_secret(settings.data_dir)
        bus = EventBus()
        runtime = make_runtime(settings, secret, db)
        ctx = AppContext(
            settings=settings,
            db=db,
            bus=bus,
            runtime=runtime,
            secret=secret,
            turns=TurnRegistry(),
        )
        app.state.ctx = ctx

        # Restart recovery: anything left `running` by a dead process is
        # marked interrupted before the loop can fire new runs.
        scheduler = Scheduler(ctx)
        ctx.scheduler = scheduler
        scheduler.recover_on_startup()
        scheduler.start()
        # Chat messages that were waiting in a queue when the platform stopped
        # still belong to the user: start them again.
        resumed = codex_bridge.resume_queues(ctx)
        if resumed:
            logger.info("resumed the chat queue of %d agent(s)", resumed)

        # Setup cut off by a restart would otherwise stay "creating" forever.
        cur = db.execute(
            "UPDATE agents SET setup_state = 'error', setup_detail = ? "
            "WHERE setup_state = 'creating'",
            (SETUP_CUT_OFF_DETAIL,),
        )
        if cur.rowcount:
            logger.warning("%d agent(s) were mid-setup at restart; marked as error", cur.rowcount)

        prepare_task = ctx.spawn(_prepare_runtime(ctx), name="runtime-prepare")
        reaper_task = ctx.spawn(lifecycle.idle_reaper(ctx), name="idle-reaper")
        ctx.http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10)
        )
        logger.info(
            "AgentWaiter started: runtime=%s model_auth=%s data_dir=%s",
            settings.runtime, settings.model_auth, settings.data_dir,
        )
        try:
            yield
        finally:
            await _shutdown(ctx, scheduler, prepare_task, reaper_task)

    app = FastAPI(title="AgentWaiter", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(CSRFMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # The API promises `{"detail": "<sentence>"}` with 400 for bad input;
        # FastAPI's default is a 422 with a list. Non-API routes keep the default.
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": _validation_message(exc)}, status_code=400)
        return await request_validation_exception_handler(request, exc)

    for module in (auth, account, agents, chat, files, jobs, mcp_server, model_proxy):
        app.include_router(module.router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        # no-store so a new deploy's page (and its script tags) load at once.
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")
    return app


async def _shutdown(
    ctx: AppContext, scheduler: Scheduler, prepare_task: asyncio.Task, reaper_task: asyncio.Task
) -> None:
    """Stop the scheduler, then turns, then background tasks, then I/O.

    The HTTP client and database close last because stopping turns and runs
    still writes their final rows (and a turn may be mid-call to the proxy).
    """
    # These two never finish on their own (the reaper loops; prepare may be in
    # a long docker build), so cancel them instead of waiting.
    for task in (reaper_task, prepare_task):
        task.cancel()
    await scheduler.stop()
    # Stopping the runners lets each one record its turn as interrupted.
    await ctx.turns.stop_all()
    pending = [t for t in ctx.background if not t.done()]
    if pending:
        # Give turn and run tasks a moment to write their final rows before
        # cancelling whatever is still going (for example a setup's probe).
        _, still = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in still:
            logger.warning("cancelling background task %s at shutdown", task.get_name())
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    await ctx.http.aclose()
    await anthropic_bridge.close_clients()
    ctx.db.close()
    logger.info("AgentWaiter stopped")
