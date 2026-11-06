"""Regression tests for turn start-up, closing agents, deleted agents and leftovers.

- A Stop or a cancellation that lands while the Codex client is starting
  must still close the app-server process it launches.
- While an agent is being deleted, no chat message may start or queue.
- A runtime asked to launch a deleted agent refuses instead of recreating
  its directory and container.
- The idle reaper also stops containers of agents whose setup failed.
- Scratch CODEX_HOMEs left by a crash are removed at the next start.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import time
import types

import pytest
from openai_codex import CodexConfig
from openai_codex import client as sdk_client

import den.codex_bridge as cb
from den import lifecycle, providers
from den.agent_files import agent_paths, prepare_scratch_home, remove_agent_dirs
from den.db import iso, new_id, utcnow
from den.runtime.base import AgentRuntimeError
from den.runtime.local import LocalRuntime
from tests.conftest import create_ready_agent, register, send_message, wait_for
from tests.test_docker_runtime import SECRET, fake_docker, make_runtime  # noqa: F401  (fixture)


# --------------------------------------------------------------------------
# Stop / cancel while the Codex client starts
# --------------------------------------------------------------------------


class _FakeDB:
    def execute(self, *a, **k):
        return types.SimpleNamespace(rowcount=1)

    @contextlib.contextmanager
    def transaction(self):
        yield self


class _FakeRuntime:
    def __init__(self, home: str) -> None:
        self.home = home

    async def status(self, agent_id):
        return "running"

    async def ensure_running(self, agent):
        return None

    def codex_config(self, agent, sub=None):
        import os

        from codex_cli_bin import bundled_codex_path

        return CodexConfig(launch_args_override=(
            "env", "-i", f"PATH={os.environ.get('PATH')}", f"HOME={self.home}",
            f"CODEX_HOME={self.home}", str(bundled_codex_path()), "app-server", "--listen", "stdio://",
        ))

    def workspace_cwd(self, agent):
        return self.home


class _FakeCtx:
    def __init__(self, home: str) -> None:
        self.db = _FakeDB()
        self.runtime = _FakeRuntime(home)
        self.settings = None

    def get_agent(self, agent_id):
        return None

    def emit(self, **kw):
        pass


@pytest.fixture
def gated_start(monkeypatch):
    """Hold CodexClient.start() until released; record the processes it spawns."""
    monkeypatch.setattr(providers, "resolve_credentials", lambda *a: object())
    monkeypatch.setattr(providers, "model_offered", lambda *a: True)
    entered, release = threading.Event(), threading.Event()
    procs: list = []
    real_start = sdk_client.CodexClient.start

    def start(self):
        entered.set()
        release.wait(10)
        real_start(self)
        procs.append(self._proc)

    monkeypatch.setattr(sdk_client.CodexClient, "start", start)
    yield entered, release, procs
    release.set()
    for proc in procs:
        if proc is not None and proc.poll() is None:
            proc.kill()


def _runner(tmp_path) -> cb.TurnRunner:
    agent = {"id": "a" * 32, "user_id": "u", "model": "m", "provider": "platform"}
    return cb.TurnRunner(
        _FakeCtx(str(tmp_path)), agent=agent, turn_id="b" * 32, prompt="hi", source="chat",
        channel="chat", thread_id=None, developer_instructions="x",
    )


async def _wait_event(event: threading.Event) -> None:
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("CodexClient.start was never called")


def _all_exited(procs: list) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if all(p is None or p.poll() is not None for p in procs):
            return True
        time.sleep(0.05)
    return False


async def test_stop_while_the_client_starts_closes_the_app_server(tmp_path, gated_start) -> None:
    entered, release, procs = gated_start
    runner = _runner(tmp_path)
    task = asyncio.ensure_future(runner.run())
    await _wait_event(entered)
    stopping = asyncio.ensure_future(runner.stop("user"))
    await asyncio.sleep(0.3)  # stop() has done all it does before the process exists
    release.set()
    await asyncio.wait_for(stopping, 30)
    outcome = await asyncio.wait_for(task, 30)

    assert outcome.status == "interrupted"
    assert len(procs) == 1
    assert _all_exited(procs), "the app-server launched during Stop is still running"


async def test_cancel_while_the_client_starts_closes_the_app_server(tmp_path, gated_start) -> None:
    entered, release, procs = gated_start
    runner = _runner(tmp_path)
    task = asyncio.ensure_future(runner.run())
    await _wait_event(entered)
    task.cancel()
    await asyncio.sleep(0.3)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)

    assert runner.outcome is not None and runner.outcome.status == "interrupted"
    assert len(procs) == 1
    assert _all_exited(procs), "the app-server launched before cancellation is still running"


# --------------------------------------------------------------------------
# Closing mark: no chat while delete / container stop / restart runs
# --------------------------------------------------------------------------


async def test_closing_mark_refuses_messages_and_holds_the_queue() -> None:
    registry = cb.TurnRegistry()
    ctx = types.SimpleNamespace(turns=registry)
    agent = {"id": "c" * 32}
    with registry.closing(agent["id"]):
        with registry.closing(agent["id"]):  # a container stop overlapping a delete
            pass
        assert registry.is_closing(agent["id"])
        with pytest.raises(cb.AgentClosing):
            await cb.submit_chat_message(ctx, agent, "hello")
        with pytest.raises(cb.AgentClosing):
            await cb.submit_chat_message(ctx, agent, "hello", mode="interrupt")
        assert cb.dispatch_next(ctx, agent["id"]) is None
    assert not registry.is_closing(agent["id"])
    # Routers that map QueueFull to 409 refuse it the same way.
    assert issubclass(cb.AgentClosing, cb.QueueFull)


def test_message_sent_during_delete_is_refused(server_factory, settings_factory, client_factory) -> None:
    server = server_factory(settings_factory(onboarding_probe=False))
    c = client_factory(server)
    register(c)
    agent_id = create_ready_agent(c, "Del")["id"]
    ctx = server.ctx
    send_message(c, agent_id, "SLOW one")
    wait_for(lambda: ctx.turns.chat.get(agent_id) is not None, message="the slow turn")

    launched: list[str] = []
    real_launch = cb._launch_chat_turn

    def spy(ctx_, agent_, turn_id, text):
        launched.append(text)
        return real_launch(ctx_, agent_, turn_id, text)

    cb._launch_chat_turn = spy
    try:
        result: dict = {}

        def do_delete() -> None:
            t0 = time.monotonic()
            r = c.delete(f"/api/agents/{agent_id}", timeout=60)
            result["delete"] = (r.status_code, time.monotonic() - t0)

        deleting = threading.Thread(target=do_delete)
        deleting.start()
        wait_for(lambda: ctx.turns.is_closing(agent_id), timeout=10, interval=0.001,
                 message="delete to mark the agent")
        other_tab = client_factory(server)
        other_tab.cookies = c.cookies
        r = other_tab.post(f"/api/agents/{agent_id}/messages", json={"text": "SLOW two"})
        deleting.join(60)
    finally:
        cb._launch_chat_turn = real_launch

    assert r.status_code == 409, r.text
    assert launched == []
    status, seconds = result["delete"]
    assert status == 204
    assert seconds < lifecycle.DELETE_DRAIN_SECONDS, "delete waited for a turn it should not have had"
    assert not ctx.turns.active_for_agent(agent_id)
    assert not ctx.turns.is_closing(agent_id)


# --------------------------------------------------------------------------
# Runtimes refuse a deleted agent
# --------------------------------------------------------------------------


async def test_docker_ensure_running_refuses_a_deleted_agent(tmp_path, fake_docker) -> None:  # noqa: F811
    settings, db, runtime, agent = make_runtime(tmp_path)
    try:
        fake_docker.set(images=[settings.agent_image], networks=["den-agents"])
        db.execute("DELETE FROM agents WHERE id = ?", (agent["id"],))
        paths = agent_paths(settings, agent["id"])
        remove_agent_dirs(paths)

        with pytest.raises(AgentRuntimeError):
            await runtime.ensure_running(agent)  # the caller's stale row
        assert "run" not in fake_docker.subcommands()
        assert fake_docker.containers() == {}
        assert not paths.root.exists()
    finally:
        db.close()


async def test_local_runtime_refuses_a_deleted_agent(tmp_path) -> None:
    settings, db, _, agent = make_runtime(tmp_path)
    try:
        runtime = LocalRuntime(settings, SECRET, db)
        db.execute("DELETE FROM agents WHERE id = ?", (agent["id"],))
        paths = agent_paths(settings, agent["id"])
        remove_agent_dirs(paths)

        with pytest.raises(AgentRuntimeError):
            await runtime.ensure_running(agent)
        with pytest.raises(AgentRuntimeError):
            runtime.codex_config(agent)
        await runtime.restart(agent)  # quietly recreates nothing
        assert not paths.root.exists()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Idle reaper and failed setups
# --------------------------------------------------------------------------


class _ReaperDB:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.execute("CREATE TABLE agents (id TEXT, setup_state TEXT, last_active_at TEXT)")
        for agent_id, state in rows:
            self.c.execute("INSERT INTO agents VALUES (?, ?, ?)", (agent_id, state, "2000-01-01T00:00:00Z"))

    def all(self, q, p=()):
        return [dict(r) for r in self.c.execute(q, p).fetchall()]

    def one(self, q, p=()):
        r = self.c.execute(q, p).fetchone()
        return dict(r) if r else None


async def test_reaper_stops_containers_of_agents_whose_setup_failed() -> None:
    ids = {state: new_id() for state in ("ready", "error", "creating", "retrying")}
    db = _ReaperDB([(ids["ready"], "ready"), (ids["error"], "error"),
                    (ids["creating"], "creating"), (ids["retrying"], "error")])
    stopped: list[str] = []

    class Runtime:
        name = "docker"

        async def status(self, agent_id):
            return "running"

        async def stop(self, agent_id):
            stopped.append(agent_id)

    ctx = types.SimpleNamespace(
        runtime=Runtime(), db=db, settings=types.SimpleNamespace(idle_stop_minutes=15),
        turns=cb.TurnRegistry(), emit=lambda **k: None,
        get_agent=lambda agent_id: db.one("SELECT * FROM agents WHERE id = ?", (agent_id,)),
    )
    # A Retry has started setup again (its container must not be stopped under it).
    lifecycle._setup_tasks[ids["retrying"]] = object()  # type: ignore[assignment]
    try:
        assert await lifecycle.reap_idle_agents(ctx) == 2
    finally:
        lifecycle._setup_tasks.pop(ids["retrying"], None)
    assert sorted(stopped) == sorted([ids["ready"], ids["error"]])


# --------------------------------------------------------------------------
# Scratch homes left by a crash
# --------------------------------------------------------------------------


def _insert_turn(db, agent_id: str, turn_id: str, status: str) -> None:
    db.execute(
        "INSERT INTO turns (id, agent_id, source, conversation_no, prompt, status, started_at) "
        "VALUES (?, ?, 'job', 1, 'p', ?, ?)",
        (turn_id, agent_id, status, iso(utcnow())),
    )


async def test_sweep_keeps_scratch_homes_of_running_turns(tmp_path) -> None:
    settings, db, _, agent = make_runtime(tmp_path)
    try:
        paths = agent_paths(settings, agent["id"])
        running, finished, orphan = new_id(), new_id(), new_id()
        _insert_turn(db, agent["id"], running, "running")
        _insert_turn(db, agent["id"], finished, "interrupted")
        for key in (running, finished, orphan):
            prepare_scratch_home(settings, paths, agent["model"], key)
        ctx = types.SimpleNamespace(settings=settings, db=db, turns=cb.TurnRegistry())

        assert await lifecycle.remove_stale_scratch_homes(ctx) == 2
        left = sorted(p.name for p in (paths.codex_home / "runs").iterdir())
        assert left == [running]
    finally:
        db.close()


def test_startup_removes_scratch_homes_left_by_a_crash(server_factory, settings_factory, client_factory) -> None:
    settings = settings_factory(onboarding_probe=False)
    server = server_factory(settings)
    c = client_factory(server)
    register(c)
    agent = create_ready_agent(c)
    paths = agent_paths(server.settings, agent["id"])
    server.stop()

    # What a SIGKILL during a job run leaves: a `running` turn and its scratch home.
    turn_id = new_id()
    scratch = paths.codex_home / prepare_scratch_home(server.settings, paths, agent["model"], turn_id)
    (scratch / "sessions").mkdir()
    (scratch / "sessions" / "rollout.jsonl").write_text("{}\n")
    con = sqlite3.connect(server.settings.data_dir / "den.db")
    con.execute(
        "INSERT INTO turns (id, agent_id, source, conversation_no, prompt, status, started_at) "
        "VALUES (?, ?, 'job', 1, 'p', 'running', ?)",
        (turn_id, agent["id"], iso(utcnow())),
    )
    con.commit()
    con.close()

    restarted = server_factory(settings)
    row = restarted.ctx.db.one("SELECT status FROM turns WHERE id = ?", (turn_id,))
    assert row["status"] == "interrupted"
    wait_for(lambda: not scratch.exists(), timeout=10, message="the leftover scratch home to go")
