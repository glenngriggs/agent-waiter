"""AppContext: the one object routers and background tasks share."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Coroutine

from fastapi import Request

from .config import Settings
from .db import Database
from .events import EventBus, emit
from .security import LoginLimiter, make_agent_token

if TYPE_CHECKING:  # imported only for type hints to avoid import cycles
    import httpx

    from .codex_bridge import TurnRegistry
    from .runtime.base import AgentRuntime
    from .scheduler import Scheduler

logger = logging.getLogger("den")


@dataclass
class AppContext:
    settings: Settings
    db: Database
    bus: EventBus
    runtime: "AgentRuntime"
    secret: bytes
    turns: "TurnRegistry"
    scheduler: "Scheduler | None" = None
    login_limiter: LoginLimiter = field(default_factory=LoginLimiter)
    http: "httpx.AsyncClient | None" = None
    background: set[asyncio.Task] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """Run a coroutine in the background, keep it referenced, log failures."""
        task = asyncio.create_task(coro, name=name)
        self.background.add(task)

        def _done(t: asyncio.Task) -> None:
            self.background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("background task %s failed", name, exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    def agent_token(self, agent: dict) -> str:
        return make_agent_token(self.secret, agent["id"], int(agent["token_version"]))

    def emit(
        self,
        *,
        agent_id: str,
        turn_id: str | None,
        channel: str,
        kind: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Shortcut for events.emit with this context's db and bus."""
        return emit(
            self.db,
            self.bus,
            agent_id=agent_id,
            turn_id=turn_id,
            channel=channel,
            kind=kind,
            data=data,
        )

    def get_agent(self, agent_id: str) -> dict | None:
        return self.db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx
