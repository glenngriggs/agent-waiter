# AgentWaiter

AgentWaiter is a cloud platform for personal AI agents. Anyone can sign up in the browser, create one or more agents (pick a name and a model), chat with them, watch what they are doing, stop them, browse their files, and give them scheduled jobs. Agents can schedule their own follow-up jobs too.

Each agent runs in its own Docker container, and **Codex app server** is the harness inside it. The backend drives it through the official Codex Python SDK.

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
- **Credentials:** the real model key never enters a container. Agents call the backend's model proxy with a per-agent token.
- **Jobs:** stored in SQLite, fired by the backend's scheduler. Each run gets a fresh Codex thread, and its result shows in the Jobs tab and as a card in chat.

## Requirements

- Linux or WSL2 with Python 3.12
- Docker. On Windows that means Docker Desktop with **WSL integration** turned on for your distro. If `docker ps` says "permission denied", you're in the `docker` group but this shell isn't yet: open a new terminal or run `newgrp docker`.
- A model credential: a U-M GPT Toolkit key or an OpenAI API key.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then put your key in .env (it is git-ignored)
```

In `.env`, set one of:

```sh
UMGPT_TOOLKIT_API_KEY=...   # U-M GPT Toolkit (https://api.toolkit.umgpt.umich.edu/v1)
OPENAI_API_KEY=...          # or OpenAI
```

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
| `DEN_MODELS` | `gpt-6.1-sol,gpt-6-luna` | Models users can pick |
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
DEN_UPSTREAM_BASE_URL=http://127.0.0.1:9100/v1 DEN_UPSTREAM_API_KEY=fake .venv/bin/python -m den
```

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
| `den/model_proxy.py` | Forwards model calls to the upstream with the real key |
| `den/runtime/` | Docker runtime (product) and local runtime (development) |
| `den/lifecycle.py` | Onboarding, idle reaper, agent deletion |
| `den/static/` | The web client (plain HTML/JS/CSS, no build step) |
| `container/Dockerfile` | The agent image |
| `tests/` | Test suite and the fake model |
