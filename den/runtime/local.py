"""LocalRuntime: run `codex app-server` directly on the host.

DEVELOPMENT AND TESTS ONLY. There is no container, so the model's shell has
the backend user's full access to this machine. It exists so the whole chain
(real app-server, real SDK, MCP endpoint, model proxy) runs on a machine
without Docker; it must be switched on explicitly with DEN_RUNTIME=local.

Isolation it does keep: Codex starts under `env -i` with only PATH, HOME,
CODEX_HOME and PLATFORM_TOKEN, so the backend's own environment (including
any real upstream API key) never reaches the Codex process.
"""

from __future__ import annotations

import logging
import os

from codex_cli_bin import bundled_codex_path, bundled_path_dir
from openai_codex import CodexConfig

from ..agent_files import agent_paths, init_agent_dirs, write_codex_config
from ..config import Settings
from ..db import Database
from ..security import make_agent_token
from .base import AgentRuntime

logger = logging.getLogger("den.runtime.local")


class LocalRuntime(AgentRuntime):
    name = "local"

    def __init__(self, settings: Settings, secret: bytes, db: Database) -> None:
        super().__init__()
        self.settings = settings
        self.secret = secret
        self.db = db

    def _fresh(self, agent: dict) -> dict:
        """Re-read the agent row so token_version and model are current.

        A caller may hold a row loaded before restart() bumped token_version;
        launching with that stale token would make every model and MCP call
        fail with 401.
        """
        if self.db is None:
            return agent
        return self.db.one("SELECT * FROM agents WHERE id = ?", (agent["id"],)) or agent

    def _codex_bin(self) -> str:
        return self.settings.codex_bin or str(bundled_codex_path())

    def _path_env(self) -> str:
        """PATH for the Codex process.

        When we launch the SDK's bundled binary, also prepend the bundled
        helper directory (it ships `rg`), exactly as the SDK does for its own
        default launch; `env -i` would otherwise drop it.
        """
        path = os.environ.get("PATH", os.defpath)
        if self.settings.codex_bin is None:
            helper_dir = bundled_path_dir()
            if helper_dir is not None:
                path = f"{helper_dir}{os.pathsep}{path}"
        return path

    def _prepare_files(self, agent: dict) -> None:
        paths = agent_paths(self.settings, agent["id"])
        init_agent_dirs(paths)
        write_codex_config(self.settings, paths, agent["model"])
        if self.settings.model_auth == "chatgpt":
            link = paths.codex_home / "auth.json"
            # lexists: a dangling link counts as present; leave it for the
            # operator rather than silently replacing it.
            if not os.path.lexists(link):
                os.symlink(self.settings.chatgpt_auth_json, link)
                logger.info("linked %s to the operator's ChatGPT login", link)

    async def prepare(self) -> None:
        return None  # nothing to build or create on the host

    async def ensure_running(self, agent: dict) -> None:
        async with self.lock_for(agent["id"]):
            self._prepare_files(self._fresh(agent))

    async def stop(self, agent_id: str) -> None:
        # Nothing long-lived to stop: each turn's app-server process belongs
        # to that turn's SDK client and ends when the client is closed.
        return None

    async def remove(self, agent_id: str) -> None:
        return None  # no container; lifecycle removes the agent's directory

    async def restart(self, agent: dict) -> None:
        """Revoke the old agent token; the next launch uses the new one."""
        async with self.lock_for(agent["id"]):
            self.db.execute(
                "UPDATE agents SET token_version = token_version + 1 WHERE id = ?",
                (agent["id"],),
            )
            self._prepare_files(self._fresh(agent))

    async def status(self, agent_id: str) -> str:
        return "running"  # there is no container that could be stopped

    def codex_config(self, agent: dict, codex_home_subdir: str | None = None) -> CodexConfig:
        agent = self._fresh(agent)
        paths = agent_paths(self.settings, agent["id"])
        codex_home = paths.codex_home / codex_home_subdir if codex_home_subdir else paths.codex_home
        token = make_agent_token(self.secret, agent["id"], int(agent["token_version"]))
        return CodexConfig(
            launch_args_override=(
                "env", "-i",
                f"PATH={self._path_env()}",
                f"HOME={paths.home}",
                f"CODEX_HOME={codex_home}",
                f"PLATFORM_TOKEN={token}",
                self._codex_bin(), "app-server", "--listen", "stdio://",
            ),
            cwd=str(paths.workspace),
        )

    def workspace_cwd(self, agent: dict) -> str:
        return str(agent_paths(self.settings, agent["id"]).workspace)
