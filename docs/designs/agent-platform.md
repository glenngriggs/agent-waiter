# Den: a cloud platform for personal AI agents

Design doc for the whole platform. The one fixed rule: the product's harness is Codex app server.

## 1. Problem

People want an AI assistant that does real work for them, such as reading and writing files, running commands and following up later, without installing anything or running it on their own computer. Den is a website where anyone can sign up, create one or more personal agents, chat with them in the browser, see their files and give them scheduled jobs. Because each agent runs commands the model chooses on our servers for people we do not know, one agent must never be able to break the server, see another user's files, or reach another user's agent. Agents have to keep working when the browser closes and come back exactly as they were after a restart, so users can trust them with tasks that outlive a browser tab.

## 2. Technical Plan

### The shape of the system

Den is one Python process (FastAPI) plus one Docker container per agent. The browser talks only to the backend. The backend starts and stops containers, drives Codex app server inside them through the Codex Python SDK, stores everything durable in SQLite and in one directory per agent, and runs the job scheduler. Agents talk back to the backend for two things only: the model (through a proxy that holds the real key) and the platform tools (scheduling jobs, through an MCP endpoint).

```
                         the cloud (one host)
 browser ──HTTPS──▶ ┌──────────────────────────────────────────────┐
  (HTML+JS,         │ Den backend (FastAPI, one process)           │
   cookies, SSE)    │                                              │
                    │  /api/*  auth, agents, chat, files, jobs     │
                    │  SSE     live events per agent               │
                    │  scheduler loop, idle reaper                 │
                    │  SQLite: users, sessions, agents, turns,     │
                    │          events, jobs, job_runs              │
                    │                                              │
                    │  /model-proxy/v1/*  ◀── agent token ──┐      │──▶ model provider
                    │  /mcp (schedule_job, ...) ◀── token ──┤      │    (real key lives
                    └───────┬───────────────────────────────┼──────┘     only here)
                            │ docker exec -i … codex app-server (stdio, one per turn)
             ┌──────────────▼─────────────┐   ┌─────────────┴──────────────┐
             │ agent A's container        │   │ agent B's container        │
             │  codex app-server          │   │  codex app-server          │
             │  /workspace  ◀ bind mount  │   │  /workspace  ◀ bind mount  │
             │  /codex (CODEX_HOME) ◀ bind│   │  /codex      ◀ bind        │
             └────────────────────────────┘   └────────────────────────────┘
                 data/agents/A/{workspace,codex-home}   data/agents/B/{...}
```

### What an agent is

An agent is a row in the `agents` table (owner, name, model, the id of its chat thread) plus a directory on the host, `data/agents/<agent_id>/`, with three children: `workspace/` (the user's files, mounted at `/workspace`), `codex-home/` (Codex's `CODEX_HOME`, mounted at `/codex`, holding `config.toml` and every saved thread), and `home/` (used only by the local development runtime). The container holds nothing that matters: it can be deleted at any time and recreated from the image, and the agent comes back with the same name, model, conversation, files and jobs. The only thing lost is software the agent installed outside `/workspace`.

### Chat: how a message becomes a streamed reply

1. The browser posts the message. The backend checks the user owns the agent, refuses if a chat turn is already running for it (the user can press Stop instead), records a `turns` row, and returns at once.
2. A background task owned by the backend (not by the browser's connection) makes sure the container is running, starting it if the idle reaper stopped it.
3. It launches `codex app-server` inside the container with `docker exec -i`, using the SDK's `CodexConfig(launch_args_override=...)`. It resumes the agent's chat thread (or starts one the first time, saving its id), passing the agent's model, `Sandbox.full_access`, `ApprovalMode.deny_all`, `/workspace` as the working directory and a short platform briefing as developer instructions.
4. It runs the turn and reads the SDK's event stream. Each event is translated into a small platform event (`item_started`, `item_completed`, `message_delta`, `notice`, `turn_completed`, ...). Events worth keeping are written to the `events` table with an increasing sequence number. Every event is also published to an in-memory bus for that agent.
5. The browser holds one Server-Sent Events connection per open agent. On connect it sends the last sequence number it has seen; the backend replays anything newer from the table, then streams live events. The text of a reply that is still streaming is kept in memory by the running turn and sent as a snapshot to a browser that connects mid-turn, so a reopened tab picks up exactly where the agent is.
6. When the turn ends, the client is closed (which ends the app-server process in the container) and the turn row gets its final status, final reply and any error.

Stop calls `interrupt()` on the turn and then closes the client (any reply text that streamed before the stop is saved as a `partial` agent message, so a reloaded page still shows it), because the docs show an interrupted turn's running command keeps going until the client is closed. Because each turn has its own client, stopping the chat never stops a scheduled job running in the same container. This was verified through real containers: closing the client ends the `docker exec` session, app-server sees end-of-input and exits, and the command it was running dies with it (`tests/test_docker_e2e.py`, Stop test).

If the backend dies mid-turn, the in-memory client dies with it. On the next start, any turn or job run still marked `running` is marked `interrupted` with the message "The platform restarted while this was running." The Codex thread itself is safe on disk, so the user's next message continues the same conversation.

**Queueing and interrupting.** A message sent while a chat turn is running is not refused: it waits in the agent's queue (a `turns` row with status `queued`) and runs when the current turn ends, in order. "Interrupt & send" puts the message at the front (`queue_priority` 1) and stops the running turn, which ends `interrupted` with "Interrupted by a newer message."; the interrupting message then starts. A dispatcher that runs whenever a chat turn ends (and at startup, so a queue survives a restart) starts the next waiting message; it has no `await` between choosing and starting, so two can never start at once. Stop ends only the running turn and the queue continues; stopping or restarting the container, or deleting the agent, clears the queue first. At most 10 messages wait per agent. Queued messages appear in a panel above the composer, not in the timeline, until they start.

**Quiet activity.** A turn's tool activity (commands, file edits, tool calls) is shown as one collapsed "Worked · N steps" row that expands on click. A command that fails is not highlighted: models routinely try something, fail and recover. Errors are shown prominently only when the user must act: a failed turn, a failed job run, a setup or container failure.

### Jobs and the scheduler

Jobs live in SQLite. A job has a name, a prompt, a schedule (once at a time, every N minutes, or a cron expression evaluated in the creator's timezone), who created it (the user or the agent), a status (`active`, `cancelled`, `finished`) and its next run time. One asyncio loop in the backend sleeps until the earliest due job (or at most 30 seconds, or until woken by a change), then fires due jobs.

Firing a job first advances its `next_run_at` (so a crash cannot fire it twice), records a `job_runs` row, and starts a run task. Each run gets a **fresh Codex thread** in the agent's container with the job prompt and a briefing that says this is an unattended scheduled run. Each run also gets its own SDK client, so cancelling one run never touches another. The run's events go to the same `events` table, tied to the run's turn, so the user can open any run and read exactly what the agent did. When a run ends, the job's last result is updated and a `job_run_finished` card is posted to the agent's chat timeline. This is how a user "comes back to what the agent did while they were gone."

Two app-server processes cannot share one `CODEX_HOME`: when a job run started while a chat turn was running, the second process failed with "failed to initialize sqlite state runtime". So every job run, and the onboarding probe, runs with its **own scratch `CODEX_HOME`** at `codex-home/runs/<turn id>/` (same `config.toml`; passed to `docker exec` as `-e CODEX_HOME=/codex/runs/<id>`). Chat keeps the agent's main `codex-home`. A scratch home is deleted when its run ends, since the run's transcript is already in the `events` table.

Rules that keep this safe and cheap: at most 4 job runs execute at once across the platform (others wait in `queued`); a run is stopped after 30 minutes; if a job's previous run is still going when it comes due again, that occurrence is recorded as `skipped`. Missed runs (the backend was down or the laptop slept): if the run is late by less than 24 hours it runs once now, marked late; if it is later than that, a `missed` run is recorded and the job moves on to its next time. Only one catch-up ever runs, however many occurrences were missed.

### Agents scheduling their own jobs (MCP)

Neither SDK can register tools, so the backend serves a minimal MCP endpoint over streamable HTTP at `/mcp` (JSON-RPC: `initialize`, `tools/list`, `tools/call`, notifications answered with 202, `GET` answered with 405, matching what the docs verified). Each agent's `config.toml` points Codex at it with `bearer_token_env_var = "PLATFORM_TOKEN"`. The token identifies the agent, so an agent can only see and create its own jobs. Tools: `schedule_job` (exactly one of `in_minutes`, `at`, `every_minutes`, `cron`), `list_jobs`, and `cancel_job`, which may cancel only jobs the agent itself created. User-created jobs can be cancelled only by the user. Testing against Codex 0.163 showed it exposes MCP tools to the model as nested tools reachable from its `exec` tool (`tools.mcp__platform__schedule_job(...)`), so the platform briefing names them explicitly.

### Model credentials: the proxy

The docs proved a model with a shell can read any key or login placed in its container, and anyone can sign up, so the real key stays in the backend. Each container's `config.toml` defines a provider whose `base_url` is the backend's `/model-proxy/v1` and whose `env_key` is `PLATFORM_TOKEN`. The proxy checks the token, checks the requested model is one the operator offers, swaps in the real key and streams the upstream response back unchanged. The upstream is the U-M GPT Toolkit or OpenAI, chosen by environment variables. For the operator's own testing only, a `chatgpt` mode mounts `~/.codex/auth.json` read-only instead; the README says plainly that the model can read that login.

### Agent tokens

An agent token is `den_<agent_id>_<version>_<hmac>`, where the HMAC is computed with a server secret kept in `data/secret.key`. Nothing secret is stored per agent; the backend verifies a token by recomputing it. Restarting an agent's container bumps the version, which revokes the old token. The model can read its own token. That is acceptable, because the token grants only what the agent can already do: call the model and manage its own jobs.

### Containers

The image is built from `container/Dockerfile` (`node:22-slim`, CA certificates, git, python3, Codex CLI pinned at **0.163.0**, the exact version bundled with the Python SDK the backend and tests use, so the protocol and the nested-MCP-tool behaviour are the ones the tests verify). The backend builds it on first start if it is missing. `docker run` options for every agent: name `den-agent-<id>`, the two bind mounts, `CODEX_HOME=/codex`, `PLATFORM_TOKEN`, `--user <host uid>:<host gid>` (so files the agent writes are owned by the backend's user and the backend can serve and delete them), `--memory 2g --cpus 1 --pids-limit 512`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--init` (so orphaned processes are reaped instead of filling the pids limit), `--add-host host.docker.internal:host-gateway`, and the `den-agents` network, created with inter-container traffic disabled. The main process is `sleep infinity`; Codex runs only through `docker exec`.

Containers are created when the agent is created, started on demand, stopped by an idle reaper after 15 minutes with no running turn or job run, and removed and recreated by the user's Restart button. A stopped container costs nothing but disk.

### Files

Because the workspace is a bind mount, the backend reads and writes it directly on the host, even while the container is stopped. Every path from the browser is split into components and opened one component at a time with `O_NOFOLLOW` under the workspace's directory handle. This means `..`, absolute paths and symlinks the agent created can never reach outside the workspace, even under a race. Downloads are always attachments. The in-browser "open" view returns text as JSON, and serves images inline only for PNG/JPEG/GIF/WebP with `Content-Security-Policy: sandbox` and `nosniff`, so a file the agent wrote can never run script on Den's origin.

### Accounts and isolation

Passwords are hashed with `scrypt`. A login creates a random session token in an `HttpOnly`, `SameSite=Lax` cookie; only its SHA-256 is stored. Every state-changing `/api` request must carry the header `X-Den-CSRF: 1`, which a cross-site form cannot send. Every agent route loads the agent with `WHERE id = ? AND user_id = ?` and answers **404** (not 403), so editing a URL to another user's agent reveals nothing. Jobs, runs and turns are always looked up together with their agent id, so a job id from another agent also returns 404. The test suite checks every route with two accounts.

### Onboarding

The user names the agent and picks a model from the operator's list (default `gpt-6.1-sol` and `gpt-6-luna`). The backend then streams its progress as `agent_status` events: creating files, preparing the image (first time only), starting the container, and checking the model. The check is one tiny real turn ("Reply with the single word: ready") on a throwaway thread. It proves the container, Codex, the credentials and the model all work before the user reaches chat, because the docs warn that a listed model does not always work. If any step fails, the screen shows which step and why, with Retry and a way to pick the other model.

### What the user sees when something fails

Nothing fails silently. Codex error events that will be retried show as a yellow "Reconnecting to the model…" notice. A failed turn shows a red card whose text depends on the cause (`TurnError.codex_error_info`):

| Cause | Message |
|---|---|
| unauthorized | "The model provider rejected the platform's credentials. The operator needs to check the API key." |
| usage or rate limit | "The model is rate-limited right now. Wait a minute or switch this agent to another model." |
| server overloaded or unknown model | "The model `<id>` is not answering right now. Try again, or switch this agent to another model." |
| context window exceeded | "This conversation is too long for the model. Start a new conversation." (button) |
| app-server process died | "The agent's runtime stopped unexpectedly. Your files and conversation are safe; send the message again." |
| container could not start | "The agent's container could not start: `<docker's message>`." |

Commands that exit non-zero are shown in red with their exit code, because the docs warn a turn can "complete" with every command failed. A failed job run shows its error as the job's last result.

### Development runtime and testing

The container runtime sits behind one small interface (ensure running, stop, restart, remove, status, and "how to launch app-server for this agent"). The product uses the Docker implementation. A second, **local** implementation exists only for development and tests: it runs the app-server binary bundled with the Python SDK directly on the host, with a per-agent `CODEX_HOME`, a clean environment (`env -i`), and the agent's host workspace as its working directory. It provides no isolation, so it must be switched on explicitly (`DEN_RUNTIME=local`) and the web page shows a "development mode" banner while it is on.

The tests drive the real app-server, the real SDK, the real MCP endpoint and the real model proxy against a **scripted fake model** that speaks the Responses API (the same approach used before design to confirm that a custom `base_url` provider, streaming and MCP calls under `deny_all` work with Codex 0.163). That way the whole chain is tested without credentials or Docker. The Docker code is tested two ways: its exact `docker` command lines against a fake `docker` executable, and an opt-in end-to-end test that runs only where a Docker daemon is available.

### Key decisions (the ones that are hardest to change later)

| Decision | Choice | What it costs |
|---|---|---|
| What an agent is | DB row + `data/agents/<id>/` on the host; container is disposable | Software installed outside `/workspace` disappears on Restart |
| Where a conversation lives | Codex thread in `codex-home` + backend event log in SQLite; backend owns the turn | A backend crash interrupts running turns (they are marked so and can be re-sent) |
| How users stay apart | Owner-scoped queries with 404, per-agent container and mounts, HMAC agent tokens, `O_NOFOLLOW` path walking, model proxy | Every new route must use the owner dependency; tests enforce it |
| Containers | Start on demand, stop after 15 idle minutes, hard limits, no capabilities | First message after idle waits ~1–2 s for `docker start`; agents cannot `apt-get install` |
| Schedule | SQLite + in-process scheduler; one late catch-up within 24 h, otherwise `missed`; overlaps `skipped` | Single backend process only (no horizontal scaling without a lock) |
| Failures | Cause-specific messages in chat and on job runs; onboarding test turn | One tiny model call per new agent |

### Known risks and limits

These were found in review and deliberately left for later; each is a real gap for a public deployment:

- **Disk:** a workspace has no quota, so one agent can fill the host disk (uploads are capped per file, not per user).
- **Spend and abuse:** there is no sign-up rate limit, no global cap on agents or running containers, and no per-user model budget; anyone who signs up costs the operator tokens.
- **Login lockout:** the login limiter is per email, so anyone can lock a user out for 10 minutes with bad passwords.
- **Egress:** containers have unrestricted outbound network access.
- **One process:** running turns, the event bus and the scheduler live in one backend process, so it cannot be scaled horizontally without a shared lock and bus.
- **Codex start-up traffic:** each app-server launch in a container runs a short `git` fetch (Codex 0.163 syncing its catalog); it adds a little latency per turn.
- **Workspace races:** a chat turn and job runs on the same agent can edit the same files at the same time.

### What is deliberately out of scope

The reach goals (skill library, custom skills, user-added MCP servers, OAuth service logins, sharing files) come after the core and are not designed here. The design leaves room for them: skills would be files in `codex-home/skills/`, and user MCP servers would be extra entries in the generated `config.toml`. Deployment to a real cloud is also out of scope; the platform runs on one host, the student's computer, and users need only a browser.

## 3. Alternatives

- **TypeScript backend with `@openai/codex-sdk`.** Rejected because that SDK runs `codex exec`, not app server, which breaks the one fixed rule (`docs/codex-sdk.md`, "Which SDK").
- **Writing our own client for the raw `codex app-server` JSON-RPC protocol.** Rejected because the docs say the protocol is experimental and changes between versions; the Python SDK speaks it for us.
- **Running the SDK inside each container behind a small service.** Rejected because it puts our code in every container, the docs did not test it, and `docker exec` was verified. Its one advantage, a turn surviving a backend restart, is covered instead by marking such turns interrupted and resuming the thread.
- **One long-lived app-server process per agent shared by chat and jobs.** Rejected because Stop has to close the client to kill running commands, which would also kill a job run in progress, and because idle agents would keep processes alive. One client per turn costs about a second of startup per turn.
- **Letting job runs share the agent's main `CODEX_HOME` with chat.** Rejected because it does not work: a second app-server process on the same `CODEX_HOME` failed with "failed to initialize sqlite state runtime" while a chat turn was running. Each job run and the probe get a scratch `CODEX_HOME` instead.
- **Running scheduled jobs inside the agent's chat thread.** Rejected because only one client may write to a thread while a turn runs (a job would block on, or collide with, the user's chat), and recurring jobs would fill the chat's context window. Instead each run gets a fresh thread and the chat gets a result card.
- **Putting the real API key (or `auth.json`) in every container.** Rejected because the docs showed the model can read it from the environment, `/proc` or `CODEX_HOME`, and anyone can sign up; one user could take the operator's key. Kept only as an explicit `chatgpt` testing mode for a single operator.
- **Codex's `workspace-write` sandbox inside the container.** Rejected because `bwrap` cannot create namespaces in a default container and every command fails (`docs/codex-sdk.md`). The container is the boundary, so Codex runs with full access inside it.
- **`ApprovalMode.auto_review` approvals.** Rejected because nobody is at the browser during scheduled runs, and the container already confines what commands can do; `deny_all` never stops to ask.
- **WebSockets for live updates.** Rejected in favour of Server-Sent Events: traffic is one-way (commands go over normal POSTs), and `EventSource` reconnects by itself and sends `Last-Event-ID`, which is exactly the resume-after-close behaviour the spec asks for.
- **React/Vite single-page app.** Rejected because a build step adds tooling for a handful of screens; one static HTML file, one JS file and one CSS file served by FastAPI are enough.
- **PostgreSQL.** Rejected for this scope: one host, one backend process, and SQLite in WAL mode is one file to back up. The schema is plain SQL so it ports.
- **docker-py (the Docker SDK for Python).** Rejected because it is synchronous and an extra dependency, while the Codex launch already shells out to `docker exec`; running the `docker` CLI through `asyncio` subprocesses keeps one mechanism and makes every command visible in logs.
- **Reading and writing workspace files through `docker exec`/`docker cp`.** Rejected because it needs the container running and is slow; a bind mount plus `--user <host uid>` lets the backend serve files directly, even for stopped agents.
- **Named Docker volumes instead of bind mounts.** Rejected for the same reason: the backend could not reach the files without a container.
- **Storing each agent's token in the database.** Rejected in favour of an HMAC-derived token: nothing to leak per agent, verification is a recomputation, and bumping a version number revokes it.
- **APScheduler (or cron in containers).** Rejected because the missed-run, overlap and catch-up rules are the hard part and are simpler to state and test in our own small loop; cron inside containers would not run while the reaper has the container stopped, and users could not see it.
- **Letting agents cancel any of their agent's jobs.** Rejected for least privilege: a prompt-injected agent could silently cancel jobs the user set up. Agents can cancel only jobs they created; the user can cancel everything.
- **Answering 403 for another user's agent.** Rejected because it confirms the id exists; 404 reveals nothing.

## 4. Detailed Implementation

The Python package is `den/`. Everything runs in one process started with `python -m den`. IDs are `uuid4().hex` (32 lowercase hex characters, no dashes). Timestamps are stored as ISO 8601 UTC strings produced by one helper (`den.db.iso`), always in the form `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, so they sort as text. Build order for `/egm-implement`: config, db, security, events, context, agent_files, runtime, codex_bridge, scheduler, lifecycle, routers, app, static, container, tests.

### 4.1 Shared contracts

These contracts are used by more than one file; every file below follows them exactly.

**Database schema** (created by `den/db.py`, SQLite, `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`):

| table | columns |
|---|---|
| `users` | `id TEXT PK`, `email TEXT UNIQUE NOT NULL COLLATE NOCASE`, `password_hash TEXT NOT NULL`, `timezone TEXT NOT NULL DEFAULT 'UTC'`, `created_at TEXT NOT NULL` |
| `sessions` | `token_hash TEXT PK`, `user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE`, `created_at TEXT NOT NULL`, `expires_at TEXT NOT NULL` |
| `agents` | `id TEXT PK`, `user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE`, `name TEXT NOT NULL`, `model TEXT NOT NULL`, `setup_state TEXT NOT NULL` (`creating`/`ready`/`error`), `setup_detail TEXT`, `token_version INTEGER NOT NULL DEFAULT 1`, `chat_thread_id TEXT`, `conversation_no INTEGER NOT NULL DEFAULT 1`, `created_at TEXT NOT NULL`, `last_active_at TEXT NOT NULL` |
| `turns` | `id TEXT PK`, `agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE`, `source TEXT NOT NULL` (`chat`/`job`/`probe`), `job_run_id TEXT`, `conversation_no INTEGER NOT NULL`, `prompt TEXT NOT NULL`, `codex_thread_id TEXT`, `codex_turn_id TEXT`, `status TEXT NOT NULL` (`queued`/`running`/`completed`/`failed`/`interrupted`/`cancelled`), `queue_priority INTEGER NOT NULL DEFAULT 0` (added by migration to schema version 2), `error_kind TEXT`, `error_message TEXT`, `final_response TEXT`, `started_at TEXT NOT NULL`, `finished_at TEXT` |
| `jobs` | `id TEXT PK`, `agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE`, `name TEXT NOT NULL`, `prompt TEXT NOT NULL`, `schedule_kind TEXT NOT NULL` (`once`/`interval`/`cron`), `schedule_value TEXT NOT NULL` (ISO UTC time / integer minutes as text / cron expression), `timezone TEXT NOT NULL`, `created_by TEXT NOT NULL` (`user`/`agent`), `source_turn_id TEXT`, `status TEXT NOT NULL` (`active`/`cancelled`/`finished`), `next_run_at TEXT` (NULL when nothing is due), `last_run_id TEXT`, `created_at TEXT NOT NULL`, `cancelled_at TEXT` |
| `job_runs` | `id TEXT PK`, `job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE`, `agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE`, `scheduled_for TEXT NOT NULL`, `status TEXT NOT NULL` (`queued`/`running`/`succeeded`/`failed`/`interrupted`/`skipped`/`missed`), `late INTEGER NOT NULL DEFAULT 0`, `trigger TEXT NOT NULL` (`schedule`/`manual`), `turn_id TEXT`, `summary TEXT`, `error_message TEXT`, `created_at TEXT NOT NULL`, `started_at TEXT`, `finished_at TEXT` |
| `events` | `seq INTEGER PRIMARY KEY AUTOINCREMENT`, `agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE`, `turn_id TEXT`, `channel TEXT NOT NULL` (`chat`/`job`/`agent`), `kind TEXT NOT NULL`, `payload TEXT NOT NULL` (JSON), `created_at TEXT NOT NULL` |

Indexes: `agents(user_id)`, `turns(agent_id, status)`, `jobs(agent_id, status)`, `jobs(status, next_run_at)`, `job_runs(job_id, created_at)`, `job_runs(status)`, `events(agent_id, seq)`, `events(turn_id, seq)`, `sessions(user_id)`.

**Event shape.** Every event sent to a browser (over SSE or inside a JSON response) is one JSON object:
`{"seq": int|null, "agent_id": str, "turn_id": str|null, "channel": "chat"|"job"|"agent", "kind": str, "data": {...}, "at": iso}`.
`seq` is set only for persisted events. `channel` is `chat` for events of chat turns, `job` for events of job-run turns and the probe turn, and `agent` for agent-level events.

| kind | persisted | data |
|---|---|---|
| `turn_queued` | yes | `{prompt, priority, mode}`: a chat message is waiting (same `turn_id` it will start with) |
| `turn_cancelled` | yes | `{reason}`: a waiting message was removed and will never start |
| `turn_started` | yes | `{source, prompt, job_id?, run_id?, job_name?}` |
| `item_started` | yes | `{item_id, item_type, ...summary}` (same fields as `item_completed`, possibly incomplete) |
| `item_completed` | yes | `{item_id, item_type, ...}`, see the item table below |
| `message_delta` | no | `{item_id, delta}` |
| `command_output` | no | `{item_id, delta}` |
| `notice` | yes | `{level: "info"|"warning"|"error", message}` |
| `turn_completed` | yes | `{status: "completed"|"failed"|"interrupted", final_response, error_kind, error_message}` |
| `agent_status` | yes | `{state, step, message}` where `state` is the agent's `setup_state` or a container phase (`starting`, `running`, `stopped`, `restarting`) and `step` is a short machine word (`files`, `image`, `container`, `probe`, `ready`, `error`) |
| `job_run_started` | yes (channel `agent`) | `{job_id, run_id, job_name, late, trigger}` |
| `job_run_finished` | yes (channel `agent`) | `{job_id, run_id, job_name, status, summary, error_message}` |
| `jobs_changed` | no (channel `agent`) | `{job_id}` |
| `snapshot` | no | `{turn_id, items: {item_id: partial_text}}`, sent once to a new subscriber while a turn is running |

Item types (`item_type`) and their fields, translated from the SDK's `event.payload.item.root`:

| SDK item | `item_type` | fields |
|---|---|---|
| `AgentMessageThreadItem` | `agent_message` | `text` |
| `ReasoningThreadItem` | `reasoning` | `text` (summary text joined, may be empty) |
| `CommandExecutionThreadItem` | `command` | `command`, `status`, `exit_code`, `output` (last 8000 chars of `aggregated_output`), `duration_ms` |
| `FileChangeThreadItem` | `file_change` | `status`, `changes: [{path, kind}]` |
| `McpToolCallThreadItem` | `mcp_tool_call` | `server`, `tool`, `status`, `arguments` (JSON text, max 2000 chars), `result_text` (text content joined, max 4000 chars), `error` |
| `WebSearchThreadItem` | `web_search` | `query` |
| `UserMessageThreadItem` | (skipped; the prompt is already in `turn_started`) | |
| anything else | `other` | `raw_type` |

Status enum values from the SDK (for example `McpToolCallStatus.completed`) are sent as their `.value` strings.

**Error kinds** (`turns.error_kind`, used by the UI to choose text and buttons): `unauthorized`, `rate_limited`, `model_unavailable`, `context_full`, `runtime_crashed`, `container_failed`, `timeout`, `interrupted`, `platform_restart`, `other`. Mapping from `TurnError.codex_error_info`: `unauthorized` → `unauthorized`; `usageLimitExceeded`, `rateLimitExceeded`, `sessionBudgetExceeded` → `rate_limited`; `serverOverloaded`, `internalServerError`, `badRequest`, `flexUnavailable` → `model_unavailable`; `contextWindowExceeded` → `context_full`; anything else → `other`. `openai_codex.TransportClosedError` (process died) → `runtime_crashed`. `AgentRuntimeError` → `container_failed`. Codex 0.163 reports provider HTTP failures as structured variants (for example `httpConnectionFailed` with `httpStatusCode`); their status code decides the kind: 401/403 → `unauthorized`, 429 → `rate_limited`, 404 and 5xx → `model_unavailable`. Codex retries a failing provider up to five times; the platform shows one "Reconnecting…" notice per turn, not one per retry.

**Agent-facing configuration.** `den/agent_files.py` writes `codex-home/config.toml` before every container start and every local launch:

```
model = "<agent model>"
model_provider = "den"            # proxy mode only

[model_providers.den]             # proxy mode only
name = "Den model proxy"
base_url = "<agent_backend_url>/model-proxy/v1"
env_key = "PLATFORM_TOKEN"

[mcp_servers.platform]
url = "<agent_backend_url>/mcp"
bearer_token_env_var = "PLATFORM_TOKEN"
```

In `chatgpt` mode the two provider sections are omitted (Codex then uses its default OpenAI provider with the mounted login), and the `mcp_servers` section stays.

**HTTP API.** All bodies are JSON unless noted. Errors use FastAPI's `{"detail": "..."}`. Every non-GET request under `/api/` must include the header `X-Den-CSRF: 1` or it gets 403. Every route under `/api/agents/{agent_id}` returns 404 when the agent does not exist **or** belongs to another user.

| method and path | body / query | success | notable errors |
|---|---|---|---|
| `GET /api/config` | | `{models: [{id, label}], runtime, model_auth, limits: {min_job_interval_minutes, max_upload_mb, max_agents_per_user}}` (no login needed) | |
| `POST /api/auth/register` | `{email, password, timezone?}` | 201 `{user}`, sets cookie | 400 invalid email/password (<8 chars), 409 email taken |
| `POST /api/auth/login` | `{email, password}` | 200 `{user}`, sets cookie | 401 wrong credentials, 429 more than 10 failures per email per 10 minutes |
| `POST /api/auth/logout` | | 204, clears cookie | |
| `GET /api/auth/me` | | `{user}` | 401 |
| `GET /api/agents` | | `{agents: [AgentOut]}` | |
| `POST /api/agents` | `{name, model}` | 201 `{agent}`; setup starts in the background | 400 bad name (1–40 chars after trim) or unknown model, 409 more than `max_agents_per_user` |
| `GET /api/agents/{agent_id}` | | `{agent}` | |
| `PATCH /api/agents/{agent_id}` | `{name?, model?}` | `{agent}` | 400 as above |
| `DELETE /api/agents/{agent_id}` | | 204; stops turns and runs, removes the container, rows and directory | |
| `POST /api/agents/{agent_id}/setup/retry` | | 202 `{agent}` | 409 unless `setup_state` is `error` |
| `POST /api/agents/{agent_id}/container/{action}` | action is `start`, `stop` or `restart` | `{agent}` | 400 unknown action, 409 setup not `ready`, 502 `AgentRuntimeError` |
| `POST /api/agents/{agent_id}/conversation/new` | | `{agent}` | 409 chat turn running |
| `GET /api/agents/{agent_id}/history` | `?limit=300` (max 1000) | `{events: [Event], last_seq, active: {turn_id, items} or null, queue: [{turn_id, prompt, priority, queued_at}]}`: the latest `limit` persisted events with channel `chat` or `agent`, oldest first | |
| `GET /api/agents/{agent_id}/events` | `?after=<seq>`; header `Last-Event-ID` wins | `text/event-stream` (see `den/chat.py`) | |
| `POST /api/agents/{agent_id}/messages` | `{text, mode?}`, text 1–20000 chars, mode `queue` (default) or `interrupt` | 202 `{turn_id, queued, position}`: runs at once when idle, otherwise waits in the queue | 400 bad mode, 409 queue full (10) or setup not ready |
| `POST /api/agents/{agent_id}/stop` | | `{stopped: bool}` (stops only the running turn; the queue continues) | |
| `DELETE /api/agents/{agent_id}/queue/{turn_id}` | | 204; cancels one waiting message | 404 not queued (any more) |
| `DELETE /api/agents/{agent_id}/queue` | | `{cancelled: n}` | |
| `GET /api/agents/{agent_id}/files` | `?path=` (relative; empty is the root) | `{path, entries: [{name, path, type: "file"|"dir"|"symlink"|"other", size, modified}]}`, directories first then by name | 400 bad path, 404 missing, 409 not a directory |
| `GET /api/agents/{agent_id}/files/preview` | `?path=` | `{path, kind: "text"|"image"|"binary", text?, truncated?, size}`; text is the first 1 MiB decoded as UTF-8 (`errors="replace"`) if the first 8 KiB has no NUL byte; `image` for `.png .jpg .jpeg .gif .webp` | 404, 409 directory |
| `GET /api/agents/{agent_id}/files/download` | `?path=&inline=0|1` | file bytes; `Content-Disposition: attachment` unless `inline=1` **and** the file is one of the four image types; always `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; sandbox` | 404, 409 directory |
| `POST /api/agents/{agent_id}/files/upload` | `?path=<dir>`, multipart field `files` (one or more) | 201 `{saved: [path]}`; existing files are replaced | 400 bad name, 413 over `max_upload_mb` |
| `POST /api/agents/{agent_id}/files/mkdir` | `{path}` | 201 `{path}` | 400, 409 exists |
| `DELETE /api/agents/{agent_id}/files` | `?path=` | 204; directories are removed recursively without following symlinks | 400 root or bad path, 404 |
| `GET /api/agents/{agent_id}/jobs` | | `{jobs: [JobOut]}`, active first, then newest | |
| `POST /api/agents/{agent_id}/jobs` | `{name, prompt, schedule: {kind: "once", at} or {kind: "interval", minutes} or {kind: "cron", expr}, timezone?}` | 201 `{job}` | 400 validation message from `validate_schedule`, 409 too many active jobs |
| `DELETE /api/agents/{agent_id}/jobs/{job_id}` | | `{job}`; status `cancelled`, `next_run_at` NULL, any queued or running run is stopped | 404 |
| `POST /api/agents/{agent_id}/jobs/{job_id}/run` | | 202 `{run}`; a manual run now, not counted as the scheduled occurrence | 409 job not active, or a run is queued or running |
| `GET /api/agents/{agent_id}/jobs/{job_id}/runs` | `?limit=20` | `{runs: [RunOut]}` newest first | |
| `GET /api/agents/{agent_id}/runs/{run_id}` | | `{run, events: [Event]}` (events of the run's turn) | 404 |
| `POST /mcp` | JSON-RPC, `Authorization: Bearer <agent token>` | see `den/mcp_server.py` | 401 bad token |
| `GET|DELETE /mcp` | | 405 | |
| `GET|POST /model-proxy/v1/{path}` | `Authorization: Bearer <agent token>` | upstream response streamed | 401, 403 model not allowed, 404 path not allowed, 502 upstream unreachable |

`AgentOut` = `{id, name, model, setup_state, setup_detail, container: "running"|"stopped"|"missing"|"unknown", chat_busy, active_turn_id, queued_count, conversation_no, created_at, last_active_at}`.
`JobOut` = `{id, name, prompt, schedule: {kind, value, description, timezone}, created_by, status, next_run_at, last_run: RunOut|null, created_at, cancelled_at}`.
`RunOut` = `{id, job_id, status, late, trigger, scheduled_for, started_at, finished_at, summary, error_message, turn_id}`.
`user` = `{id, email, timezone}`.

### 4.2 Backend files

#### `den/__init__.py` (new)

- **What changes:** package marker with `__version__ = "0.1.0"`.
- **Why:** makes `den` importable for `python -m den` and tests.

#### `den/__main__.py` (new)

- **What changes:** `main()` loads settings with `load_settings()`, prints a warning banner when `settings.runtime == "local"` ("LOCAL RUNTIME: agents run on this machine without containers. Development and tests only."), and runs `uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1, log_level="info")`.
- **Why:** one documented way to start the platform; one worker because running turns, the event bus and the scheduler live in process memory (Technical Plan, "Key decisions").

#### `den/config.py` (new)

- **What changes:** a frozen dataclass `Settings` and `load_settings(env: Mapping[str, str] = os.environ) -> Settings`. Fields, their environment variables and defaults:
  - `data_dir: Path` (`DEN_DATA_DIR`, `~/.local/share/den`, resolved to an absolute path; outside the repository because Codex looks for `AGENTS.md` from a workspace up to its git root, and must not find this project's course instructions)
  - `runtime: str` (`DEN_RUNTIME`, `docker`; `local` allowed; anything else raises `ValueError`)
  - `host: str` (`DEN_HOST`; default `127.0.0.1` for both runtimes: Docker Desktop containers reach a WSL server bound to 127.0.0.1 through `host.docker.internal` (verified), which keeps `/mcp` and `/model-proxy` off the network; native Linux Docker needs `0.0.0.0` or the bridge IP; an empty value means the default), `port: int` (`DEN_PORT`, `8000`)
  - `agent_backend_url: str` (`DEN_AGENT_BACKEND_URL`; default `http://host.docker.internal:<port>` for docker, `http://127.0.0.1:<port>` for local): the URL agents use for the model proxy and MCP
  - `model_auth: str` (`DEN_MODEL_AUTH`, `proxy`; or `chatgpt`)
  - `upstream_base_url: str` and `upstream_api_key: str|None`: `DEN_UPSTREAM_BASE_URL` and `DEN_UPSTREAM_API_KEY` if set; otherwise if `UMGPT_TOOLKIT_API_KEY` is set use it with `https://api.toolkit.umgpt.umich.edu/v1`; otherwise if `OPENAI_API_KEY` is set use it with `https://api.openai.com/v1`; otherwise base `https://api.openai.com/v1` and key `None` (the proxy then answers 502 "no upstream key configured")
  - `chatgpt_auth_json: Path` (`DEN_CHATGPT_AUTH_JSON`, `~/.codex/auth.json`)
  - `models: tuple[str, ...]` (`DEN_MODELS`, comma separated, default `gpt-6.1-sol,gpt-6-luna`) and `model_labels: dict[str, str]` (built-in labels "GPT-6.1 Sol (strongest)" and "GPT-6 Luna (fast, low cost)"; unknown ids are labelled with the id)
  - `agent_image: str` (`DEN_AGENT_IMAGE`, `den-agent:0.163.0-2`), `docker_network: str` (`den-agents`), `container_memory: str` (`2g`), `container_cpus: str` (`1.0`), `container_pids: int` (`512`), `container_uid_gid: str` (`DEN_CONTAINER_USER`, default `f"{os.getuid()}:{os.getgid()}"`)
  - `codex_bin: str|None` (`DEN_CODEX_BIN`; local runtime only; default `None`, meaning the binary bundled with the SDK)
  - `idle_stop_minutes: int` (`DEN_IDLE_STOP_MINUTES`, 15), `min_job_interval_minutes: int` (`DEN_MIN_JOB_INTERVAL_MINUTES`, 5), `max_jobs_per_agent: int` (20), `max_agents_per_user: int` (`DEN_MAX_AGENTS_PER_USER`, 5), `max_concurrent_job_runs: int` (`DEN_MAX_CONCURRENT_JOB_RUNS`, 4), `job_run_timeout_minutes: int` (`DEN_JOB_RUN_TIMEOUT_MINUTES`, 30), `missed_run_grace_hours: int` (24), `onboarding_probe: bool` (`DEN_ONBOARDING_PROBE`, true), `probe_timeout_seconds: int` (90), `max_upload_mb: int` (`DEN_MAX_UPLOAD_MB`, 100), `secure_cookies: bool` (`DEN_SECURE_COOKIES`, false), `session_days: int` (30), `scheduler_max_sleep_seconds: float` (30.0)
  - Booleans parse `1/true/yes/on` case-insensitively.
  - Method `settings.label_for(model_id) -> str`.
  - Tests build settings with `dataclasses.replace(load_settings({...}), ...)`.
- **Why:** every tunable named in the Technical Plan in one place; the upstream presets implement "the upstream is the U-M GPT Toolkit or OpenAI, chosen by environment variables".

#### `den/db.py` (new)

- **What changes:**
  - `utcnow() -> datetime` (aware, UTC), `iso(dt: datetime) -> str` (converts to UTC and uses `isoformat(timespec="microseconds")`), `parse_iso(s: str) -> datetime`, `new_id() -> str` (`uuid.uuid4().hex`).
  - `class Database`: `__init__(path: Path)` opens one `sqlite3` connection with `check_same_thread=False`, `isolation_level=None` (autocommit), `row_factory=sqlite3.Row`, and a `threading.RLock`. Methods: `execute(sql, params=()) -> sqlite3.Cursor`, `one(sql, params=()) -> dict|None`, `all(sql, params=()) -> list[dict]`, `transaction()` (context manager that holds the lock and wraps `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`; nested use just reuses the outer transaction), `migrate()` (creates the schema in 4.1 with `CREATE TABLE IF NOT EXISTS` and the indexes, and records `PRAGMA user_version = 1`), `close()`. Every method takes the lock, so calls from the event loop and from worker threads are serialized.
- **Why:** SQLite is the single source of durable truth (Technical Plan, "Key decisions"). One locked connection is the simplest correct choice at this scale; each call is a few milliseconds and is called directly from async code.

#### `den/security.py` (new)

- **What changes:**
  - `hash_password(pw: str) -> str` using `hashlib.scrypt(n=2**14, r=8, p=1, dklen=32)` with a 16-byte random salt, encoded `scrypt$16384$8$1$<salt b64>$<hash b64>`; `verify_password(pw, encoded) -> bool` with `hmac.compare_digest`.
  - `new_session_token() -> tuple[str, str]` returns `(token, sha256_hex(token))` where token is `secrets.token_urlsafe(32)`; `hash_token(token) -> str`.
  - `load_or_create_secret(data_dir: Path) -> bytes` reads `data_dir/secret.key` or writes 32 random bytes there with mode 0600.
  - `make_agent_token(secret: bytes, agent_id: str, version: int) -> str` returns `f"den_{agent_id}_{version}_{hmac_sha256(secret, f'{agent_id}:{version}').hexdigest()}"`.
  - `parse_agent_token(secret: bytes, token: str) -> tuple[str, int] | None` checks the format and the HMAC (with `compare_digest`) and returns `(agent_id, version)`; the caller still checks the version against the database.
  - `class LoginLimiter`: in-memory, `check(email) -> bool` (False when 10 failures in the last 10 minutes), `fail(email)`, `reset(email)`.
- **Why:** "Accounts and isolation" and "Agent tokens" in the Technical Plan.

#### `den/events.py` (new)

- **What changes:**
  - `class EventBus`: `subscribe(agent_id) -> Subscription` (an async context manager exposing `queue: asyncio.Queue` with `maxsize=2000` and a flag `overflowed`), `publish(event: dict)` puts the event into every subscriber queue for `event["agent_id"]` with `put_nowait`; on `QueueFull` it sets `overflowed` and puts nothing more for that subscriber (the SSE handler then ends the stream so the browser reconnects and replays from the database). `publish` must be called from the event loop thread.
  - `PERSISTED_KINDS` (set of kinds marked "persisted" in 4.1).
  - `def emit(db, bus, *, agent_id, turn_id, channel, kind, data) -> dict` builds the event, inserts it into `events` when `kind in PERSISTED_KINDS` (filling `seq` from `lastrowid`), publishes it, and returns it.
  - `def row_to_event(row: dict) -> dict` converts an `events` row to the event shape.
- **Why:** step 4 and 5 of "Chat: how a message becomes a streamed reply"; one function to persist and publish so they never diverge.

#### `den/context.py` (new)

- **What changes:** `@dataclass class AppContext` with `settings`, `db: Database`, `bus: EventBus`, `runtime: AgentRuntime`, `secret: bytes`, `turns: TurnRegistry` (from `codex_bridge`), `scheduler: Scheduler | None` (set after construction), `login_limiter: LoginLimiter`, `background: set[asyncio.Task]`. Helpers: `spawn(coro, *, name) -> asyncio.Task` (keeps a reference in `background`, removes it when done, and logs any exception with `logger.exception`), `agent_token(agent: dict) -> str`, and `get_ctx(request) -> AppContext` (returns `request.app.state.ctx`) for use as a FastAPI dependency.
- **Why:** one object that routers and background tasks share instead of module globals, so tests can build several independent apps.

#### `den/agent_files.py` (new)

- **What changes:**
  - `@dataclass(frozen=True) class AgentPaths`: `root`, `workspace`, `codex_home`, `home`.
  - `agent_paths(settings, agent_id) -> AgentPaths` (`data_dir/agents/<agent_id>/...`; validates that `agent_id` is 32 hex characters).
  - `init_agent_dirs(paths)` creates the three directories with mode 0700 for `root` and 0755 for the children.
  - `write_codex_config(settings, paths, model: str)` writes `config.toml` as in 4.1, writing to a temporary file and renaming it.
  - `remove_agent_dirs(paths)` uses `shutil.rmtree` (which does not follow symlinks on Linux).
  - `prepare_scratch_home(settings, paths, model, key) -> str` creates `codex-home/runs/<key>/` with its own `config.toml` (and, in `chatgpt` mode, a relative `auth.json` link to the parent's) and returns the subdirectory; `remove_scratch_home(paths, key)` deletes it. `AgentRuntime.codex_config(agent, codex_home_subdir=None)` launches app-server with that directory as `CODEX_HOME`; `TurnRunner(..., scratch_home=True)` uses it for job runs and the probe.
  - `developer_instructions(agent_name: str, *, purpose: str, job_name: str | None = None) -> str` where `purpose` is `chat`, `job` or `probe`. The text names the agent; says the user's files live in the current working directory (the workspace) and that anything the user should see must be saved there; says it runs in its own container; describes the three platform tools by their exact nested names `tools.mcp__platform__schedule_job`, `tools.mcp__platform__list_jobs` and `tools.mcp__platform__cancel_job` (callable from the `exec` tool), their arguments, that each run starts a fresh conversation (so job prompts must be self-contained), and that the user can see and cancel every job. For `job` it adds: "This is an unattended scheduled run of the job '<name>'. Nobody is watching. Do the task, then end with a short summary of what you did; that summary is shown to the user as the job's result." For `probe`: "Reply with the single word: ready."
- **Why:** "What an agent is", "Agent-facing configuration" and the MCP section of the Technical Plan.

#### `den/runtime/__init__.py` (new)

- **What changes:** `make_runtime(settings, secret, db) -> AgentRuntime` returns `DockerRuntime` or `LocalRuntime` according to `settings.runtime`.
- **Why:** one switch for the pluggable runtime.

#### `den/runtime/base.py` (new)

- **What changes:**
  - `class AgentRuntimeError(Exception)` with `message: str` (user-facing) and `detail: str` (stderr tail for logs).
  - Abstract `class AgentRuntime` with: `name: str`; `async prepare() -> None` (one-time host setup, idempotent); `async ensure_running(agent: dict) -> None` (creates or starts as needed, then writes nothing else); `async stop(agent_id: str) -> None`; `async restart(agent: dict) -> None` (remove, then `ensure_running`); `async remove(agent_id: str) -> None`; `async status(agent_id: str) -> str` returning `running`, `stopped`, `missing` or `unknown`; `codex_config(agent: dict) -> CodexConfig`; `workspace_cwd(agent: dict) -> str`.
  - `agent` dicts are `agents` rows. Implementations hold one `asyncio.Lock` per agent id so concurrent `ensure_running`, `stop` and `restart` calls for one agent serialize.
- **Why:** lets the product run on Docker and tests run without it (Technical Plan, "Development runtime and testing").

#### `den/runtime/docker.py` (new)

- **What changes:** `class DockerRuntime(AgentRuntime)`, `name = "docker"`, constructed with `(settings, secret, db)`.
  - `container_name(agent_id) -> str` = `f"den-agent-{agent_id}"`.
  - `async _docker(*args, check=True, timeout=120) -> tuple[int, str, str]` runs `docker <args>` with `asyncio.create_subprocess_exec`, captures output, and on non-zero exit with `check=True` raises `AgentRuntimeError(message=<first line of stderr>, detail=<last 2000 chars>)`. A missing `docker` binary or an unreachable daemon raises `AgentRuntimeError("Docker is not available on the server: ...")`.
  - `async prepare()`: if `docker image inspect <image>` fails, runs `docker build -t <image> <repo>/container` (timeout 900 s); creates the network with `docker network create --driver bridge -o com.docker.network.bridge.enable_icc=false den-agents` when `docker network inspect den-agents` fails. Guarded by a lock and a `prepared` flag; `ensure_running` awaits it.
  - `async status(agent_id)`: `docker inspect -f {{.State.Status}} <name>`; `running` → `running`; `created`/`exited`/`paused`/`dead` → `stopped`; "No such object" → `missing`; other failures → `unknown`.
  - `run_args(agent) -> list[str]` (pure function, unit tested) builds: `run -d --name <name> --label den.managed=1 --label den.agent_id=<id> --user <uid:gid> --memory <m> --memory-swap <m> --cpus <c> --pids-limit <n> --cap-drop ALL --security-opt no-new-privileges --network den-agents --add-host host.docker.internal:host-gateway -e CODEX_HOME=/codex -e HOME=/home/agent -e PLATFORM_TOKEN=<make_agent_token(secret, id, token_version)> --mount type=bind,src=<workspace>,dst=/workspace --mount type=bind,src=<codex_home>,dst=/codex [--mount type=bind,src=<chatgpt_auth_json>,dst=/codex/auth.json,readonly when model_auth is chatgpt] -w /workspace <image> sleep infinity`.
  - `async ensure_running(agent)`: under the agent lock, `prepare()`, `write_codex_config`, then by status: `missing` → `docker run`; `stopped` → `docker start`; `running` → nothing.
  - `async stop(agent_id)`: `docker stop -t 5 <name>`, ignoring "No such container".
  - `async remove(agent_id)`: `docker rm -f <name>`, ignoring "No such container".
  - `async restart(agent)`: bumps `agents.token_version` by 1 (revoking the old token), `remove`, then `ensure_running` with the updated row.
  - `codex_config(agent) -> CodexConfig(launch_args_override=("docker", "exec", "-i", <name>, "codex", "app-server", "--listen", "stdio://"))`.
  - `workspace_cwd(agent) -> "/workspace"`.
- **Why:** "Containers" in the Technical Plan; the flags implement the limits and isolation listed there; the launch matches `docs/codex-sdk.md`, "Connecting to a container".

#### `den/runtime/local.py` (new)

- **What changes:** `class LocalRuntime(AgentRuntime)`, `name = "local"`. No containers: `prepare` does nothing; `ensure_running` writes `config.toml` and initializes directories; `stop`/`remove` do nothing; `restart` bumps `token_version`; `status` returns `running`. `codex_config(agent)` returns `CodexConfig(launch_args_override=("env", "-i", f"PATH={os.environ['PATH']}", f"HOME={paths.home}", f"CODEX_HOME={paths.codex_home}", f"PLATFORM_TOKEN={token}", <codex binary>, "app-server", "--listen", "stdio://"), cwd=str(paths.workspace))`. The binary is `settings.codex_bin` or `codex_cli_bin.bundled_codex_path()`. In `chatgpt` mode it symlinks `codex_home/auth.json` to `settings.chatgpt_auth_json` if not present. `workspace_cwd(agent)` returns the host workspace path.
- **Why:** lets the full chain (real app-server, real SDK, MCP, model proxy) run in tests and on a machine without Docker. `env -i` keeps the backend's own environment, including any real key, out of the Codex process.

#### `den/codex_bridge.py` (new)

- **What changes:**
  - `class Busy(Exception)`.
  - `@dataclass class TurnOutcome`: `status` (`completed`/`failed`/`interrupted`), `final_response: str|None`, `error_kind: str|None`, `error_message: str|None`, `thread_id: str|None`.
  - `translate_item(item) -> dict` implements the item table in 4.1 (pure function, unit tested with SDK model instances).
  - `classify_turn_error(error: TurnError|None, model: str) -> tuple[str, str]` and `classify_exception(exc: BaseException, model: str) -> tuple[str, str]` return `(error_kind, user_message)` using the error-kind mapping in 4.1 and the messages table in the Technical Plan.
  - `class TurnRunner`: `__init__(ctx, *, agent: dict, turn_id: str, prompt: str, source: str, channel: str, thread_id: str|None, developer_instructions: str, ephemeral: bool = False, on_thread_started: Callable[[str], None] | None = None)`.
    - `async run() -> TurnOutcome`:
      1. Calls `ctx.runtime.ensure_running(agent)` and emits `agent_status` `{state: "starting", step: "container", message: "Starting the agent's container…"}` only when `status()` was not `running`.
      2. Opens `AsyncCodex(ctx.runtime.codex_config(agent))`.
      3. If `thread_id` is set, calls `thread_resume(thread_id, model=agent["model"], cwd=..., sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all, developer_instructions=...)`. If that raises a `CodexError` whose message says the thread does not exist (`no rollout found for thread id` or `invalid session id`, which is what Codex 0.163 answers), it emits a warning `notice` ("The previous conversation could not be restored, so a new one was started.") and falls back to `thread_start` with the same options plus `ephemeral`. Any other resume error fails the turn instead, because starting a new thread would overwrite `chat_thread_id` and lose the real conversation. When a new thread starts, it calls `on_thread_started(thread.id)`.
      4. `turn = await thread.turn(prompt)`; stores `codex_thread_id`/`codex_turn_id` on the `turns` row.
      5. `async for ev in turn.stream()`: `item/started` → `item_started`; `item/completed` → `item_completed` (and remembers the last `agent_message` text as the final response); `item/agentMessage/delta` → `message_delta` (and appends to `self.partial[item_id]`); `item/commandExecution/outputDelta` → `command_output`; `error` with `will_retry` → `notice` warning "Reconnecting to the model… (<message>)", without `will_retry` → remembered as the turn error; `turn/completed` → final status from `payload.turn.status` and error from `payload.turn.error`. Other methods are ignored.
      6. On `TransportClosedError` or any `CodexError`: if `self.stop_requested` the outcome is `interrupted` (`error_kind="interrupted"`, message "Stopped."); otherwise `failed` with `classify_exception`. On `AgentRuntimeError`: `failed` with `container_failed` and the error's message.
      7. `finally`: closes the client, clears `partial`, and updates the `turns` row (`status`, `final_response`, `error_kind`, `error_message`, `finished_at`) and `agents.last_active_at`; emits `turn_completed`; when status is `failed` also emits a `notice` with level `error` and the user message.
    - `async stop(reason: str = "user") -> None`: sets `stop_requested`, then if a turn handle exists `await asyncio.wait_for(turn.interrupt(), 5)` (ignoring errors), then `await codex.close()`. Safe to call before the turn has started (the run then ends as `interrupted` right after start).
    - `snapshot() -> dict` returns `{"turn_id", "items": dict(self.partial)}`.
  - `class TurnRegistry`: `chat: dict[str, TurnRunner]` (by agent id), `runs: dict[str, TurnRunner]` (by job run id). `chat_busy(agent_id) -> bool`. `async stop_agent(agent_id)` stops the chat runner and every run runner whose agent matches. `async stop_all()`.
  - `async def start_chat_turn(ctx, agent: dict, text: str) -> str`: raises `Busy` if `ctx.turns.chat_busy(agent_id)`; inserts the `turns` row (`source="chat"`, `status="running"`, `conversation_no` from the agent); emits `turn_started` (`channel="chat"`); creates the runner with `thread_id=agent["chat_thread_id"]`, `developer_instructions(purpose="chat")`, and `on_thread_started` saving `agents.chat_thread_id`; registers it in `ctx.turns.chat` **before** spawning `runner.run()` with `ctx.spawn`; the task removes the registry entry when done. Returns the turn id.
  - `async def run_probe(ctx, agent: dict) -> TurnOutcome`: a `probe` turn on an ephemeral new thread with a timeout of `settings.probe_timeout_seconds` (on timeout it stops the runner and returns `failed`/`timeout` "The model did not answer within 90 seconds.").
- **Why:** this is the "Codex connection" box of the spec and steps 2–6 of "Chat: how a message becomes a streamed reply", plus Stop and the failure messages.

#### `den/scheduler.py` (new)

- **What changes:**
  - `class ScheduleError(ValueError)`.
  - `normalize_schedule(kind: str, raw: dict|str|int, tz: str, settings, now: datetime) -> str` validates and returns `schedule_value`: `once` takes an ISO 8601 string (a time without an offset is read in `tz`), must be no more than 60 s in the past and no more than 366 days ahead, and is stored as UTC ISO; `interval` takes integer minutes from `settings.min_job_interval_minutes` to 525600; `cron` takes a 5-field expression valid for `croniter`, and the gap between each of its next 6 occurrences must be at least `min_job_interval_minutes`. `tz` must be a valid `zoneinfo` key. Error messages are full sentences shown to the user and to the agent.
  - `compute_next_run(kind, value, tz, *, after: datetime) -> datetime|None`: `once` → the time if it is after `after - 60 s`, else `None`; `interval` → `after + minutes`; `cron` → `croniter(value, after.astimezone(ZoneInfo(tz))).get_next(datetime)` converted to UTC.
  - `describe_schedule(kind, value, tz) -> str` (for example "Once at 2026-11-06 09:00 (America/Detroit)", "Every 60 minutes", "Cron `0 9 * * 1-5` (America/Detroit)").
  - `create_job(ctx, *, agent: dict, name: str, prompt: str, kind: str, raw_value, tz: str, created_by: str, source_turn_id: str|None) -> dict`: trims and checks `name` (1–80 chars) and `prompt` (1–8000 chars), counts active jobs (`max_jobs_per_agent`), normalizes the schedule, sets `next_run_at` (for `interval` this is now plus the interval), inserts the row, emits `jobs_changed`, calls `ctx.scheduler.wake()`, and returns the row.
  - `job_out(ctx, job_row) -> dict` and `run_out(run_row) -> dict` build `JobOut`/`RunOut`.
  - `class Scheduler`:
    - `__init__(ctx)`; `start()` spawns the loop; `async stop()` cancels it; `wake()` sets an `asyncio.Event`.
    - `recover_on_startup()`: in one transaction, marks every `job_runs` row with status `queued` or `running` as `interrupted` (`error_message` "The platform restarted while this run was in progress.", `finished_at` now) and every `turns` row with status `running` as `interrupted` with `error_kind="platform_restart"` and the same message; sets each affected job's `last_run_id`; for every `once` job left `active` with `next_run_at` NULL and an interrupted run, sets status `finished`.
    - Loop: `due = SELECT * FROM jobs WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at <= now ORDER BY next_run_at`. For each, call `fire(job, now)`. Then sleep until the earliest remaining `next_run_at`, at most `scheduler_max_sleep_seconds`, or until `wake()`.
    - `fire(job, now)` (one transaction per job): `scheduled_for = job.next_run_at`; compute the job's following time with `compute_next_run(..., after=max(now, scheduled_for))` for `interval`/`cron` and `None` for `once`, and store it as `next_run_at`. Then:
      - if `now - scheduled_for > missed_run_grace_hours`: insert a run with status `missed` (`summary` "Missed: the platform was not running at the scheduled time."), and set `status='finished'` for a `once` job;
      - else if the job has a run with status `queued` or `running`: insert a run with status `skipped` (`summary` "Skipped: the previous run was still going.");
      - else insert a run with status `queued`, `late = 1` if `now - scheduled_for > 60 s`, `trigger='schedule'`, and spawn `execute(run_id)`.
      - Set `jobs.last_run_id` to the new run and emit `jobs_changed`.
    - `async run_now(job) -> dict`: 409-style `ScheduleError` if a run is queued or running or the job is not active; otherwise inserts a `manual` queued run and spawns `execute`.
    - `async execute(run_id)`: waits on a semaphore of size `max_concurrent_job_runs`; re-reads the run and returns if it is no longer `queued` (cancelled meanwhile); inserts a `turns` row (`source="job"`, `job_run_id`), sets the run to `running` with `turn_id` and `started_at`, emits `job_run_started` (channel `agent`) and `turn_started` (channel `job`); builds a `TurnRunner` (`thread_id=None`, `developer_instructions(purpose="job", job_name=...)`), registers it in `ctx.turns.runs[run_id]`, and awaits `runner.run()` under `asyncio.wait_for(..., job_run_timeout_minutes * 60)`; on timeout it calls `runner.stop("timeout")` and records `failed` with "The run took longer than 30 minutes and was stopped." Outcome → run status: `completed` → `succeeded`; `failed` → `failed`; `interrupted` → `interrupted`. `summary` = the first 500 characters of the final response; `error_message` from the outcome. Updates the run, sets a `once` job to `finished`, emits `job_run_finished` and `jobs_changed`, and removes the registry entry.
    - `async cancel_job(job_id)`: sets the job `cancelled`, `cancelled_at` now, `next_run_at` NULL; marks a `queued` run `interrupted` ("Cancelled before it started."); stops a running run's runner (its outcome becomes `interrupted`); emits `jobs_changed`.
- **Why:** "Jobs and the scheduler" in the Technical Plan, including the missed-run, overlap, concurrency and timeout rules, shared by the user API and MCP.

#### `den/lifecycle.py` (new)

- **What changes:**
  - `async def setup_agent(ctx, agent_id)`: the onboarding pipeline. Emits `agent_status` (`state="creating"`) for each step: `files` (`init_agent_dirs`, `write_codex_config`), `image` (`runtime.prepare()`, message "Preparing the agent image (first time only, can take a few minutes)…"), `container` (`ensure_running`), `probe` (`run_probe` when `settings.onboarding_probe`; failure message comes from the probe outcome), then sets `setup_state='ready'` and emits `{state: "ready", step: "ready"}`. On any failure it sets `setup_state='error'` and `setup_detail` to `"<step>: <message>"` and emits `{state: "error", step: <step>, message}`.
  - `async def idle_reaper(ctx)`: every 60 s, when `runtime.name == "docker"`, stops the container of every `ready` agent that has no chat runner, no run runner, and `last_active_at` older than `idle_stop_minutes`, emitting `agent_status` `{state: "stopped", step: "container", message: "Stopped after <n> idle minutes. It starts again by itself when needed."}` only when `status()` was `running`.
  - `async def delete_agent(ctx, agent)`: `turns.stop_agent`, cancels the agent's jobs in the scheduler, `runtime.remove`, deletes the `agents` row (cascades), `remove_agent_dirs`.
  - `async def agent_out(ctx, agent_row) -> dict` builds `AgentOut` (container status from `runtime.status`, `chat_busy` and `active_turn_id` from the registry).
- **Why:** "Onboarding" and the idle part of "Containers" in the Technical Plan; deletion in one place so nothing is left behind.

#### `den/auth.py` (new)

- **What changes:** `router = APIRouter(prefix="/api/auth")` with the four auth routes in 4.1. Email: trimmed, lowercased, must match `^[^@\s]+@[^@\s]+\.[^@\s]+$`, max 254 chars. Password: 8–200 chars. Timezone: kept if it is a valid `zoneinfo` key, otherwise `UTC`. Cookie `den_session`: `HttpOnly`, `SameSite=Lax`, `Secure` when `settings.secure_cookies`, `Max-Age` of `session_days`, path `/`. Login uses `LoginLimiter`. Dependency `current_user(request, ctx) -> dict` reads the cookie, looks up `sessions` by token hash with `expires_at > now`, joins `users`, and raises 401 otherwise. Dependency `owned_agent(agent_id: str, user=Depends(current_user), ctx=Depends(get_ctx)) -> dict` returns the `agents` row `WHERE id=? AND user_id=?` or raises 404 "Agent not found". `user_out(row)`.
- **Why:** "Accounts and isolation"; `owned_agent` is the single dependency every agent route uses.

#### `den/agents.py` (new)

- **What changes:** `router = APIRouter(prefix="/api")` with `GET /api/config`, and the agent routes in 4.1 except the chat, files and jobs routes. `POST /api/agents` inserts the row with `setup_state='creating'` and spawns `lifecycle.setup_agent`. `PATCH` validates name and model; a model change rewrites `config.toml` and applies on the next turn (the model is also passed to `thread_resume`). `container/{action}`: `start` → `ensure_running`, `stop` → stops the chat runner and every run runner of the agent, then `runtime.stop`, `restart` → stops them likewise, then `runtime.restart`; each emits an `agent_status` event. `conversation/new` clears `chat_thread_id`, increments `conversation_no`, and emits a `notice` (`info`, channel `chat`, "Started a new conversation.").
- **Why:** "Onboarding" and "Containers" (launch, stop and restart one container per agent).

#### `den/chat.py` (new)

- **What changes:** `router = APIRouter(prefix="/api/agents/{agent_id}")` with `history`, `events`, `messages` and `stop`, all depending on `owned_agent`.
  - `messages` returns 409 "This agent is still being set up." unless `setup_state == 'ready'`, and maps `Busy` to 409 "The agent is already working on a message. Stop it or wait for it to finish."
  - `events` returns a `StreamingResponse` with media type `text/event-stream` and headers `Cache-Control: no-cache` and `X-Accel-Buffering: no`. The generator: (1) subscribe to the bus **first**; (2) replay persisted events for the agent with `seq > after` in batches of 500 (all channels), each as `id: <seq>\ndata: <json>\n\n`; (3) if a chat or run runner is active for the agent, send one `snapshot` event per runner; (4) loop on the queue with a 15-second timeout, sending `: ping\n\n` on timeout, skipping persisted events whose `seq` was already replayed, sending persisted events with `id:` and live ones without; (5) stop when the client disconnects (`await request.is_disconnected()` checked on each timeout) or when the subscription overflowed.
- **Why:** steps 1, 5 and Stop of "Chat: how a message becomes a streamed reply"; replay-then-live is what makes a reopened tab catch up.

#### `den/files.py` (new)

- **What changes:** `router = APIRouter(prefix="/api/agents/{agent_id}/files")` with the five file routes in 4.1, all depending on `owned_agent`, plus these helpers:
  - `split_rel_path(raw: str) -> list[str]` strips leading and trailing `/` and rejects (400) empty components, `.`, `..`, NUL bytes, backslashes, components over 255 bytes and paths over 4096 characters.
  - `open_dir_fd(root: Path, parts: list[str]) -> int` opens `root` with `O_RDONLY|O_DIRECTORY`, then each component with `os.open(name, O_RDONLY|O_DIRECTORY|O_NOFOLLOW, dir_fd=parent)`, closing parents as it goes; a symlink or missing component raises 404.
  - Listing uses `os.scandir(fd)` and `entry.is_symlink()`/`is_dir(follow_symlinks=False)`; symlinks are listed with type `symlink` and are never followed by any route.
  - Reading opens the final component with `O_RDONLY|O_NOFOLLOW` relative to its parent fd, checks `S_ISREG` with `os.fstat`, and streams it in 64 KiB chunks.
  - Upload writes each file to `.<name>.upload-<random>` in the target directory (opened with `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0644), counts bytes and aborts with 413 past `max_upload_mb`, then `os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)`. File names come from the multipart filename's last path component and go through the same component checks.
  - `mkdir` uses `os.mkdir(name, 0o755, dir_fd=parent)`.
  - Delete uses `os.unlink(name, dir_fd=parent)` for files and symlinks, and `shutil.rmtree(name, dir_fd=parent)` for directories.
  - Blocking file operations run in `asyncio.to_thread` (the download stream reads chunks in a thread).
- **Why:** "Files" in the Technical Plan (browse, open, upload, download); the fd-relative walk is what keeps symlinks the agent creates from reaching host files.

#### `den/jobs.py` (new)

- **What changes:** `router = APIRouter(prefix="/api/agents/{agent_id}")` with the job and run routes in 4.1, all depending on `owned_agent`. Every job lookup is `WHERE id=? AND agent_id=?` and every run lookup is `WHERE id=? AND agent_id=?`. `POST jobs` defaults `timezone` to the user's timezone and calls `scheduler.create_job(..., created_by="user")`, mapping `ScheduleError` to 400 (or 409 for the job limit). `DELETE` calls `ctx.scheduler.cancel_job`. `run` calls `ctx.scheduler.run_now`. `runs/{run_id}` returns the run plus `row_to_event` for every event with that run's `turn_id`, ordered by `seq`.
- **Why:** "Jobs" in the spec: schedule, see and cancel any of the agent's jobs, each with its next run and last result.

#### `den/mcp_server.py` (new)

- **What changes:** `router = APIRouter()` with `POST /mcp`, plus `GET /mcp` and `DELETE /mcp` answering 405.
  - Auth: `Authorization: Bearer <token>` → `parse_agent_token`, then the `agents` row with matching `token_version` and `setup_state != 'error'`; otherwise HTTP 401 with a JSON-RPC error body.
  - Body: a single JSON-RPC 2.0 object (arrays get error -32600). A message without `id` (notification) gets HTTP 202 and an empty body. `initialize` returns `{protocolVersion: <client's requested version>, capabilities: {tools: {listChanged: false}}, serverInfo: {name: "den-platform", version}}`. `ping` returns `{}`. `tools/list` returns the three tools with JSON Schemas. `tools/call` dispatches by name; unknown methods get -32601, bad params -32602.
  - Tools:
    - `schedule_job({name, prompt, in_minutes?, at?, every_minutes?, cron?, timezone?})`: exactly one of the four schedule fields; `in_minutes` (1–525600) becomes `once` at now plus N minutes. `timezone` defaults to the owner's timezone. Calls `create_job(created_by="agent", source_turn_id=<our turn id for _meta.x-codex-turn-metadata.turn_id, if known>)`. Returns text "Scheduled job <id> '<name>': <description>. Next run: <time in the job's timezone>."
    - `list_jobs({})`: JSON text of the agent's active jobs: `id`, `name`, `schedule` description, `next_run_at`, `created_by`, last run status and summary.
    - `cancel_job({job_id})`: the job must belong to this agent; if `created_by == "user"` it returns an error result "Only the user can cancel this job because they created it."; otherwise calls `cancel_job` and returns "Cancelled job <id>."
  - Tool failures (for example a `ScheduleError`) return `{content: [{type: "text", text: <message>}], isError: true}` so the model can read the reason and try again.
- **Why:** "Agents scheduling their own jobs (MCP)"; the response codes follow what `docs/codex-sdk.md` verified, and the test suite confirms Codex 0.163 calls it under `deny_all`.

#### `den/model_proxy.py` (new)

- **What changes:** `router = APIRouter()` with `api_route("/model-proxy/v1/{path:path}", methods=["GET", "POST"])`.
  - Auth: the same agent token check as MCP; 401 on failure.
  - Allowed paths: `responses`, anything under `responses/`, `models`, anything under `models/`, and `chat/completions`; others 404.
  - For POST with a JSON body containing `model`, 403 with `{"error": {"message": "Model '<m>' is not offered on this platform."}}` unless `model in settings.models`.
  - 502 `{"error": {"message": "No upstream API key is configured on the platform."}}` when `upstream_api_key` is `None`, or when `settings.model_auth == "chatgpt"`.
  - Forwards to `f"{upstream_base_url}/{path}"` with the query string, the body unchanged, and headers: `Authorization: Bearer <upstream key>`, plus these from the request when present: `content-type`, `accept`, `openai-beta`, `user-agent`, `originator`, `session_id`, `conversation_id`, `version`, and any `x-codex-*` or `x-openai-*`. Uses one shared `httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10))` created in the app lifespan, with `client.send(req, stream=True)`; returns a `StreamingResponse` with the upstream status code, `content-type`, and the body streamed chunk by chunk via `aiter_raw()`, closing the upstream response when done. Connection errors give 502.
- **Why:** "Model credentials: the proxy" in the Technical Plan.

#### `den/app.py` (new)

- **What changes:** `create_app(settings: Settings | None = None) -> FastAPI`.
  - Lifespan startup: set the loop's default executor to `ThreadPoolExecutor(max_workers=64)` (the docs warn each streaming turn holds a thread); create `data_dir`; `Database(data_dir / "den.db").migrate()`; `load_or_create_secret`; build the `EventBus`, the runtime, the `TurnRegistry` and the `AppContext`; create the `Scheduler`, call `recover_on_startup()` and `start()`; for any agent with `setup_state='creating'` (setup cut off by a restart) set it to `error` with detail "setup: the platform restarted during setup; press Retry"; spawn `runtime.prepare()` in the background (errors logged, retried on first use); spawn `idle_reaper`; create the proxy's `httpx.AsyncClient`.
  - Shutdown: stop the scheduler, `turns.stop_all()` (marking those turns interrupted happens through their runners), cancel background tasks, close the HTTP client and the database.
  - Middleware: a CSRF check rejecting non-`GET`/`HEAD`/`OPTIONS` requests under `/api/` without `X-Den-CSRF: 1` (403 `{"detail": "Missing CSRF header."}`).
  - Routers: auth, agents, chat, files, jobs, mcp, model proxy, in that order; then static files: `GET /` serves `den/static/index.html` with `Cache-Control: no-store`, and `/static` is mounted on `den/static`.
- **Why:** wiring for every component in the Technical Plan diagram; restart recovery for "Agents come back the same after a restart".

### 4.3 Web client files

#### `den/static/index.html` (new)

- **What changes:** one page with a header (product name "Den", a development-mode banner shown when `/api/config` says `runtime == "local"`, the signed-in email and a Log out button) and an empty `<main id="app">`. It loads `/static/style.css` and `/static/app.js` (plain script, no modules, no CDN, no build).
- **Why:** "Web client: sign up, chat, files and jobs in the browser".

#### `den/static/app.js` (new)

- **What changes:** a small hash-routed app (`#/login`, `#/register`, `#/agents`, `#/new`, `#/agent/<id>/chat`, `#/agent/<id>/files?path=...`, `#/agent/<id>/jobs`).
  - `api(method, path, body)` sends JSON with `credentials: "same-origin"` and the `X-Den-CSRF: 1` header, turns non-2xx responses into errors carrying `detail`, and sends the user to `#/login` on 401.
  - DOM is built with `document.createElement` and `textContent` only (never `innerHTML` with server data), so text from agents and files cannot inject script.
  - **Login/register:** email, password; register sends `Intl.DateTimeFormat().resolvedOptions().timeZone`.
  - **Agents list:** cards with name, model label, setup state and container state; "New agent".
  - **New agent (onboarding):** name field and a model radio list from `/api/config`; on create, the page switches to a progress list fed by the agent's SSE stream (`agent_status` events with `step` `files`, `image`, `container`, `probe`, `ready`), shows the error with Retry and a model picker on `error`, and an "Open chat" button on `ready`.
  - **Agent page:** header with name, model, container state and Start/Stop/Restart buttons, a settings dialog (rename, change model, delete with confirmation), and tabs Chat, Files and Jobs. One `EventSource` per open agent (`/api/agents/<id>/events?after=<last_seq>`), which the browser reconnects with `Last-Event-ID` by itself.
  - **Chat tab:** loads `/history`, renders the timeline, then applies live events. Renders `turn_started` (chat) as the user's bubble; `agent_message` items as the agent's bubble (plain text with `white-space: pre-wrap`; fenced code blocks shown in `<pre>`); `message_delta` appended to the streaming bubble; `snapshot` fills in partial text; `command` items as collapsible rows showing the command, a spinner while running, the exit code in red when non-zero, and live `command_output`; `file_change` items as a list of changed paths linking to the Files tab; `mcp_tool_call` items as "Used platform tool <tool>" with the result; `reasoning` as a "Thinking…" line; `notice` as a coloured banner; `turn_completed` with `failed` as a red card with the message (and a "Start a new conversation" button for `context_full`, a "Switch model" button for `model_unavailable` and `rate_limited`); `job_run_started`/`job_run_finished` as small cards linking to the run. A composer with Send (Enter sends, Shift+Enter is a newline) is disabled while a chat turn runs, with a Stop button shown instead. "New conversation" button.
  - **Files tab:** breadcrumb; table of name, size and modified time; click a folder to open it; click a file to open a preview pane (text in `<pre>`, images via `download?inline=1`, otherwise "Download"); Upload (multiple, `FormData` field `files`), New folder, Delete (confirm), Download. It refreshes when an `item_completed` of type `file_change` or `command` arrives.
  - **Jobs tab:** a form (name, prompt, schedule kind: once with `datetime-local`, every N minutes, or cron) and a list of jobs showing name, "by you"/"by the agent" badge, schedule description, status, next run (local time), last result (status badge plus summary), and Cancel and Run now buttons. Expanding a job loads its runs; clicking a run shows its transcript with the same item renderer as chat. It refreshes on `jobs_changed`, `job_run_started` and `job_run_finished`.
- **Why:** every user capability on the "What a user can do" slide.

#### `den/static/style.css` (new)

- **What changes:** a clean, responsive layout (system font, light theme, max-width content, tabs, cards, chat bubbles, status badges, red/yellow notices) with no external assets.
- **Why:** the web client needs to be usable by people who are not the developer.

### 4.4 Container, packaging and docs

#### `container/Dockerfile` (new)

- **What changes:** `FROM node:22-slim`; `apt-get install -y --no-install-recommends ca-certificates git python3 python3-pip python3-venv curl procps less` and clean the apt lists; `npm install -g @openai/codex@0.163.0`; `mkdir -p /workspace /codex /home/agent && chmod 1777 /home/agent /codex /workspace`; `ENV CODEX_HOME=/codex HOME=/home/agent`; `WORKDIR /workspace`; `CMD ["sleep", "infinity"]`.
- **Why:** the image in `docs/codex-sdk.md`, "Inside Docker", plus the tools an assistant commonly needs; CA certificates are required or turns hang.

#### `requirements.txt` (new)

- **What changes:** pinned: `fastapi==0.162.0`, `uvicorn==0.54.0`, `openai-codex==0.163.0`, `httpx==0.28.1`, `croniter==6.2.4`, `python-multipart==0.0.33`, `pytest==9.2.0`, `pytest-asyncio==1.4.1.post1`.
- **Why:** reproducible installs; versions are the ones the tests ran with.

#### `pytest.ini` (new)

- **What changes:** `asyncio_mode = auto`, `testpaths = tests`, a `docker` marker ("needs a working Docker daemon"), and `-m "not docker"` in `addopts` so the Docker end-to-end test runs only when asked for (`pytest -m docker`).
- **Why:** the default test run must pass on a machine without Docker.

#### `.env.example` (new)

- **What changes:** every `DEN_*` variable with its default and a comment, plus `UMGPT_TOOLKIT_API_KEY=` and `OPENAI_API_KEY=` placeholders. `den/__main__.py` loads `.env` from the working directory if it exists (simple `KEY=VALUE` lines, not overriding variables already set), so no extra dependency is needed.
- **Why:** documents configuration without committing secrets (`.env` is git-ignored).

#### `.gitignore` (new)

- **What changes:** ignores `.venv/`, Python caches, `data/`, database files, `.env`, `secret.key` and `auth.json`.
- **Why:** AGENTS.md: keep keys, logins and runtime data out of commits.

#### `README.md` (new)

- **What changes:** what Den is; requirements (Python 3.12, Docker with WSL integration enabled on Windows); install (`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`); credentials (Toolkit key, OpenAI key, or the `chatgpt` testing mode, with the warning that the model can read a mounted login); run (`.venv/bin/python -m den`, open `http://localhost:8000`); development mode without Docker (`DEN_RUNTIME=local`); trying it for free with the fake model (`python -m tests.fake_model --port 9100` and `DEN_UPSTREAM_BASE_URL=http://127.0.0.1:9100/v1 DEN_UPSTREAM_API_KEY=fake`); running tests (`pytest`, `pytest -m docker`); a map of the code; links to the design doc.
- **Why:** the deliverable is "the whole platform" pushed to GitHub, so someone else must be able to run it.

### 4.5 Tests

All tests use temporary data directories and never touch `~/.codex` or real credentials.

#### `tests/__init__.py` (new)

- **What changes:** empty, so `python -m tests.fake_model` works.
- **Why:** packaging for the fake model.

#### `tests/fake_model.py` (new)

- **What changes:** a scripted fake of the Responses API, usable from tests and from the command line (`python -m tests.fake_model --port N`). `build_fake_app() -> FastAPI` handles `GET /v1/models` (lists `gpt-6.1-sol` and `gpt-6-luna`) and `POST /v1/responses` (streams SSE: `response.created`, `response.output_item.added`, `response.output_text.delta` per word, `response.output_item.done`, `response.completed`). Behaviour is chosen from the text of the last user message:
  - contains `SCHEDULE:<n>` → the first response is a `custom_tool_call` to `exec` with JavaScript calling `tools.mcp__platform__schedule_job({name: "follow-up", prompt: "Check on the thing", in_minutes: n})` and `text(JSON.stringify(r))`; after the `custom_tool_call_output` arrives it answers "Scheduled it."
  - contains `RUN:<shell command>` → a `custom_tool_call` to `exec` calling `tools.exec_command({cmd: <command>})`, then "Ran it."
  - contains `SLOW` → streams one delta every 0.5 s for 60 s (for Stop tests).
  - contains `FAIL401` → HTTP 401 with an OpenAI-style error body.
  - the probe prompt → "ready".
  - otherwise → "Echo: <first 200 chars of the user text>".
  It records every request (path, headers, JSON body) in `app.state.requests` so tests can assert what Codex sent through the proxy. `run_in_thread(port) -> server` starts it with uvicorn in a daemon thread and returns a handle with `.url` and `.stop()`.
- **Why:** lets the whole chain run with the real app-server and no credentials (Technical Plan, "Development runtime and testing").

#### `tests/conftest.py` (new)

- **What changes:** fixtures: `fake_model` (session scope, `run_in_thread` on a free port); `settings_factory(tmp_path, **overrides)` producing `Settings` with `runtime="local"`, `data_dir=tmp_path`, `upstream_base_url=fake_model.url + "/v1"`, `upstream_api_key="fake-key"`, `onboarding_probe=True`, `min_job_interval_minutes=1`, `idle_stop_minutes=15`; `live_server(settings)` that runs `create_app(settings)` under uvicorn in a background thread on a free port (agent URLs point at it) and yields its base URL, then stops it; `client(live_server)` an `httpx.Client` with `X-Den-CSRF: 1` set; helpers `register(client, email)`, `create_ready_agent(client, name="A", model="gpt-6-luna")` (creates the agent and polls `GET /api/agents/{id}` until `setup_state` is `ready` or `error`, failing on `error`), and `wait_for(predicate, timeout)`.
- **Why:** shared plumbing so each test file stays short.

#### `tests/test_security.py` (new)

- **What changes:** password hash round trip and wrong password; agent token round trip, tampered HMAC, wrong secret, malformed tokens; `LoginLimiter` blocks after 10 failures.
- **Why:** "Accounts and isolation", "Agent tokens".

#### `tests/test_auth.py` (new)

- **What changes:** register sets a cookie and `me` works; duplicate email is 409 (case-insensitive); short password is 400; bad login is 401; logout ends the session; a missing CSRF header gets 403 on POST; an unauthenticated request to `/api/agents` gets 401.
- **Why:** the "Accounts" row of the spec.

#### `tests/test_isolation.py` (new)

- **What changes:** two users; user A creates an agent, a job and a file. User B, using A's ids, gets 404 on **every** route under `/api/agents/{agent_id}` in 4.1 (enumerated in a table in the test, including `events`, `files/download`, `runs/{run_id}`), and A's agent is absent from B's list. B's own agent with A's job id or run id in the path also returns 404. An agent token for agent A cannot list or cancel agent B's jobs over MCP, and a token with a bumped `token_version` is rejected.
- **Why:** "No user can reach another user's agents, even by editing a URL. Test it with two accounts."

#### `tests/test_files.py` (new)

- **What changes:** upload, list, preview (text and binary), download (attachment headers, `nosniff`, CSP), `inline=1` for a PNG and forced attachment for an HTML file, mkdir, delete file, delete directory. Rejected paths: `..`, `a/../b`, absolute paths, NUL. A symlink inside the workspace pointing at `/etc` or at a file outside is listed as `symlink`, and download, preview, list-through, upload-through and delete-through all fail without touching the target. An upload over the size limit gets 413 and leaves no temporary file.
- **Why:** "Files" in the Technical Plan and the workspace row of the spec.

#### `tests/test_schedule.py` (new)

- **What changes:** pure tests of `normalize_schedule`, `compute_next_run` and `describe_schedule`: once in the past rejected, a naive `once` read in its timezone, interval under the minimum rejected, cron every minute rejected when the minimum is 5, cron next run across a DST change in `America/Detroit`, an invalid timezone rejected.
- **Why:** schedule math is where off-by-one and timezone bugs hide.

#### `tests/test_scheduler.py` (new)

- **What changes:** with a `Scheduler` over a temporary database and a stub runner (monkeypatching `TurnRunner.run` to return a chosen outcome after a chosen delay): a due job fires once and its `next_run_at` advances before the run starts; a job 2 hours overdue runs once with `late=1`; a job 3 days overdue records `missed` and does not run; an occurrence while the previous run is `running` records `skipped`; `recover_on_startup` marks `running` runs and turns interrupted; `cancel_job` stops a running run and clears `next_run_at`; a `once` job ends `finished`; the concurrency semaphore holds the 5th run in `queued` when the limit is 4; the timeout stops a run.
- **Why:** "Jobs and the scheduler" rules.

#### `tests/test_mcp.py` (new)

- **What changes:** over HTTP against the live server: `initialize` echoes the protocol version; notifications get 202; `GET /mcp` gets 405; `tools/list` lists three tools; `schedule_job` with each of the four schedule fields creates a job with `created_by="agent"` visible in the user's `GET jobs`; more than one schedule field is an `isError` result; `list_jobs` shows only this agent's jobs; `cancel_job` on an agent-created job works and on a user-created job returns the "Only the user can cancel" error; a missing or bad token gets 401.
- **Why:** "Agents scheduling their own jobs (MCP)".

#### `tests/test_model_proxy.py` (new)

- **What changes:** a request with a valid agent token is forwarded to the fake model with `Authorization: Bearer fake-key` (asserted from `fake_model.requests`) and the SSE body arrives intact; a bad token gets 401; a model not in the list gets 403; a path such as `files` gets 404; with no upstream key, 502.
- **Why:** "Model credentials: the proxy".

#### `tests/test_chat_e2e.py` (new)

- **What changes:** with the local runtime, the real bundled app-server and the fake model: onboarding reaches `ready` and emits the `files`, `container`, `probe` and `ready` steps; a message streams `message_delta` events over SSE and ends with an `agent_message` item and `turn_completed` `completed`, and the reply is in `/history`; a second message continues the same `chat_thread_id`; `RUN:echo hi > note.txt` produces a `command` item with exit code 0 and the file appears in the Files API; `SCHEDULE:30` produces an `mcp_tool_call` item and an agent-created job; a `SLOW` message can be stopped (turn `interrupted` within 10 s, the composer is free again); a second message while one is running gets 409; closing the SSE connection mid-turn and reconnecting with `Last-Event-ID` replays the missing events and a `snapshot`; `FAIL401` ends the turn `failed` with `error_kind == "unauthorized"`; the onboarding probe with a model the proxy refuses ends in `setup_state == "error"`.
- **Why:** the Chat and Onboarding rows of the spec and "Agents keep working after the browser closes", exercised through the real harness.

#### `tests/test_jobs_e2e.py` (new)

- **What changes:** a user job "Run now" on a ready agent produces a run that ends `succeeded` with the fake model's reply as its summary, a `job_run_finished` event in `/history`, and a transcript at `/runs/{run_id}`; a `once` job due in a few seconds fires on its own and the job ends `finished`; cancelling a job with a `SLOW` run in progress ends that run `interrupted`; a job run and a chat turn on the same agent run at the same time.
- **Why:** the Jobs row of the spec and "scheduled jobs run with no browser open".

#### `tests/test_restart.py` (new)

- **What changes:** start a live server on a data directory, create a user, an agent, a chat exchange, a job and a file; stop the server; start a new one on the same directory: login still works (sessions persist), the agent, its model, its history, its job with the same `next_run_at`, and its file are all there, and a new message continues the same `chat_thread_id`. Also: a turn left `running` in the database before the restart is reported `interrupted` with `error_kind == "platform_restart"`.
- **Why:** "Agents come back the same after a restart."

#### `tests/test_docker_runtime.py` (new)

- **What changes:** `DockerRuntime.run_args` contains every isolation flag listed in 4.2 (`--cap-drop ALL`, `no-new-privileges`, memory, cpus, pids, `--user`, both mounts, the network, the host gateway, `PLATFORM_TOKEN`, no upstream key anywhere in the arguments) and the read-only `auth.json` mount only in `chatgpt` mode; with a fake `docker` script placed first on `PATH` (it logs its arguments and answers `inspect` from a state file), `ensure_running` runs `docker run` for a missing container, `docker start` for an exited one and nothing for a running one; `status` maps states correctly; `restart` bumps `token_version`; a non-zero exit raises `AgentRuntimeError` with stderr's first line; `codex_config` launch arguments equal the documented `docker exec -i <name> codex app-server --listen stdio://`.
- **Why:** the Docker code cannot be run in every environment, so its contract is tested without a daemon.

#### `tests/test_docker_e2e.py` (new)

- **What changes:** marked `docker` and skipped when `docker info` fails. Builds the image if needed, starts a live server with `runtime="docker"`, `host="0.0.0.0"` and the fake model, creates an agent, checks the container exists with the expected limits (`docker inspect`), sends a message and gets the fake model's reply through the container, the proxy and the MCP endpoint (`SCHEDULE:30`), writes a file with `RUN:` and reads it through the Files API, confirms `env` inside the container has no upstream key, stops and restarts the container and continues the conversation, then deletes the agent and confirms the container and directory are gone.
- **Why:** the one test that proves the real containers work, to be run once Docker is enabled.
