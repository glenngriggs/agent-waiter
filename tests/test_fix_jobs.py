"""Regressions for scheduler bugs: a run cancelling its own job, queued runs
starting during shutdown, the cron minimum-gap check, and DST fall-back."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from den import mcp_server
from den.db import utcnow
from den.scheduler import ScheduleError, compute_next_run, normalize_schedule
from tests.test_scheduler import eventually, make_env  # noqa: F401  (fixture)

SETTINGS = SimpleNamespace(min_job_interval_minutes=5)


# --------------------------------------------------------------------------
# A run that cancels its own job over MCP
# --------------------------------------------------------------------------


async def test_run_that_cancels_its_own_job_finishes_with_its_summary(make_env) -> None:
    env = await make_env()
    env.control.release = asyncio.Event()
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1), created_by="agent")
    results: list[dict] = []

    def on_start(runner) -> None:
        # Codex sends its own turn id in _meta; TurnRunner stores it on the row.
        env.db.execute("UPDATE turns SET codex_turn_id = 'codex-t1' WHERE id = ?", (runner.turn_id,))
        meta = {"x-codex-turn-metadata": {"turn_id": "codex-t1", "thread_id": "codex-th1"}}

        async def call() -> None:
            results.append(
                await mcp_server._call_tool(env.ctx, env.agent, "cancel_job", {"job_id": job["id"]}, meta)
            )
            env.control.release.set()  # the model goes on to write its summary

        asyncio.get_running_loop().create_task(call())

    env.control.on_start = on_start
    env.start()
    run = await env.single_run(job["id"])
    final = await env.wait_run(run["id"], {"succeeded", "failed", "interrupted"})

    assert results and not results[0].get("isError"), results
    assert final["status"] == "succeeded", (final["status"], final["error_message"])
    assert final["summary"] == "All done."
    assert not env.runners_for(run["id"])[0].stop_requested
    after = env.job(job["id"])
    assert after["status"] == "cancelled"
    assert after["next_run_at"] is None


async def test_mcp_cancel_from_another_turn_still_stops_the_running_run(make_env) -> None:
    env = await make_env()
    env.control.release = asyncio.Event()
    job = env.insert_job(next_run_at=utcnow() - timedelta(seconds=1), created_by="agent")
    env.start()
    run = await env.single_run(job["id"])
    await env.wait_run(run["id"], {"running"})

    # A chat turn (no matching job run) cancels the job.
    result = await mcp_server._call_tool(env.ctx, env.agent, "cancel_job", {"job_id": job["id"]}, None)

    assert not result.get("isError"), result
    final = await env.wait_run(run["id"], {"interrupted"})
    assert final["error_message"] == "The job was cancelled while this run was in progress."


# --------------------------------------------------------------------------
# Shutdown with a run waiting for a slot
# --------------------------------------------------------------------------


async def test_queued_run_does_not_start_once_shutdown_has_begun(make_env) -> None:
    env = await make_env(max_concurrent_job_runs=2)
    env.control.release = asyncio.Event()  # runs block until stopped
    jobs = [env.insert_job(next_run_at=utcnow() - timedelta(seconds=1), name=f"J{i}") for i in range(3)]
    env.start()

    def runs() -> list[dict]:
        return [r for j in jobs for r in env.runs(j["id"])]

    await eventually(
        lambda: sorted(r["status"] for r in runs()) == ["queued", "running", "running"],
        message="2 running, 1 queued",
    )
    [queued] = [r for r in runs() if r["status"] == "queued"]

    # The app's shutdown order: scheduler first, then every registered runner.
    await env.sched.stop()
    await env.ctx.turns.stop_all()
    await eventually(lambda: env.run(queued["id"])["status"] != "queued", message="queued run to end")
    await asyncio.sleep(0.2)

    assert len(env.control.runners) == 2, "a queued run started a runner during shutdown"
    final = env.run(queued["id"])
    assert final["status"] == "interrupted"
    assert final["error_message"] == "The platform shut down before this run started."
    assert final["turn_id"] is None


# --------------------------------------------------------------------------
# Cron minimum interval
# --------------------------------------------------------------------------


def _cron(expr: str, now: datetime, tz: str = "America/New_York") -> str:
    return normalize_schedule("cron", expr, tz, SETTINGS, now)


def test_cron_dense_after_its_first_six_occurrences_is_rejected() -> None:
    before_nine = datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc)  # 08:00 in New York
    with pytest.raises(ScheduleError, match="runs more often than every 5 minutes"):
        _cron("0,5,10,15,20,25,30-59 9 * * *", before_nine)


@pytest.mark.parametrize(
    "expr",
    [
        "0,58 0,23 * * 1,2",  # Monday 23:58 then Tuesday 00:00
        "0,59 0,23 29,1 2,3 *",  # Feb 29 23:59 then Mar 1 00:00
    ],
)
def test_cron_runs_too_close_across_midnight_are_rejected(expr: str) -> None:
    now = datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc)
    with pytest.raises(ScheduleError, match="runs more often"):
        _cron(expr, now)


@pytest.mark.parametrize(
    "expr",
    [
        "*/5 * * * *",
        "0 9 * * 1-5",
        "30 1 * * *",
        "0,58 0,23 * * 1,3,5",  # never two consecutive days
        "0,59 0,23 1 * *",  # the 1st of a month is never followed by another 1st
        "0 0 L * *",
    ],
)
def test_cron_with_enough_space_between_runs_is_accepted(expr: str) -> None:
    now = datetime(2026, 11, 5, 13, 0, tzinfo=timezone.utc)
    assert _cron(expr, now) == expr


# --------------------------------------------------------------------------
# DST fall-back
# --------------------------------------------------------------------------


def _chain(expr: str, start: datetime, n: int, tz: str = "America/New_York") -> list[str]:
    out, after = [], start
    for _ in range(n):
        after = compute_next_run("cron", expr, tz, after=after)
        out.append(after.strftime("%m-%d %H:%M"))
    return out


def test_daily_cron_in_the_repeated_hour_runs_once_on_fall_back_day() -> None:
    start = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    # 01:30 EDT on Nov 1 is 05:30Z; from Nov 2 on, 01:30 EST is 06:30Z.
    assert _chain("30 1 * * *", start, 3) == ["11-01 05:30", "11-02 06:30", "11-03 06:30"]


def test_wildcard_hour_cron_keeps_running_through_the_repeated_hour() -> None:
    start = datetime(2026, 11, 1, 4, 50, tzinfo=timezone.utc)  # 00:50 EDT
    # 01:00 EDT, 01:30 EDT, then 01:00 EST and 01:30 EST, then 02:00 EST.
    assert _chain("*/30 * * * *", start, 5) == [
        "11-01 05:00", "11-01 05:30", "11-01 06:00", "11-01 06:30", "11-01 07:00",
    ]
