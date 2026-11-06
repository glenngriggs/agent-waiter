"""Per-agent directories on the host, Codex config, and the platform briefing."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

_AGENT_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class AgentPaths:
    root: Path
    workspace: Path
    codex_home: Path
    home: Path


def agent_paths(settings: Settings, agent_id: str) -> AgentPaths:
    if not _AGENT_ID.match(agent_id):
        raise ValueError(f"invalid agent id {agent_id!r}")
    root = settings.data_dir / "agents" / agent_id
    return AgentPaths(
        root=root,
        workspace=root / "workspace",
        codex_home=root / "codex-home",
        home=root / "home",
    )


def init_agent_dirs(paths: AgentPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.root, 0o700)
    for child in (paths.workspace, paths.codex_home, paths.home):
        child.mkdir(exist_ok=True)
        os.chmod(child, 0o755)


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_codex_config(settings: Settings, model: str) -> str:
    base = settings.agent_backend_url
    lines = [f"model = {_toml_str(model)}"]
    if settings.model_auth == "proxy":
        lines += [
            'model_provider = "den"',
            "",
            "[model_providers.den]",
            'name = "Den model proxy"',
            f"base_url = {_toml_str(base + '/model-proxy/v1')}",
            'env_key = "PLATFORM_TOKEN"',
        ]
    lines += [
        "",
        "[mcp_servers.platform]",
        f"url = {_toml_str(base + '/mcp')}",
        'bearer_token_env_var = "PLATFORM_TOKEN"',
        "",
    ]
    return "\n".join(lines)


def write_codex_config(settings: Settings, paths: AgentPaths, model: str) -> None:
    """Atomically (re)write codex-home/config.toml before every start."""
    paths.codex_home.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=paths.codex_home, prefix=".config.", suffix=".toml")
    with os.fdopen(fd, "w") as fh:
        fh.write(render_codex_config(settings, model))
    os.chmod(tmp, 0o644)
    os.replace(tmp, paths.codex_home / "config.toml")


def remove_agent_dirs(paths: AgentPaths) -> None:
    # shutil.rmtree does not follow symlinks on Linux, so a link the agent
    # planted cannot make this delete files outside its directory.
    shutil.rmtree(paths.root, ignore_errors=True)


_TOOLS_TEXT = """\
Platform tools (an MCP server named `platform`). Depending on your tool setup they
appear either as direct tools in the `mcp__platform` namespace, or as functions you call
from the `exec` tool, for example
`const r = await tools.mcp__platform__schedule_job({...}); text(JSON.stringify(r));`:
- `schedule_job({name, prompt, in_minutes | at | every_minutes | cron, timezone?})`
  creates a scheduled job for you. Give exactly one of: `in_minutes` (run once that many
  minutes from now), `at` (ISO 8601 time), `every_minutes` (repeat), or `cron` (5-field
  cron expression in the user's timezone). Use it for follow-ups you decide you need and
  for recurring tasks the user asks for.
- `list_jobs({})` lists your active jobs.
- `cancel_job({job_id})` cancels a job you created.
Each job run starts a fresh conversation that sees only the job's prompt, so write job
prompts that are self-contained (include the context and file paths the run will need).
The user can see every job and cancel any of them."""


def developer_instructions(
    agent_name: str, *, purpose: str, job_name: str | None = None
) -> str:
    if purpose == "probe":
        return "This is a connectivity check. Reply with the single word: ready."
    parts = [
        f"You are {agent_name}, a personal AI assistant on the AgentWaiter platform, working for one user.",
        "You run in your own isolated container. Your current working directory is the user's "
        "workspace: the user can browse, open, upload and download the files there, so save "
        "anything the user should see in it.",
        _TOOLS_TEXT,
    ]
    if purpose == "job":
        parts.append(
            f"This is an unattended scheduled run of the job '{job_name}'. Nobody is watching. "
            "Do the task, then end with a short summary of what you did; that summary is shown "
            "to the user as the job's result."
        )
    return "\n\n".join(parts)


SCRATCH_DIR = "runs"


def prepare_scratch_home(settings: Settings, paths: AgentPaths, model: str, key: str) -> str:
    """Create a private CODEX_HOME for one job run or probe; return its subdir.

    It lives inside codex-home (so it is inside the container's /codex mount)
    and gets the same config.toml. In chatgpt mode it links to the parent's
    auth.json with a relative link that resolves both on the host and in the
    container.
    """
    if not _AGENT_ID.match(key):
        raise ValueError(f"invalid scratch key {key!r}")
    subdir = f"{SCRATCH_DIR}/{key}"
    home = paths.codex_home / subdir
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(render_codex_config(settings, model))
    if settings.model_auth == "chatgpt" and not (home / "auth.json").is_symlink():
        (home / "auth.json").symlink_to("../../auth.json")
    return subdir


def remove_scratch_home(paths: AgentPaths, key: str) -> None:
    if _AGENT_ID.match(key):
        shutil.rmtree(paths.codex_home / SCRATCH_DIR / key, ignore_errors=True)
