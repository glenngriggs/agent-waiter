# AgentWaiter

AgentWaiter is a cloud platform for personal AI agents. Anyone can sign up in the browser, create one or more agents (pick a name and a model), chat with them, watch what they are doing, stop them, browse their files, and give them scheduled jobs. Agents can schedule their own follow-up jobs too.

Each agent runs in its own Docker container, and **Codex app server** is the harness inside it. The backend drives it through the official Codex Python SDK.

**Features**

- **Bring your own key.** After signing in, open **Settings → API keys** and add a key for OpenAI, Anthropic, the U-M GPT Toolkit, or any OpenAI-compatible server. Keys are encrypted on the server, shown back only as their last four characters, and never enter an agent's container.
- **GPT and Claude models.** Claude (Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1) runs under the same Codex harness, through a translation layer from the Responses API to the Anthropic Messages API. Any agent can switch models later.
- Chat with live streaming. You can queue messages or interrupt a running task.
- A file browser for each agent's workspace.
- Scheduled jobs that users and agents can both create.
- **Usage page** with token counts per model, and cost estimates for Claude.
- **Account settings:** change password, or delete the account with everything in it.

- Design doc: [docs/designs/agent-platform.md](docs/designs/agent-platform.md)
- Spec: `slides.pdf`

## How it fits together

```
browser ──▶ AgentWaiter backend (FastAPI, one process) ──docker exec──▶ codex app-server (one container per agent)
             │  SQLite, SSE, scheduler                               │
             │◀──── /model-proxy (holds the real key) ◀──────────────┤
             │◀──── /mcp (schedule_job, list_jobs, cancel_job) ◀─────┘
```

- **What an agent is:** a database row plus `data/agents/<id>/{workspace,codex-home}` on the host. The container is disposable; Restart recreates it and nothing the user cares about is lost.
- **Conversations:** Codex keeps the thread in the agent's `codex-home`. The backend records every streamed item in SQLite and owns the running turn, so closing the browser changes nothing.
- **Credentials:** no real model key ever enters a container. Agents call the backend's model proxy with a per-agent token, and the proxy uses the agent owner's own key for that provider. The operator's own key is used only for accounts listed in `DEN_PLATFORM_KEY_USERS`.
- **Jobs:** stored in SQLite, fired by the backend's scheduler. Each run gets a fresh Codex thread, and its result shows in the Jobs tab and as a card in chat.

## Requirements

- Linux or WSL2 with Python 3.12
- Docker. On Windows that means Docker Desktop with **WSL integration** turned on for your distro. If `docker ps` says "permission denied", you're in the `docker` group but this shell isn't yet: open a new terminal or run `newgrp docker`.
- A model key. Each user can add their own on the Settings page. Optionally, the operator can share one key from `.env` (below).

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then put your key in .env (it is git-ignored)
```

Nothing in `.env` is required: users add their own keys in **Settings → API keys**.

To also share one key of your own with chosen accounts (for example, just yourself), set it in `.env` together with who may use it:

```sh
UMGPT_TOOLKIT_API_KEY=...                 # or OPENAI_API_KEY=...
DEN_PLATFORM_KEY_USERS=you@example.com    # comma separated; * = everyone (private servers only)
```

Accounts not on that list never touch your key: they see only the providers they added keys for.

## Run

```sh
.venv/bin/python -m den
```

Open http://localhost:8000. The first agent you create builds the agent image (`container/Dockerfile`), which takes a few minutes once. You can also build it ahead of time:

```sh
docker build -t den-agent:0.163.0-2 container/
```

Everything the platform stores lives under `DEN_DATA_DIR` (default `~/.local/share/den`). Delete that directory, and any `den-agent-*` containers, to start over.

### Configuration

All settings are environment variables. They can go in `.env`; see `.env.example` for the full list. The ones you are most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `DEN_PORT` | `8000` | Port for the web app |
| `DEN_HOST` | `127.0.0.1` | Interface to listen on. Docker Desktop containers reach `127.0.0.1` through `host.docker.internal`. On native Linux Docker, set this to `0.0.0.0` or the bridge IP. |
| `DEN_MODELS` | `gpt-6.1-sol,gpt-6-luna` | Models offered on the shared key |
| `DEN_PLATFORM_KEY_USERS` | (empty) | Accounts allowed to use the shared key from `.env` |
| `DEN_ALLOW_PRIVATE_PROVIDER_URLS` | `false` | Let "OpenAI-compatible" keys point at private/loopback URLs (self-hosted models) |
| `DEN_DATA_DIR` | `~/.local/share/den` | Database, secrets and agent files |
| `DEN_RUNTIME` | `docker` | `local` runs agents on the host with **no isolation** (development only) |

### Development mode (no Docker)

```sh
DEN_RUNTIME=local .venv/bin/python -m den
```

Agents then run the Codex binary bundled with the SDK directly on your machine. Each agent still gets its own `CODEX_HOME` and workspace, but there is no isolation. The web page shows a banner while this mode is on.

### Try it without spending tokens

`tests/fake_model.py` is a scripted stand-in for the model API:

```sh
.venv/bin/python -m tests.fake_model --port 9100 &
.venv/bin/python -m tests.fake_anthropic --port 9200 &      # fake Claude; key: sk-ant-test-0123456789
DEN_UPSTREAM_BASE_URL=http://127.0.0.1:9100/v1 DEN_UPSTREAM_API_KEY=fake DEN_PLATFORM_KEY_USERS='*' \
  DEN_ANTHROPIC_BASE_URL=http://127.0.0.1:9200 .venv/bin/python -m den
```

## Keeping keys out of git (and publishing)

- `.env`, `secret.key`, databases and agent data are git-ignored. Keys live only in `.env` and in the encrypted database.
- `python scripts/check_secrets.py --history` checks every tracked file and every commit on every branch. It looks for the exact values in your local `.env` and for anything shaped like an OpenAI or Anthropic key, and it never prints a secret. Run it before pushing to a new or public repository. CI runs it on every push.
- `sh scripts/install-git-hooks.sh` installs a pre-commit hook that runs the quick check before each commit.
- To publish, push the code without `.env`, then add your keys in Settings or a fresh `.env` on the server. Behind HTTPS, set `DEN_SECURE_COOKIES=true`, which also turns on HSTS.

## Tests

```sh
.venv/bin/python -m pytest            # everything except the real-Docker test
.venv/bin/python -m pytest -m docker  # end-to-end through real containers
```

Most tests run the real Codex app server (the binary bundled with the SDK) against the fake model, so they need neither credentials nor Docker.

## Code map

| Path | What it is |
|---|---|
| `den/app.py` | FastAPI app, startup/shutdown, CSRF check |
| `den/auth.py` | Register, login, sessions, the `owned_agent` check every agent route uses |
| `den/agents.py` | Create/rename/delete agents, start/stop/restart containers |
| `den/chat.py` | Send, stop, history, and the live event stream (SSE) |
| `den/files.py` | Workspace browse/preview/upload/download, symlink-safe |
| `den/jobs.py` | Job and run routes |
| `den/scheduler.py` | Schedule math and the scheduler loop |
| `den/codex_bridge.py` | Runs one Codex turn through the SDK and turns its events into platform events |
| `den/mcp_server.py` | The MCP endpoint agents use to schedule jobs |
| `den/model_proxy.py` | Routes each agent's model calls to its provider with the right key; records usage |
| `den/providers.py` | Provider catalog, encrypted user keys, key checks, whose key a call uses |
| `den/anthropic_bridge.py` | Runs Claude under Codex: Responses API ⇄ Messages API translation |
| `den/account.py` | Settings routes: API keys, usage |
| `den/usage.py` | Token usage counting and the cost estimate |
| `den/security_headers.py` | CSP and other browser security headers |
| `den/runtime/` | Docker runtime (product) and local runtime (development) |
| `den/lifecycle.py` | Onboarding, idle reaper, agent deletion |
| `den/static/` | The web client (plain HTML/JS/CSS, no build step) |
| `container/Dockerfile` | The agent image |
| `scripts/check_secrets.py` | Secret scan for files and history (also run by CI) |
| `tests/` | Test suite, the fake model, and a strict fake of the Anthropic API |
