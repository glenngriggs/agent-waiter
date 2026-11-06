"""Platform settings, read once from the environment.

Every tunable named in the design doc lives here so tests can build a
Settings object with `dataclasses.replace` instead of patching modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent

TOOLKIT_BASE_URL = "https://api.toolkit.umgpt.umich.edu/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

DEFAULT_MODEL_LABELS = {
    "gpt-6.1-sol": "GPT-6.1 Sol (strongest)",
    "gpt-6-luna": "GPT-6 Luna (fast, low cost)",
}


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    runtime: str
    host: str
    port: int
    agent_backend_url: str
    model_auth: str
    upstream_base_url: str
    upstream_api_key: str | None
    chatgpt_auth_json: Path
    models: tuple[str, ...]
    model_labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODEL_LABELS))
    agent_image: str = "den-agent:0.163.0-2"
    docker_network: str = "den-agents"
    container_memory: str = "2g"
    container_cpus: str = "1.0"
    container_pids: int = 512
    container_uid_gid: str = ""
    codex_bin: str | None = None
    idle_stop_minutes: int = 15
    min_job_interval_minutes: int = 5
    max_jobs_per_agent: int = 20
    max_agents_per_user: int = 5
    max_concurrent_job_runs: int = 4
    job_run_timeout_minutes: int = 30
    missed_run_grace_hours: int = 24
    onboarding_probe: bool = True
    probe_timeout_seconds: int = 90
    max_upload_mb: int = 100
    secure_cookies: bool = False
    session_days: int = 30
    scheduler_max_sleep_seconds: float = 30.0
    # Who may use the operator's own key (the "platform" provider): emails, or
    # "*" for everyone. Empty by default, so a public deployment never shares
    # the operator's key unless the operator says so.
    platform_key_users: tuple[str, ...] = ()
    platform_label: str = "Shared key on this server"
    # Base URL overrides for the providers users bring keys for (tests point
    # these at fakes; operators can point them at a gateway).
    provider_base_urls: dict[str, str] = field(default_factory=dict)
    # Extra model ids offered per provider, from DEN_<PROVIDER>_MODELS.
    provider_extra_models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # User-supplied base URLs (the OpenAI-compatible provider) may not point at
    # loopback, private or link-local addresses unless this is on, so a public
    # server cannot be used to reach its own internal network.
    allow_private_provider_urls: bool = False
    allow_custom_provider: bool = True

    def label_for(self, model_id: str) -> str:
        return self.model_labels.get(model_id, model_id)

    def platform_allowed(self, email: str) -> bool:
        """True when this account may use the operator's own key."""
        if self.model_auth != "chatgpt" and not self.upstream_api_key:
            return False
        allowed = {e.strip().lower() for e in self.platform_key_users}
        return "*" in allowed or email.strip().lower() in allowed

    @property
    def container_dir(self) -> Path:
        """Directory holding the agent image's Dockerfile."""
        return REPO_ROOT / "container"


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    runtime = env.get("DEN_RUNTIME", "docker").strip().lower()
    if runtime not in {"docker", "local"}:
        raise ValueError(f"DEN_RUNTIME must be 'docker' or 'local', not {runtime!r}")

    port = _int(env.get("DEN_PORT"), 8000)
    # 127.0.0.1 keeps /mcp and /model-proxy off the network. Docker Desktop
    # containers still reach it through host.docker.internal (verified on
    # WSL2); native Linux Docker needs DEN_HOST=0.0.0.0 or the bridge IP.
    default_host = "127.0.0.1"
    default_agent_url = (
        f"http://host.docker.internal:{port}" if runtime == "docker" else f"http://127.0.0.1:{port}"
    )

    model_auth = env.get("DEN_MODEL_AUTH", "proxy").strip().lower()
    if model_auth not in {"proxy", "chatgpt"}:
        raise ValueError(f"DEN_MODEL_AUTH must be 'proxy' or 'chatgpt', not {model_auth!r}")

    # Upstream presets: explicit DEN_* wins, then the Toolkit key, then OpenAI.
    base_url = env.get("DEN_UPSTREAM_BASE_URL", "").strip()
    api_key = env.get("DEN_UPSTREAM_API_KEY", "").strip() or None
    if not base_url and not api_key:
        if env.get("UMGPT_TOOLKIT_API_KEY", "").strip():
            base_url, api_key = TOOLKIT_BASE_URL, env["UMGPT_TOOLKIT_API_KEY"].strip()
        elif env.get("OPENAI_API_KEY", "").strip():
            base_url, api_key = OPENAI_BASE_URL, env["OPENAI_API_KEY"].strip()
    if not base_url:
        base_url = OPENAI_BASE_URL

    models = tuple(
        m.strip() for m in env.get("DEN_MODELS", "gpt-6.1-sol,gpt-6-luna").split(",") if m.strip()
    )
    if not models:
        raise ValueError("DEN_MODELS must list at least one model")

    uid_gid = env.get("DEN_CONTAINER_USER", "").strip() or f"{os.getuid()}:{os.getgid()}"

    platform_users = tuple(
        e.strip().lower() for e in env.get("DEN_PLATFORM_KEY_USERS", "").split(",") if e.strip()
    )
    provider_urls: dict[str, str] = {}
    extra_models: dict[str, tuple[str, ...]] = {}
    for provider in ("openai", "anthropic", "umgpt"):
        url = env.get(f"DEN_{provider.upper()}_BASE_URL", "").strip()
        if url:
            provider_urls[provider] = url.rstrip("/")
        extra = tuple(
            m.strip() for m in env.get(f"DEN_{provider.upper()}_MODELS", "").split(",") if m.strip()
        )
        if extra:
            extra_models[provider] = extra

    return Settings(
        # Outside the repo by default: Codex walks up from a workspace looking
        # for AGENTS.md, and must not find this project's course instructions.
        data_dir=Path(env.get("DEN_DATA_DIR", "~/.local/share/den")).expanduser().resolve(),
        runtime=runtime,
        host=env.get("DEN_HOST", "").strip() or default_host,
        port=port,
        agent_backend_url=(env.get("DEN_AGENT_BACKEND_URL", "").strip() or default_agent_url).rstrip("/"),
        model_auth=model_auth,
        upstream_base_url=base_url.rstrip("/"),
        upstream_api_key=api_key,
        chatgpt_auth_json=Path(
            env.get("DEN_CHATGPT_AUTH_JSON", "~/.codex/auth.json")
        ).expanduser(),
        models=models,
        agent_image=env.get("DEN_AGENT_IMAGE", "den-agent:0.163.0-2"),
        container_uid_gid=uid_gid,
        codex_bin=env.get("DEN_CODEX_BIN") or None,
        idle_stop_minutes=_int(env.get("DEN_IDLE_STOP_MINUTES"), 15),
        min_job_interval_minutes=_int(env.get("DEN_MIN_JOB_INTERVAL_MINUTES"), 5),
        max_agents_per_user=_int(env.get("DEN_MAX_AGENTS_PER_USER"), 5),
        max_concurrent_job_runs=_int(env.get("DEN_MAX_CONCURRENT_JOB_RUNS"), 4),
        job_run_timeout_minutes=_int(env.get("DEN_JOB_RUN_TIMEOUT_MINUTES"), 30),
        onboarding_probe=_bool(env.get("DEN_ONBOARDING_PROBE"), True),
        max_upload_mb=_int(env.get("DEN_MAX_UPLOAD_MB"), 100),
        secure_cookies=_bool(env.get("DEN_SECURE_COOKIES"), False),
        platform_key_users=platform_users,
        platform_label=env.get("DEN_PLATFORM_LABEL", "").strip() or "Shared key on this server",
        provider_base_urls=provider_urls,
        provider_extra_models=extra_models,
        allow_private_provider_urls=_bool(env.get("DEN_ALLOW_PRIVATE_PROVIDER_URLS"), False),
        allow_custom_provider=_bool(env.get("DEN_ALLOW_CUSTOM_PROVIDER"), True),
    )


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines into os.environ without overriding set vars.

    Kept dependency-free on purpose; quotes around values are stripped.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
