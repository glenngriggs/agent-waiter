"""The runtime interface: how the platform starts agents and launches Codex.

DockerRuntime is the product. LocalRuntime exists only for development and
tests on machines without Docker; it gives agents no isolation.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict

from openai_codex import CodexConfig


class AgentRuntimeError(Exception):
    """A container operation failed. `message` is safe to show the user."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


# Raised when a launch is asked for an agent whose row is gone.
AGENT_DELETED_MESSAGE = "The agent was deleted."


class AgentRuntime(ABC):
    name: str = "abstract"

    def __init__(self) -> None:
        # One lock per agent so ensure_running/stop/restart never interleave.
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock_for(self, agent_id: str) -> asyncio.Lock:
        return self._locks[agent_id]

    @abstractmethod
    async def prepare(self) -> None:
        """One-time host setup (image, network). Idempotent."""

    @abstractmethod
    async def ensure_running(self, agent: dict) -> None:
        """Create or start the agent's container as needed."""

    @abstractmethod
    async def stop(self, agent_id: str) -> None: ...

    @abstractmethod
    async def restart(self, agent: dict) -> None:
        """Remove and recreate the container; bumps token_version."""

    @abstractmethod
    async def remove(self, agent_id: str) -> None: ...

    @abstractmethod
    async def status(self, agent_id: str) -> str:
        """'running', 'stopped', 'missing' or 'unknown'."""

    @abstractmethod
    def codex_config(self, agent: dict, codex_home_subdir: str | None = None) -> CodexConfig:
        """How to launch `codex app-server` (stdio) for this agent.

        `codex_home_subdir` (relative to the agent's codex-home) gives the
        process its own CODEX_HOME. Two app-server processes cannot share
        one CODEX_HOME (the second fails to open Codex's state database), so
        job runs and the probe use a scratch home while chat uses the main one.
        """

    @abstractmethod
    def workspace_cwd(self, agent: dict) -> str:
        """The workspace path as Codex sees it."""
