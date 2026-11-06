"""Shared test plumbing: the fake model, live Den servers, clients and helpers.

Tests never read ~/.codex or real keys: settings are built from an explicit
dict (so the process environment is not consulted for credentials), the real
key variables are removed from the environment for the session, and every
server gets a temporary data directory.

Test modules import the plain helpers with `from tests.conftest import ...`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import pytest
import uvicorn

from den.config import Settings, load_settings
from tests.fake_model import FakeModelServer, free_port, run_in_thread

# Variables that could carry a real credential into a test run.
REAL_KEY_VARS = (
    "UMGPT_TOOLKIT_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "DEN_UPSTREAM_API_KEY",
    "DEN_UPSTREAM_BASE_URL",
)
FAKE_UPSTREAM_KEY = "fake-key"
DEFAULT_PASSWORD = "correct horse battery"


# --------------------------------------------------------------------------
# Settings and servers
# --------------------------------------------------------------------------


def make_settings(
    data_dir: Path, upstream: FakeModelServer | str, **overrides: Any
) -> Settings:
    """Settings for a test server: local runtime, fake model, temp data dir.

    `port` and `agent_backend_url` are filled in by LiveServer once it has a
    port, so overrides normally leave them alone.
    """
    upstream_url = upstream if isinstance(upstream, str) else upstream.url
    env = {
        "DEN_RUNTIME": "local",
        "DEN_DATA_DIR": str(data_dir),
        "DEN_UPSTREAM_BASE_URL": upstream_url.rstrip("/") + "/v1",
        "DEN_UPSTREAM_API_KEY": FAKE_UPSTREAM_KEY,
        "DEN_MIN_JOB_INTERVAL_MINUTES": "1",
        # Never let the chatgpt mode point at the developer's real login.
        "DEN_CHATGPT_AUTH_JSON": str(Path(data_dir) / "no-such-auth.json"),
    }
    settings = load_settings(env)
    settings = dataclasses.replace(
        settings, onboarding_probe=True, min_job_interval_minutes=1, idle_stop_minutes=15
    )
    return dataclasses.replace(settings, **overrides)


class LiveServer:
    """Den's real app under uvicorn in a background thread on a free port."""

    def __init__(self, settings: Settings) -> None:
        if settings.runtime == "docker":
            # Containers reach the backend through the Docker host gateway, so
            # it must listen on every interface.
            bind_host = "0.0.0.0"
            agent_host = os.environ.get("DEN_TEST_AGENT_HOST", "host.docker.internal")
        else:
            bind_host = agent_host = "127.0.0.1"
        port = free_port(bind_host)
        self.settings = dataclasses.replace(
            settings,
            host=bind_host,
            port=port,
            agent_backend_url=f"http://{agent_host}:{port}",
        )
        self.url = f"http://127.0.0.1:{port}"

        from den.app import create_app  # imported late so conftest loads on its own

        self.app = create_app(self.settings)
        config = uvicorn.Config(
            self.app,
            host=bind_host,
            port=port,
            log_level="warning",
            lifespan="on",
            # Open SSE streams would otherwise hold shutdown forever.
            timeout_graceful_shutdown=5,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name=f"den-test-{port}", daemon=True
        )

    @property
    def ctx(self) -> Any:
        """The server's AppContext (its db is safe to use from this thread)."""
        return self.app.state.ctx

    def start(self, timeout: float = 60.0) -> "LiveServer":
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError("Den test server exited during startup (see log above)")
            if time.monotonic() > deadline:
                raise TimeoutError("Den test server did not start in time")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=60)
        if self._thread.is_alive():
            raise RuntimeError("Den test server did not shut down within 60 s")


@contextlib.contextmanager
def running_server(settings: Settings) -> Iterator[LiveServer]:
    server = LiveServer(settings).start()
    try:
        yield server
    finally:
        server.stop()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _no_real_credentials() -> Iterator[None]:
    saved = {name: os.environ.pop(name) for name in REAL_KEY_VARS if name in os.environ}
    yield
    os.environ.update(saved)


@pytest.fixture(scope="session")
def fake_model() -> Iterator[FakeModelServer]:
    server = run_in_thread(0)
    yield server
    server.stop()


@pytest.fixture
def settings_factory(tmp_path: Path, fake_model: FakeModelServer) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        overrides.setdefault("data_dir", tmp_path / "data")
        return make_settings(overrides.pop("data_dir"), fake_model, **overrides)

    return factory


@pytest.fixture
def server_factory() -> Iterator[Callable[[Settings], LiveServer]]:
    started: list[LiveServer] = []

    def start(settings: Settings) -> LiveServer:
        server = LiveServer(settings).start()
        started.append(server)
        return server

    yield start
    for server in reversed(started):
        if server._thread.is_alive():
            server.stop()


@pytest.fixture
def live_server(server_factory, settings_factory) -> LiveServer:
    return server_factory(settings_factory())


@pytest.fixture
def client_factory() -> Iterator[Callable[..., httpx.Client]]:
    clients: list[httpx.Client] = []

    def make(server: LiveServer | str, **kwargs: Any) -> httpx.Client:
        c = new_client(server, **kwargs)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.close()


@pytest.fixture
def client(live_server: LiveServer, client_factory) -> httpx.Client:
    return client_factory(live_server)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


def new_client(server: LiveServer | str, *, csrf: bool = True, **kwargs: Any) -> httpx.Client:
    """An httpx client for the server; sends the CSRF header unless told not to."""
    url = server if isinstance(server, str) else server.url
    headers = {"X-Den-CSRF": "1"} if csrf else {}
    kwargs.setdefault("timeout", httpx.Timeout(30.0, read=60.0))
    return httpx.Client(base_url=url, headers=headers, **kwargs)


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def register(
    client: httpx.Client,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
    timezone: str | None = "UTC",
) -> dict[str, Any]:
    body: dict[str, Any] = {"email": email or unique_email(), "password": password}
    if timezone is not None:
        body["timezone"] = timezone
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()["user"]


def wait_for(
    predicate: Callable[[], Any],
    timeout: float = 30.0,
    interval: float = 0.1,
    message: str = "condition",
) -> Any:
    """Poll until predicate() is truthy and return its value, or fail the test."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {message}")
        time.sleep(interval)


def get_agent(client: httpx.Client, agent_id: str) -> dict[str, Any]:
    r = client.get(f"/api/agents/{agent_id}")
    assert r.status_code == 200, r.text
    return r.json()["agent"]


def create_agent(client: httpx.Client, name: str = "A", model: str = "gpt-6-luna") -> dict[str, Any]:
    r = client.post("/api/agents", json={"name": name, "model": model})
    assert r.status_code == 201, r.text
    return r.json()["agent"]


def wait_setup_settled(client: httpx.Client, agent_id: str, timeout: float = 120.0) -> dict[str, Any]:
    """Poll the agent until setup is `ready` or `error` and return it."""

    def settled() -> dict[str, Any] | None:
        agent = get_agent(client, agent_id)
        return agent if agent["setup_state"] in ("ready", "error") else None

    return wait_for(settled, timeout, interval=0.2, message=f"agent {agent_id} setup")


def create_ready_agent(
    client: httpx.Client, name: str = "A", model: str = "gpt-6-luna", timeout: float = 120.0
) -> dict[str, Any]:
    agent = create_agent(client, name, model)
    agent = wait_setup_settled(client, agent["id"], timeout)
    assert agent["setup_state"] == "ready", f"setup failed: {agent['setup_detail']}"
    return agent


def history(client: httpx.Client, agent_id: str, limit: int = 1000) -> dict[str, Any]:
    r = client.get(f"/api/agents/{agent_id}/history", params={"limit": limit})
    assert r.status_code == 200, r.text
    return r.json()


def events_of(events: list[dict], kind: str | None = None, turn_id: str | None = None) -> list[dict]:
    return [
        e
        for e in events
        if (kind is None or e["kind"] == kind) and (turn_id is None or e["turn_id"] == turn_id)
    ]


def send_message(client: httpx.Client, agent_id: str, text: str) -> str:
    r = client.post(f"/api/agents/{agent_id}/messages", json={"text": text})
    assert r.status_code == 202, r.text
    return r.json()["turn_id"]


def wait_turn_completed(
    client: httpx.Client, agent_id: str, turn_id: str, timeout: float = 60.0
) -> dict[str, Any]:
    """Wait for a chat turn's persisted `turn_completed` event and return it."""

    def done() -> dict[str, Any] | None:
        found = events_of(history(client, agent_id)["events"], "turn_completed", turn_id)
        return found[-1] if found else None

    return wait_for(done, timeout, interval=0.2, message=f"turn {turn_id} to complete")


def chat(client: httpx.Client, agent_id: str, text: str, timeout: float = 60.0) -> dict[str, Any]:
    """Send a message and return its `turn_completed` event."""
    turn_id = send_message(client, agent_id, text)
    return wait_turn_completed(client, agent_id, turn_id, timeout)


def item_events(events: list[dict], item_type: str, kind: str = "item_completed") -> list[dict]:
    return [e for e in events if e["kind"] == kind and e["data"].get("item_type") == item_type]


def jobs_of(client: httpx.Client, agent_id: str) -> list[dict[str, Any]]:
    r = client.get(f"/api/agents/{agent_id}/jobs")
    assert r.status_code == 200, r.text
    return r.json()["jobs"]


def runs_of(client: httpx.Client, agent_id: str, job_id: str) -> list[dict[str, Any]]:
    r = client.get(f"/api/agents/{agent_id}/jobs/{job_id}/runs")
    assert r.status_code == 200, r.text
    return r.json()["runs"]


def get_run(client: httpx.Client, agent_id: str, run_id: str) -> dict[str, Any]:
    r = client.get(f"/api/agents/{agent_id}/runs/{run_id}")
    assert r.status_code == 200, r.text
    return r.json()


def wait_run_status(
    client: httpx.Client, agent_id: str, run_id: str, statuses: set[str], timeout: float = 60.0
) -> dict[str, Any]:
    def reached() -> dict[str, Any] | None:
        run = get_run(client, agent_id, run_id)["run"]
        return run if run["status"] in statuses else None

    return wait_for(reached, timeout, interval=0.2, message=f"run {run_id} to reach {statuses}")


# --------------------------------------------------------------------------
# Database and token helpers (read the server's own state)
# --------------------------------------------------------------------------


def agent_row(server: LiveServer, agent_id: str) -> dict[str, Any]:
    row = server.ctx.db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
    assert row is not None, f"agent {agent_id} not in the database"
    return row


def agent_token(server: LiveServer, agent_id: str) -> str:
    """The agent's current PLATFORM_TOKEN, as the backend would issue it."""
    return server.ctx.agent_token(agent_row(server, agent_id))


# --------------------------------------------------------------------------
# MCP helpers
# --------------------------------------------------------------------------


def mcp_request(
    server: LiveServer | str,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int | None = 1,
) -> httpx.Response:
    url = (server if isinstance(server, str) else server.url) + "/mcp"
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        # strip(): an empty token is sent as a bare "Bearer" (no trailing space).
        headers["Authorization"] = f"Bearer {token}".strip()
    return httpx.post(url, json=message, headers=headers, timeout=30)


def mcp_tool(server: LiveServer | str, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a platform tool and return the JSON-RPC `result` (a CallToolResult)."""
    r = mcp_request(server, token, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "result" in body, body
    return body["result"]


def tool_text(result: dict[str, Any]) -> str:
    return "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


# --------------------------------------------------------------------------
# Server-Sent Events
# --------------------------------------------------------------------------


class EventStream:
    """A live `GET /api/agents/{id}/events` stream, parsed into event dicts.

    Each event gets an extra `_sse_id` key holding the `id:` line (or None).
    """

    def __init__(
        self,
        client: httpx.Client,
        agent_id: str,
        *,
        after: int | None = None,
        last_event_id: int | None = None,
        read_timeout: float = 25.0,
    ) -> None:
        params = {"after": after} if after is not None else {}
        headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
        # The server pings every 15 s, so a 25 s read timeout only fires if it hangs.
        self._cm = client.stream(
            "GET",
            f"/api/agents/{agent_id}/events",
            params=params,
            headers=headers,
            timeout=httpx.Timeout(10.0, read=read_timeout),
        )
        self.response = self._cm.__enter__()
        assert self.response.status_code == 200, self.response.read()
        self._lines = self.response.iter_lines()
        self.received: list[dict[str, Any]] = []

    def next_event(self) -> dict[str, Any]:
        """Block until the next complete event; pings are skipped."""
        data_lines: list[str] = []
        event_id: str | None = None
        for line in self._lines:
            if line == "":
                if data_lines:
                    event = json.loads("\n".join(data_lines))
                    event["_sse_id"] = int(event_id) if event_id else None
                    self.received.append(event)
                    return event
                event_id = None
                continue
            if line.startswith(":"):
                continue  # comment, e.g. ": ping"
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data":
                data_lines.append(value)
            elif field == "id":
                event_id = value
        raise EOFError("the server closed the event stream")

    def read_until(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = 60.0
    ) -> list[dict[str, Any]]:
        """Read events until one matches; return everything read in this call."""
        deadline = time.monotonic() + timeout
        batch: list[dict[str, Any]] = []
        while True:
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"no matching event within {timeout:.0f}s; got kinds "
                    f"{[e['kind'] for e in batch][-20:]}"
                )
            event = self.next_event()
            batch.append(event)
            if predicate(event):
                return batch

    def close(self) -> None:
        self._cm.__exit__(None, None, None)

    def __enter__(self) -> "EventStream":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_synced_stream(client: httpx.Client, agent_id: str) -> EventStream:
    """Open the event stream and return once it is surely subscribed.

    The server subscribes to the live bus before replaying, so seeing a
    replayed event proves live events (like message_delta) will not be missed.
    """
    last_seq = history(client, agent_id).get("last_seq") or 0
    if last_seq <= 0:
        return EventStream(client, agent_id, after=0)
    stream = EventStream(client, agent_id, after=last_seq - 1)
    stream.read_until(lambda e: (e.get("seq") or 0) >= last_seq, timeout=30)
    return stream
