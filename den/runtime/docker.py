"""DockerRuntime: one container per agent, driven through the `docker` CLI.

Every docker operation is a CLI call through asyncio subprocesses (not
docker-py), so there is one mechanism for both container management and the
`docker exec` that launches Codex, and every command shows up in the logs.

The container itself holds nothing that matters: the workspace and
CODEX_HOME are bind mounts of `data/agents/<id>/...`, so a container can be
removed and recreated at any time without losing files or conversations.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from openai_codex import CodexConfig

from ..agent_files import agent_paths, init_agent_dirs, write_codex_config
from ..config import Settings
from ..db import Database
from ..security import make_agent_token
from .base import AgentRuntime, AgentRuntimeError

logger = logging.getLogger("den.runtime.docker")

# Raw `docker inspect` states that mean "exists but is not running".
_STOPPED_STATES = {"created", "exited", "paused", "dead"}

# Fragments of docker CLI stderr meaning the daemon itself is unreachable,
# as opposed to one command failing. "could not be found in this WSL 2
# distro" is what Docker Desktop's stub prints when WSL integration is off.
_DAEMON_DOWN_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "is the docker daemon running",
    "permission denied while trying to connect to the docker daemon",
    "could not be found in this wsl",
)

_BUILD_TIMEOUT_SECONDS = 900


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _is_missing(stderr: str) -> bool:
    """True when docker says the container does not exist."""
    lowered = stderr.lower()
    return "no such object" in lowered or "no such container" in lowered


def _mount_field(key: str, value: str) -> str:
    """One `key=value` field of a `--mount` spec.

    Docker parses `--mount` as a CSV record, so a path containing a comma or
    a double quote would split the field; CSV-quote it in that case.
    """
    field = f"{key}={value}"
    if "," in field or '"' in field:
        return '"' + field.replace('"', '""') + '"'
    return field


def _bind_mount(src: Path | str, dst: str, *, readonly: bool = False) -> str:
    fields = ["type=bind", _mount_field("src", os.path.abspath(str(src))), _mount_field("dst", dst)]
    if readonly:
        fields.append("readonly")
    return ",".join(fields)


def _redact(args: tuple[str, ...] | list[str]) -> str:
    """The command line for logs, with the agent token hidden."""
    shown = []
    for arg in args:
        if arg.startswith("PLATFORM_TOKEN="):
            arg = "PLATFORM_TOKEN=***"
        shown.append(arg)
    return " ".join(shown)


class DockerRuntime(AgentRuntime):
    name = "docker"

    def __init__(self, settings: Settings, secret: bytes, db: Database) -> None:
        super().__init__()
        self.settings = settings
        self.secret = secret
        self.db = db
        self.prepared = False
        self._prepare_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # docker CLI plumbing

    def container_name(self, agent_id: str) -> str:
        return f"den-agent-{agent_id}"

    async def _docker(
        self, *args: str, check: bool = True, timeout: float = 120
    ) -> tuple[int, str, str]:
        """Run `docker <args>` and return (exit code, stdout, stderr).

        Raises AgentRuntimeError when docker is missing or its daemon is
        unreachable (always, whatever `check` says, because no caller can do
        anything useful with that), on timeout, and on a non-zero exit when
        `check` is true.
        """
        # `inspect` runs for every AgentOut, so keep it out of the info log.
        level = logging.DEBUG if "inspect" in args[:2] else logging.INFO
        logger.log(level, "docker %s", _redact(args))
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            reason = (
                "the docker command was not found"
                if isinstance(exc, FileNotFoundError)
                else (exc.strerror or str(exc))
            )
            raise AgentRuntimeError(
                f"Docker is not available on the server: {reason}.", str(exc)
            ) from exc

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException as exc:
            # Timeout or task cancellation: never leave a docker client
            # process behind (a cancelled `docker build` would otherwise keep
            # running unobserved).
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if isinstance(exc, TimeoutError):
                raise AgentRuntimeError(
                    f"Docker did not answer within {timeout:g} seconds (docker {args[0]}).",
                    _redact(args),
                ) from exc
            raise

        rc = proc.returncode if proc.returncode is not None else -1
        out = out_b.decode("utf-8", errors="replace")
        err = err_b.decode("utf-8", errors="replace")
        if rc != 0:
            lowered = err.lower()
            if any(marker in lowered for marker in _DAEMON_DOWN_MARKERS):
                raise AgentRuntimeError(
                    f"Docker is not available on the server: {_first_line(err)}", err[-2000:]
                )
            if check:
                raise AgentRuntimeError(
                    _first_line(err) or f"docker {args[0]} failed with exit code {rc}",
                    (err or out)[-2000:],
                )
        return rc, out, err

    async def _state(self, agent_id: str) -> str | None:
        """The raw `.State.Status` of the agent's container, or None if missing."""
        rc, out, err = await self._docker(
            "inspect", "-f", "{{.State.Status}}", self.container_name(agent_id),
            check=False, timeout=30,
        )
        if rc == 0:
            return out.strip()
        if _is_missing(err):
            return None
        raise AgentRuntimeError(
            _first_line(err) or f"docker inspect failed with exit code {rc}", err[-2000:]
        )

    def _fresh(self, agent: dict) -> dict:
        """Re-read the agent row so token_version and model are current.

        Callers may hold a row loaded before a restart bumped token_version;
        baking that stale token into a new container would make every model
        and MCP call fail with 401.
        """
        if self.db is None:
            return agent
        return self.db.one("SELECT * FROM agents WHERE id = ?", (agent["id"],)) or agent

    # ------------------------------------------------------------------
    # AgentRuntime interface

    async def prepare(self) -> None:
        """Build the image and create the network if they are missing.

        The flag is set only on success, so a failure (say, Docker was off
        at startup) is retried on the next ensure_running.
        """
        async with self._prepare_lock:
            if self.prepared:
                return
            s = self.settings
            rc, _, _ = await self._docker("image", "inspect", s.agent_image, check=False, timeout=60)
            if rc != 0:
                logger.info("agent image %s is missing; building it from %s", s.agent_image, s.container_dir)
                try:
                    await self._docker(
                        "build", "-t", s.agent_image, str(s.container_dir),
                        timeout=_BUILD_TIMEOUT_SECONDS,
                    )
                except AgentRuntimeError as exc:
                    # BuildKit's first stderr line is progress chatter
                    # ("#0 building with ..."); the real cause is an ERROR line.
                    lines = [ln.strip() for ln in exc.detail.splitlines() if ln.strip()]
                    cause = next((ln for ln in reversed(lines) if "ERROR" in ln), exc.message)
                    raise AgentRuntimeError(
                        f"Could not build the agent image {s.agent_image}: {cause}", exc.detail
                    ) from exc

            rc, _, _ = await self._docker(
                "network", "inspect", s.docker_network, check=False, timeout=30
            )
            if rc != 0:
                # enable_icc=false: agents on this network cannot reach each
                # other; they can still reach the backend via host-gateway.
                rc, _, err = await self._docker(
                    "network", "create", "--driver", "bridge",
                    "-o", "com.docker.network.bridge.enable_icc=false",
                    s.docker_network,
                    check=False, timeout=60,
                )
                if rc != 0 and "already exists" not in err.lower():
                    raise AgentRuntimeError(
                        f"Could not create the Docker network {s.docker_network}: {_first_line(err)}",
                        err[-2000:],
                    )
            self.prepared = True

    def run_args(self, agent: dict) -> list[str]:
        """The `docker run` arguments for an agent (pure; unit tested).

        Every isolation and limit flag from the design lives here. The real
        upstream API key is deliberately absent: the container only gets its
        own agent token, which the model proxy swaps for the key.
        """
        s = self.settings
        agent_id = agent["id"]
        paths = agent_paths(s, agent_id)
        token = make_agent_token(self.secret, agent_id, int(agent["token_version"]))
        # Run as the backend's own uid:gid so files the agent writes into the
        # bind mounts stay readable and deletable by the backend.
        user = s.container_uid_gid or f"{os.getuid()}:{os.getgid()}"
        args = [
            "run", "-d",
            "--name", self.container_name(agent_id),
            "--label", "den.managed=1",
            "--label", f"den.agent_id={agent_id}",
            "--user", user,
            "--memory", s.container_memory,
            "--memory-swap", s.container_memory,  # equal to --memory: no swap
            "--cpus", s.container_cpus,
            "--pids-limit", str(s.container_pids),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            # `sleep infinity` as PID 1 never reaps orphans, and zombies
            # count against --pids-limit; docker's init reaps them.
            "--init",
            "--network", s.docker_network,
            "--add-host", "host.docker.internal:host-gateway",
            "-e", "CODEX_HOME=/codex",
            "-e", "HOME=/home/agent",
            "-e", f"PLATFORM_TOKEN={token}",
            "--mount", _bind_mount(paths.workspace, "/workspace"),
            "--mount", _bind_mount(paths.codex_home, "/codex"),
        ]
        if s.model_auth == "chatgpt":
            # Operator testing only: the model can read this login.
            args += ["--mount", _bind_mount(s.chatgpt_auth_json, "/codex/auth.json", readonly=True)]
        args += ["-w", "/workspace", s.agent_image, "sleep", "infinity"]
        return args

    async def ensure_running(self, agent: dict) -> None:
        async with self.lock_for(agent["id"]):
            await self._ensure_running_locked(agent)

    async def _ensure_running_locked(self, agent: dict) -> None:
        await self.prepare()
        agent = self._fresh(agent)
        paths = agent_paths(self.settings, agent["id"])
        # Idempotent; a missing bind-mount source would make `docker run` fail.
        init_agent_dirs(paths)
        # Codex reads config.toml when each app-server starts, so rewriting it
        # here applies a model change to the next turn.
        write_codex_config(self.settings, paths, agent["model"])

        name = self.container_name(agent["id"])
        state = await self._state(agent["id"])
        if state == "running":
            return
        if state is None:
            await self._create(agent)
        elif state == "paused":
            await self._docker("unpause", name)
        elif state == "dead":
            # A dead container cannot be started again; recreate it.
            await self._docker("rm", "-f", name)
            await self._create(agent)
        else:  # created, exited (and transient states, which start resolves)
            await self._docker("start", name)

    async def _create(self, agent: dict) -> None:
        if self.settings.model_auth == "chatgpt" and not self.settings.chatgpt_auth_json.is_file():
            # `--mount` would fail anyway; say what to do instead of docker's text.
            raise AgentRuntimeError(
                f"ChatGPT login mode is on, but {self.settings.chatgpt_auth_json} does not exist. "
                "Run `codex login` on the server."
            )
        await self._docker(*self.run_args(agent))

    async def stop(self, agent_id: str) -> None:
        async with self.lock_for(agent_id):
            rc, _, err = await self._docker(
                "stop", "-t", "5", self.container_name(agent_id), check=False, timeout=60
            )
            if rc != 0 and not _is_missing(err):
                raise AgentRuntimeError(_first_line(err) or "docker stop failed", err[-2000:])

    async def remove(self, agent_id: str) -> None:
        async with self.lock_for(agent_id):
            await self._remove_locked(agent_id)

    async def _remove_locked(self, agent_id: str) -> None:
        rc, _, err = await self._docker(
            "rm", "-f", self.container_name(agent_id), check=False, timeout=60
        )
        if rc != 0 and not _is_missing(err):
            raise AgentRuntimeError(_first_line(err) or "docker rm failed", err[-2000:])

    async def restart(self, agent: dict) -> None:
        """Recreate the container with a new agent token.

        The old container goes first and the version is bumped second, so a
        failed `docker rm` leaves the old container and its still-valid token
        in place instead of a running container holding a revoked token.
        """
        agent_id = agent["id"]
        async with self.lock_for(agent_id):
            await self._remove_locked(agent_id)
            self.db.execute(
                "UPDATE agents SET token_version = token_version + 1 WHERE id = ?", (agent_id,)
            )
            if self.db.one("SELECT id FROM agents WHERE id = ?", (agent_id,)) is None:
                # Deleted while we waited for the lock: do not leave an
                # orphan container behind.
                logger.info("agent %s was deleted during restart; not recreating", agent_id)
                return
            await self._ensure_running_locked(agent)  # re-reads the bumped row

    async def status(self, agent_id: str) -> str:
        try:
            state = await self._state(agent_id)
        except AgentRuntimeError as exc:
            # Status feeds listings; report "unknown" rather than fail them.
            logger.warning("docker status for agent %s failed: %s", agent_id, exc.message)
            return "unknown"
        if state is None:
            return "missing"
        if state == "running":
            return "running"
        if state in _STOPPED_STATES:
            return "stopped"
        return "unknown"  # restarting, removing

    def codex_config(self, agent: dict, codex_home_subdir: str | None = None) -> CodexConfig:
        # The container already has CODEX_HOME, HOME, PLATFORM_TOKEN, the
        # user and /workspace as its working directory, and `docker exec`
        # inherits all of them; only a scratch CODEX_HOME needs overriding.
        env_args: tuple[str, ...] = ()
        if codex_home_subdir:
            env_args = ("-e", f"CODEX_HOME=/codex/{codex_home_subdir}")
        return CodexConfig(
            launch_args_override=(
                "docker", "exec", "-i", *env_args, self.container_name(agent["id"]),
                "codex", "app-server", "--listen", "stdio://",
            )
        )

    def workspace_cwd(self, agent: dict) -> str:
        return "/workspace"
