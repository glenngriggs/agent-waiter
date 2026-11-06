"""Scheduler rules over a temporary database with a stub turn runner.

`den.scheduler.TurnRunner` is replaced by FakeRunner, whose run() returns a
chosen outcome after a chosen delay (or when the test releases it), so the
firing, missed-run, overlap, concurrency, cancel and timeout rules can be
checked quickly and without Codex.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

import den.scheduler as scheduler_module
from den.codex_bridge import TurnOutcome, TurnRegistry
from den.config import load_settings
from den.context import AppContext
from den.db import Database, iso, new_id, parse_iso, utcnow
from den.events import EventBus
from den.runtime.base import AgentRuntime
from den.scheduler import Scheduler, ScheduleError, create_job, job_out, run_out

RESTART_MESSAGE = "The platform restarted while this run was in progress."


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


class StubRuntime(AgentRuntime):
    """The fake runner never launches Codex, so nothing here is used."""

    name = "stub"

    async def prepare(self) -> None:
        return None

    async def ensure_running(self, agent: dict) -> None:
        return None

    async def stop(self, agent_id: str) -> None:
        return None

    async def restart(self, agent: dict) -> None:
        return None

    async def remove(self, agent_id: str) -> None:
        return None

    async def status(self, agent_id: str) -> str:
        return "running"

    def codex_config(self, agent: dict):
        raise AssertionError("the scheduler tests must not launch Codex")

    def workspace_cwd(self, agent: dict) -> str:
        return "/workspace"


def outcome(status: str = "completed", final: str | None = "All done.", kind=None, message=None):
    return TurnOutcome(
        status=status, final_response=final, error_kind=kind, error_message=message, thread_id="thr"
    )


class Control:
    """Per-test knobs shared by every FakeRunner."""

    def __init__(self) -> None:
        self.delay = 0.05
        self.release: asyncio.Event | None = None  # when set, runs wait for it
        self.outcome = outcome()
        self.on_start: Callable[["FakeRunner"], None] | None = None
        self.runners: list[FakeRunner] = []


class FakeRunner:
    """Same constructor and surface as codex_bridge.TurnRunner."""

    control: Control  # set by the fixture

    def __init__(
        self,
        ctx,
        *,
        agent: dict,
        turn_id: str,
        prompt: str,
        source: str,
        channel: str,
        thread_id: str | None,
        developer_instructions: str,
        ephemeral: bool = False,
        on_thread_started=None,
        **extra: Any,
    ) -> None:
        self.ctx = ctx
        self.agent = agent
        self.agent_id = agent["id"]
        self.turn_id = turn_id
        self.prompt = prompt
        self.source = source
        self.channel = channel
        self.thread_id = thread_id
        self.developer_instructions = developer_instructions
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.finished = False
        self.partial: dict[str, str] = {}
        self._stop_event = asyncio.Event()
        self.started = asyncio.Event()
        self.control.runners.append(self)

    async def run(self) -> TurnOutcome:
        self.started.set()
        control = self.control
        if control.on_start is not None:
            control.on_start(self)
        stop_wait = asyncio.ensure_future(self._stop_event.wait())
        if control.release is not None:
            gate = asyncio.ensure_future(control.release.wait())
        else:
            gate = asyncio.ensure_future(asyncio.sleep(control.delay))
        try:
            await asyncio.wait({stop_wait, gate}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Also runs when the scheduler's timeout cancels run().
            stop_wait.cancel()
            gate.cancel()
        result = (
            outcome("interrupted", None, "interrupted", "Stopped.")
            if self.stop_requested
            else control.outcome
        )
        # The real runner finishes its turns row; mirror that so the DB stays sane.
        self.ctx.db.execute(
            "UPDATE turns SET status=?, final_response=?, error_kind=?, error_message=?, "
            "finished_at=? WHERE id=?",
            (
                result.status,
                result.final_response,
                result.error_kind,
                result.error_message,
                iso(utcnow()),
                self.turn_id,
            ),
        )
        self.finished = True
        return result

    async def stop(self, reason: str = "user", **extra: Any) -> None:
        self.stop_requested = True
        self.stop_reason = reason
        self._stop_event.set()

    def snapshot(self) -> dict:
        return {"turn_id": self.turn_id, "items": dict(self.partial)}


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


class Env:
    def __init__(self, ctx: AppContext, control: Control) -> None:
        self.ctx = ctx
        self.db: Database = ctx.db
        self.sched: Scheduler = ctx.scheduler
        self.control = control
        now = iso(utcnow())
        self.user_id = new_id()
        self.db.execute(
            "INSERT INTO users (id, email, password_hash, timezone, created_at) VALUES (?,?,?,?,?)",
            (self.user_id, f"{self.user_id}@example.com", "x", "UTC", now),
        )
        self.agent = self.add_agent("Ada")
        self.started = False

    def start(self) -> None:
        self.started = True
        self.sched.start()

    def add_agent(self, name: str) -> dict:
        agent_id, now = new_id(), iso(utcnow())
        self.db.execute(
            "INSERT INTO agents (id, user_id, name, model, setup_state, created_at, last_active_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (agent_id, self.user_id, name, "gpt-6-luna", "ready", now, now),
        )
        return self.db.one("SELECT * FROM agents WHERE id=?", (agent_id,))

    def insert_job(
        self,
        *,
        next_run_at: datetime | None,
        kind: str = "interval",
        value: str = "60",
        status: str = "active",
        name: str = "Report",
        created_by: str = "user",
        agent: dict | None = None,
    ) -> dict:
        job_id = new_id()
        self.db.execute(
            "INSERT INTO jobs (id, agent_id, name, prompt, schedule_kind, schedule_value, timezone, "
            "created_by, status, next_run_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                (agent or self.agent)["id"],
                name,
                f"Do the {name} task.",
                kind,
                value,
                "UTC",
                created_by,
                status,
                iso(next_run_at) if next_run_at else None,
                iso(utcnow()),
            ),
        )
        return self.job(job_id)

    def insert_run(self, job: dict, status: str, **extra: Any) -> str:
        run_id = new_id()
        self.db.execute(
            "INSERT INTO job_runs (id, job_id, agent_id, scheduled_for, status, trigger, turn_id, "
            "created_at, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                job["id"],
                job["agent_id"],
                iso(utcnow()),
                status,
                "schedule",
                extra.get("turn_id"),
                iso(utcnow()),
                iso(utcnow()) if status == "running" else None,
            ),
        )
        return run_id

    def job(self, job_id: str) -> dict:
        return self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def runs(self, job_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM job_runs WHERE job_id=? ORDER BY created_at, rowid", (job_id,))

    def run(self, run_id: str) -> dict:
        return self.db.one("SELECT * FROM job_runs WHERE id=?", (run_id,))

    def runners_for(self, run_id: str) -> list[FakeRunner]:
        turn_id = self.run(run_id)["turn_id"]
        return [r for r in self.control.runners if r.turn_id == turn_id]

    async def wait_run(self, run_id: str, statuses: set[str], timeout: float = 5.0) -> dict:
        return await eventually(
            lambda: (r := self.run(run_id)) is not None and r["status"] in statuses and r,
            timeout,
            f"run to reach {statuses}",
        )

    async def single_run(self, job_id: str, timeout: float = 5.0) -> dict:
        runs = await eventually(lambda: self.runs(job_id), timeout, "the job to fire")
        return runs[0]


async def eventually(predicate: Callable[[], Any], timeout: float = 5.0, message: str = "condition"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.02)


async def maybe_await(value: Any) -> Any:
    """`fire` is documented as a plain call; tolerate an async implementation."""
    return await value if inspect.isawaitable(value) else value


@pytest.fixture
async def make_env(tmp_path, monkeypatch):
    envs: list[Env] = []

    async def make(**overrides: Any) -> Env:
        control = Control()
        FakeRunner.control = control
        monkeypatch.setattr(scheduler_module, "TurnRunner", FakeRunner)
        settings = load_settings({"DEN_RUNTIME": "local", "DEN_DATA_DIR": str(tmp_path)})
        settings = dataclasses.replace(
            settings,
            min_job_interval_minutes=1,
            scheduler_max_sleep_seconds=0.1,
            **overrides,
        )
        db = Database(tmp_path / f"den-{len(envs)}.db")
        db.migrate()
        ctx = AppContext(
            settings=settings,
            db=db,
            bus=EventBus(),
            runtime=StubRuntime(),
            secret=b"k" * 32,
            turns=TurnRegistry(),
        )
        ctx.scheduler = Scheduler(ctx)
        env = Env(ctx, control)
        envs.append(env)
        return env

    yield make

    for env in envs:
        if env.control.release is not None:
            env.control.release.set()
        if env.started:
            await env.sched.stop()
        for task in list(env.ctx.background):
            task.cancel()
        if env.ctx.background:
            await asyncio.gather(*env.ctx.background, return_exceptions=True)
        env.db.close()


# --------------------------------------------------------------------------
# Firing
# --------------------------------------------------------------------------


async def test_due_job_fires_once_and_advances_next_run_before_the_run_starts(make_env) -> None:
    env = await make_env()
    due = utcnow() - timedelta(seconds=1)
    job = env.insert_job(next_run_at=due)
    seen: dict[str, str] = {}
    env.control.on_start = lambda runner: seen.setdefault(
        "next_run_at", env.job(job["id"])["next_run_at"]
    )
    env.control.release = asyncio.Event()

    env.start()
    run = await env.single_run(job["id"])
    await env.wait_run(run["id"], {"running"})
    await asyncio.sleep(0.5)  # many loop iterations (max sleep is 0.1 s)

    assert len(env.runs(job["id"])) == 1, "a due job must fire exactly once"
    assert parse_iso(seen["next_run_at"]) >= due + timedelta(minutes=59)
    assert run["trigger"] == "schedule"
    assert parse_iso(run["scheduled_for"]) == due
    assert run["late"] == 0
    assert env.job(job["id"])["last_run_id"] == run["id"]

    env.control.release.set()
    done = await env.wait_run(run["id"], {"succeeded"})
    assert done["summary"] == "All done."
    assert done["finished_at"]


async def test_job_two_hours_overdue_runs_once_marked_late(make_env) -> None:
    env = await make_env()
    job = env.insert_job(next_run_at=utcnow() - timedelta(hours=2))
    env.start()

    run = await env.single_run(job["id"])
    run = await env.wait_run(run["id"], {"succeeded"})
    await asyncio.sleep(0.3)

    assert len(env.runs(job["id"])) == 1, "only one catch-up run, however many were missed"
    assert run["late"] == 1
    assert run_out(run)["late"] is True
    # The following run is counted from now, not from the missed time.
    assert parse_iso(env.job(job["id"])["next_run_at"]) > utcnow() + timedelta(minutes=55)


async def test_job_three_days_overdue_is_recorded_missed_and_not_run(make_env) -> None:
    env = await make_env()
    job = env.insert_job(next_run_at=utcnow() - timedelta(days=3))
    env.start()

    run = await env.single_run(job["id"])
    await asyncio.sleep(0.3)

    assert run["status"] == "missed"
    assert run["summary"].startswith("Missed")
    assert env.control.runners == []
    after = env.job(job["id"])
    assert after["status"] == "active"
    assert parse_iso(after["next_run_at"]) > utcnow()
    assert len(env.runs(job["id"])) == 1


async def test_once_job_three_days_overdue_is_missed_and_finished(make_env) -> None:
    env = await make_env()
    at = utcnow() - timedelta(days=3)
    job = env.insert_job(next_run_at=at, kind="once", value=iso(at))
    env.start()

    run = await env.single_run(job["id"])
    assert run["status"] == "missed"
    await eventually(lambda: env.job(job["id"])["status"] == "finished", message="job finished")
    assert env.job(job["id"])["next_run_at"] is None
    assert env.control.runners == []


async def test_fire_with_an_explicit_now(make_env) -> None:
    """fire(job, now) judges lateness by the `now` it is given."""
    env = await make_env()
    scheduled = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    job = env.insert_job(next_run_at=scheduled)
    await maybe_await(env.sched.fire(env.job(job["id"]), scheduled + timedelta(days=2)))

    [run] = env.runs(job["id"])
    assert run["status"] == "missed"
    assert parse_iso(env.job(job["id"])["next_run_at"]) == scheduled + timedelta(days=2, minutes=60)


async def test_occurrence_while_previous_run_is_running_is_skipped(make_env) -> None:
    env = await make_env()
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1))
    env.control.release = asyncio.Event()
    env.start()

    first = await env.single_run(job["id"])
    await env.wait_run(first["id"], {"running"})
    # The next occurrence comes due while the first run is still going.
    env.db.execute(
        "UPDATE jobs SET next_run_at=? WHERE id=?",
        (iso(utcnow() - timedelta(seconds=1)), job["id"]),
    )
    env.sched.wake()
    runs = await eventually(lambda: len(env.runs(job["id"])) == 2 and env.runs(job["id"]))

    assert runs[1]["status"] == "skipped"
    assert runs[1]["summary"].startswith("Skipped")
    assert len(env.control.runners) == 1
    env.control.release.set()
    await env.wait_run(first["id"], {"succeeded"})


async def test_once_job_ends_finished_after_its_run(make_env) -> None:
    env = await make_env()
    at = utcnow() - timedelta(seconds=1)
    job = env.insert_job(next_run_at=at, kind="once", value=iso(at))
    env.start()

    run = await env.single_run(job["id"])
    await env.wait_run(run["id"], {"succeeded"})
    after = await eventually(
        lambda: (j := env.job(job["id"]))["status"] == "finished" and j, message="job finished"
    )
    assert after["next_run_at"] is None
    assert len(env.runs(job["id"])) == 1


# --------------------------------------------------------------------------
# What a run does
# --------------------------------------------------------------------------


async def test_execute_records_a_job_turn_and_events(make_env) -> None:
    env = await make_env()
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1), name="Digest")
    env.start()

    run = await env.single_run(job["id"])
    run = await env.wait_run(run["id"], {"succeeded"})

    turn = env.db.one("SELECT * FROM turns WHERE id=?", (run["turn_id"],))
    assert turn["source"] == "job"
    assert turn["job_run_id"] == run["id"]
    assert turn["agent_id"] == env.agent["id"]

    [runner] = env.control.runners
    assert runner.source == "job"
    assert runner.channel == "job"
    assert runner.thread_id is None  # every run gets a fresh thread
    assert runner.prompt == job["prompt"]
    assert "Digest" in runner.developer_instructions

    events = env.db.all("SELECT * FROM events WHERE agent_id=? ORDER BY seq", (env.agent["id"],))
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["job_run_started"]["channel"] == "agent"
    assert by_kind["job_run_finished"]["channel"] == "agent"
    assert by_kind["turn_started"]["channel"] == "job"
    assert by_kind["turn_started"]["turn_id"] == run["turn_id"]
    assert '"succeeded"' in by_kind["job_run_finished"]["payload"]
    assert run["id"] not in env.ctx.turns.runs  # registry entry removed when done


@pytest.mark.parametrize(
    "result,status",
    [
        (outcome("failed", None, "rate_limited", "The model is rate-limited right now."), "failed"),
        (outcome("interrupted", None, "interrupted", "Stopped."), "interrupted"),
    ],
)
async def test_outcome_maps_to_run_status(make_env, result, status) -> None:
    env = await make_env()
    env.control.outcome = result
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1))
    env.start()

    run = await env.single_run(job["id"])
    run = await env.wait_run(run["id"], {status})
    assert run["error_message"] == result.error_message


async def test_summary_is_the_first_500_characters(make_env) -> None:
    env = await make_env()
    env.control.outcome = outcome(final="x" * 600)
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1))
    env.start()

    run = await env.wait_run((await env.single_run(job["id"]))["id"], {"succeeded"})
    assert run["summary"] == "x" * 500


# --------------------------------------------------------------------------
# Recovery, cancel, concurrency, timeout
# --------------------------------------------------------------------------


async def test_recover_on_startup_marks_running_work_interrupted(make_env) -> None:
    env = await make_env()
    interval_job = env.insert_job(next_run_at=utcnow() + timedelta(minutes=30))
    turn_id = new_id()
    env.db.execute(
        "INSERT INTO turns (id, agent_id, source, conversation_no, prompt, status, started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (turn_id, env.agent["id"], "job", 1, "p", "running", iso(utcnow())),
    )
    running = env.insert_run(interval_job, "running", turn_id=turn_id)
    once_job = env.insert_job(next_run_at=None, kind="once", value=iso(utcnow()))
    queued = env.insert_run(once_job, "queued")
    chat_turn = new_id()
    env.db.execute(
        "INSERT INTO turns (id, agent_id, source, conversation_no, prompt, status, started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (chat_turn, env.agent["id"], "chat", 1, "hello", "running", iso(utcnow())),
    )

    env.sched.recover_on_startup()

    for run_id in (running, queued):
        run = env.run(run_id)
        assert run["status"] == "interrupted"
        assert run["error_message"] == RESTART_MESSAGE
        assert run["finished_at"]
    for tid in (turn_id, chat_turn):
        turn = env.db.one("SELECT * FROM turns WHERE id=?", (tid,))
        assert turn["status"] == "interrupted"
        assert turn["error_kind"] == "platform_restart"
        assert turn["error_message"]
    assert env.job(interval_job["id"])["last_run_id"] == running
    assert env.job(interval_job["id"])["status"] == "active"
    assert env.job(once_job["id"])["last_run_id"] == queued
    assert env.job(once_job["id"])["status"] == "finished"


async def test_cancel_job_stops_a_running_run_and_clears_next_run(make_env) -> None:
    env = await make_env()
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1))
    env.control.release = asyncio.Event()
    env.start()
    run = await env.single_run(job["id"])
    await env.wait_run(run["id"], {"running"})

    await env.sched.cancel_job(job["id"])

    after = env.job(job["id"])
    assert after["status"] == "cancelled"
    assert after["cancelled_at"]
    assert after["next_run_at"] is None
    await env.wait_run(run["id"], {"interrupted"})
    [runner] = env.runners_for(run["id"])
    assert runner.stop_requested


async def test_cancel_job_interrupts_a_queued_run_before_it_starts(make_env) -> None:
    env = await make_env(max_concurrent_job_runs=1)
    env.control.release = asyncio.Event()
    jobs = [env.insert_job(next_run_at=utcnow() - timedelta(seconds=1), name=n) for n in "AB"]
    env.start()

    def by_status() -> dict[str, dict] | None:
        runs = [r for j in jobs for r in env.runs(j["id"])]
        found = {r["status"]: r for r in runs}
        return found if set(found) == {"running", "queued"} else None

    found = await eventually(by_status, message="one run running and one queued")
    running_run, queued_run = found["running"], found["queued"]

    await env.sched.cancel_job(queued_run["job_id"])
    cancelled = env.run(queued_run["id"])
    assert cancelled["status"] == "interrupted"
    assert cancelled["error_message"] == "Cancelled before it started."

    env.control.release.set()
    await env.wait_run(running_run["id"], {"succeeded"})
    await asyncio.sleep(0.2)
    assert env.run(queued_run["id"])["status"] == "interrupted"
    assert len(env.control.runners) == 1, "a cancelled queued run must never start"


async def test_semaphore_holds_the_fifth_run_in_queued(make_env) -> None:
    env = await make_env(max_concurrent_job_runs=4)
    env.control.release = asyncio.Event()
    jobs = [
        env.insert_job(next_run_at=utcnow() - timedelta(seconds=1), name=f"J{i}") for i in range(5)
    ]
    env.start()

    def statuses() -> list[str]:
        return sorted(r["status"] for j in jobs for r in env.runs(j["id"]))

    await eventually(lambda: statuses() == ["queued"] + ["running"] * 4, message="4 running, 1 queued")
    await asyncio.sleep(0.3)
    assert statuses() == ["queued"] + ["running"] * 4

    env.control.release.set()
    await eventually(lambda: statuses() == ["succeeded"] * 5, message="all five to succeed")


async def test_timeout_stops_a_run(make_env) -> None:
    # 0.01 minutes is 0.6 s; the setting is read as minutes * 60.
    env = await make_env(job_run_timeout_minutes=0.01)
    env.control.release = asyncio.Event()  # never set: the run would hang
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1))
    env.start()

    run = await env.single_run(job["id"])
    run = await env.wait_run(run["id"], {"failed", "interrupted"}, timeout=10)
    assert run["status"] == "failed"
    assert "longer than" in run["error_message"]
    [runner] = env.runners_for(run["id"])
    assert runner.stop_requested
    assert runner.stop_reason == "timeout"


# --------------------------------------------------------------------------
# run_now, create_job, job_out
# --------------------------------------------------------------------------


async def test_run_now_is_manual_and_does_not_move_the_schedule(make_env) -> None:
    env = await make_env()
    env.control.release = asyncio.Event()
    next_time = utcnow() + timedelta(minutes=30)
    job = env.insert_job(next_run_at=next_time)
    env.start()

    run = await env.sched.run_now(env.job(job["id"]))
    assert run["trigger"] == "manual"
    assert run["status"] in ("queued", "running")
    assert parse_iso(env.job(job["id"])["next_run_at"]) == next_time

    with pytest.raises(ScheduleError) as busy:
        await env.sched.run_now(env.job(job["id"]))
    assert busy.value.status == 409

    env.control.release.set()
    await env.wait_run(run["id"], {"succeeded"})

    await env.sched.cancel_job(job["id"])
    with pytest.raises(ScheduleError) as inactive:
        await env.sched.run_now(env.job(job["id"]))
    assert inactive.value.status == 409


async def test_create_job_sets_next_run_and_announces_it(make_env) -> None:
    env = await make_env()
    before = utcnow()
    async with env.ctx.bus.subscribe(env.agent["id"]) as sub:
        row = create_job(
            env.ctx,
            agent=env.agent,
            name="  Hourly check  ",
            prompt="Check the inbox.",
            kind="interval",
            raw_value=60,
            tz="UTC",
            created_by="user",
        )
        published = []
        while not sub.queue.empty():
            published.append(sub.queue.get_nowait())

    assert row["name"] == "Hourly check"
    assert row["status"] == "active"
    assert row["created_by"] == "user"
    assert row["schedule_kind"] == "interval" and row["schedule_value"] == "60"
    next_run = parse_iso(row["next_run_at"])
    assert before + timedelta(minutes=60) <= next_run <= utcnow() + timedelta(minutes=60)
    assert any(e["kind"] == "jobs_changed" and e["data"]["job_id"] == row["id"] for e in published)

    out = job_out(env.ctx, row)
    assert set(out) >= {
        "id", "name", "prompt", "schedule", "created_by", "status", "next_run_at",
        "last_run", "created_at", "cancelled_at",
    }
    assert set(out["schedule"]) >= {"kind", "value", "description", "timezone"}
    assert out["schedule"]["description"] == "Every 60 minutes"
    assert out["last_run"] is None


@pytest.mark.parametrize(
    "name,prompt",
    [("", "do it"), ("   ", "do it"), ("x" * 81, "do it"), ("ok", ""), ("ok", "p" * 8001)],
)
async def test_create_job_validates_name_and_prompt(make_env, name, prompt) -> None:
    env = await make_env()
    with pytest.raises(ScheduleError) as err:
        create_job(
            env.ctx, agent=env.agent, name=name, prompt=prompt, kind="interval",
            raw_value=60, tz="UTC", created_by="user",
        )
    assert err.value.status == 400


async def test_create_job_enforces_the_active_job_limit(make_env) -> None:
    env = await make_env(max_jobs_per_agent=2)
    for i in range(2):
        create_job(
            env.ctx, agent=env.agent, name=f"J{i}", prompt="p", kind="interval",
            raw_value=60, tz="UTC", created_by="agent",
        )
    with pytest.raises(ScheduleError) as err:
        create_job(
            env.ctx, agent=env.agent, name="J3", prompt="p", kind="interval",
            raw_value=60, tz="UTC", created_by="agent",
        )
    assert err.value.status == 409
    # Another agent has its own limit.
    other = env.add_agent("Bo")
    create_job(
        env.ctx, agent=other, name="J1", prompt="p", kind="interval",
        raw_value=60, tz="UTC", created_by="user",
    )


async def test_run_out_shape(make_env) -> None:
    env = await make_env()
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1))
    env.start()
    run = await env.wait_run((await env.single_run(job["id"]))["id"], {"succeeded"})
    out = run_out(run)
    assert set(out) >= {
        "id", "job_id", "status", "late", "trigger", "scheduled_for", "started_at",
        "finished_at", "summary", "error_message", "turn_id",
    }
    assert out["late"] is False
    assert job_out(env.ctx, env.job(job["id"]))["last_run"]["id"] == run["id"]
