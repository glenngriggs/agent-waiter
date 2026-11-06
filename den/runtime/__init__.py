"""Pick the agent runtime named in the settings."""

from __future__ import annotations

from ..config import Settings
from ..db import Database
from .base import AgentRuntime, AgentRuntimeError

__all__ = ["AgentRuntime", "AgentRuntimeError", "make_runtime"]


def make_runtime(settings: Settings, secret: bytes, db: Database) -> AgentRuntime:
    if settings.runtime == "docker":
        from .docker import DockerRuntime

        return DockerRuntime(settings, secret, db)
    from .local import LocalRuntime

    return LocalRuntime(settings, secret, db)
