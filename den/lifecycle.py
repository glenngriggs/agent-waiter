"""Agent lifecycle: onboarding, the idle reaper, deletion and the AgentOut shape.

Onboarding streams its progress as `agent_status` events (channel `agent`) so
the browser can show each step and, on failure, which step broke and why
(design doc, "Onboarding"). Deletion lives here so that stopping turns,
cancelling jobs, removing the container and removing the files always happen
together and in a safe order.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from .agent_files import agent_paths, init_agent_dirs, remove_agent_dirs, write_codex_config
from . import providers
from .codex_bridge import run_probe
from .db import iso, utcnow
from .runtime.base import AgentRuntimeError

logger = logging.getLogger("den.lifecycle")

# How often the idle reaper looks for containers to stop. Tests may lower it.
IDLE_CHECK_SECONDS = 60.0
# How long delete_agent waits for stopped turns and runs to write their final
# rows before it deletes the agent (their writes would fail once it is gone).
DELETE_DRAIN_SECONDS = 20.0

# Onboarding currently running, by agent id, so delete_agent can stop it
# before it creates a container for an agent that no longer exists. Agent ids
# are random, so one module-level map is safe even with several apps in tests.
_setup_tasks: dict[str, asyncio.Task] = {}

_STEP_MESSAGES = {
    "files": "Creating the agent's files…",
    "image": "Preparing the agent image (first time only, can take a few minutes)…",
    "container": "Starting the agent's container…",
    "probe": "Checking that the model answers…",
}


class _SetupFailed(Exception):
    """A setup step failed with a message meant for the user."""


class _AgentGone(Exception):
    """The agent was deleted while setup was running."""


def _emit_status(ctx: Any, agent_id: str, state: str, step: str, message: str) -> None:
    ctx.emit(
        agent_id=agent_id, turn_id=None, channel="agent", kind="agent_status",
        data={"state": state, "step": step, "message": message},
    )


def _current_agent(ctx: Any, agent_id: str) -> dict:
    """Re-read the agent between steps; stop quietly if it was deleted."""
    agent = ctx.get_agent(agent_id)
    if agent is None:
        raise _AgentGone()
    return agent


def _begin_step(ctx: Any, agent_id: str, step: str) -> dict:
    agent = _current_agent(ctx, agent_id)
    _emit_status(ctx, agent_id, "creating", step, _STEP_MESSAGES[step])
    return agent


async def setup_agent(ctx: Any, agent_id: str) -> None:
    """The onboarding pipeline: files, image, container, model probe, ready.

    Spawned in the background by POST /api/agents and by setup/retry. Never
    raises (except cancellation): failures become setup_state='error' with a
    `"<step>: <message>"` detail and an `agent_status` error event.
    """
    task = asyncio.current_task()
    if task is not None:
        _setup_tasks[agent_id] = task
    settings = ctx.settings
    step = "files"
    try:
        # Retry re-enters here, so (re)mark the agent as being set up.
        ctx.db.execute(
            "UPDATE agents SET setup_state = 'creating', setup_detail = NULL WHERE id = ?",
            (agent_id,),
        )
        agent = _begin_step(ctx, agent_id, "files")
        paths = agent_paths(settings, agent_id)
        init_agent_dirs(paths)
        write_codex_config(settings, paths, agent["model"])

        step = "image"
        _begin_step(ctx, agent_id, "image")
        await ctx.runtime.prepare()

        step = "container"
        agent = _begin_step(ctx, agent_id, "container")
        await ctx.runtime.ensure_running(agent)

        if settings.onboarding_probe:
            step = "probe"
            agent = _begin_step(ctx, agent_id, "probe")
            # One tiny real turn proves container, Codex, credentials and the
            # model all work, because a listed model does not always answer.
            outcome = await run_probe(ctx, agent)
            if outcome.status != "completed":
                raise _SetupFailed(outcome.error_message or "The model check did not finish.")

        _current_agent(ctx, agent_id)
        ctx.db.execute(
            "UPDATE agents SET setup_state = 'ready', setup_detail = NULL WHERE id = ?",
            (agent_id,),
        )
        _emit_status(ctx, agent_id, "ready", "ready", "Your agent is ready.")
        logger.info("agent %s is ready", agent_id)
    except _AgentGone:
        logger.info("agent %s was deleted during setup (step %s); setup stopped", agent_id, step)
    except _SetupFailed as exc:
        _fail_setup(ctx, agent_id, step, str(exc))
    except AgentRuntimeError as exc:
        logger.warning("agent %s setup failed at %s: %s | %s", agent_id, step, exc.message, exc.detail)
        _fail_setup(ctx, agent_id, step, exc.message)
    except asyncio.CancelledError:
        # delete_agent or shutdown; app startup turns a leftover 'creating'
        # into 'error' with a Retry, so there is nothing to record here.
        logger.info("setup of agent %s cancelled at step %s", agent_id, step)
        raise
    except Exception as exc:
        logger.exception("agent %s setup failed at %s", agent_id, step)
        _fail_setup(ctx, agent_id, step, f"Unexpected error: {exc or type(exc).__name__}")
    finally:
        if _setup_tasks.get(agent_id) is task:
            del _setup_tasks[agent_id]


def _fail_setup(ctx: Any, agent_id: str, step: str, message: str) -> None:
    logger.warning("agent %s setup failed at step %s: %s", agent_id, step, message)
    cur = ctx.db.execute(
        "UPDATE agents SET setup_state = 'error', setup_detail = ? WHERE id = ?",
        (f"{step}: {message}", agent_id),
    )
    if cur.rowcount:  # skip the event if the agent was deleted meanwhile
        _emit_status(ctx, agent_id, "error", step, message)


async def reap_idle_agents(ctx: Any) -> int:
    """One pass of the idle reaper; returns how many containers it stopped."""
    if ctx.runtime.name != "docker":
        return 0  # the local runtime has no containers to stop
    minutes = int(ctx.settings.idle_stop_minutes)
    cutoff = iso(utcnow() - timedelta(minutes=minutes))
    candidates = ctx.db.all(
        "SELECT id FROM agents WHERE setup_state = 'ready' AND last_active_at < ?", (cutoff,)
    )
    stopped = 0
    for row in candidates:
        agent_id = row["id"]
        try:
            if ctx.turns.active_for_agent(agent_id):
                continue
            if await ctx.runtime.status(agent_id) != "running":
                continue
            # Re-check after the await: a message or job may have started while
            # we asked Docker. With no await between this check and stop()
            # taking the runtime's per-agent lock, a turn starting now waits
            # for the stop and then starts the container again.
            if ctx.turns.active_for_agent(agent_id):
                continue
            await ctx.runtime.stop(agent_id)
            stopped += 1
            logger.info("stopped idle container of agent %s", agent_id)
            if ctx.get_agent(agent_id) is not None:
                _emit_status(
                    ctx, agent_id, "stopped", "container",
                    f"Stopped after {minutes} idle minutes. It starts again by itself when needed.",
                )
        except AgentRuntimeError as exc:
            logger.warning("idle stop of agent %s failed: %s | %s", agent_id, exc.message, exc.detail)
        except Exception:
            # One agent's failure must not keep the others running forever.
            logger.exception("idle check of agent %s failed", agent_id)
    return stopped


async def idle_reaper(ctx: Any) -> None:
    """Every IDLE_CHECK_SECONDS, stop containers idle for idle_stop_minutes."""
    while True:
        await asyncio.sleep(IDLE_CHECK_SECONDS)
        try:
            await reap_idle_agents(ctx)
        except Exception:
            logger.exception("idle reaper pass failed")


async def _wait_until_idle(ctx: Any, agent_id: str, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while ctx.turns.active_for_agent(agent_id):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.1)
    return True


async def delete_agent(ctx: Any, agent: dict) -> None:
    """Delete an agent completely: setup, turns, jobs, container, rows, files."""
    agent_id = agent["id"]
    logger.info("deleting agent %s", agent_id)

    # 1. Stop onboarding first, or it could create a container after we remove it.
    setup = _setup_tasks.get(agent_id)
    if setup is not None and not setup.done():
        setup.cancel()
        await asyncio.wait({setup}, timeout=DELETE_DRAIN_SECONDS)

    # 2. Cancel jobs before stopping turns, so the scheduler cannot fire a new
    #    run in between. cancel_job also stops any run in progress.
    if ctx.scheduler is not None:
        jobs = ctx.db.all(
            "SELECT id FROM jobs WHERE agent_id = ? AND (status = 'active' OR id IN "
            "(SELECT job_id FROM job_runs WHERE agent_id = ? AND status IN ('queued', 'running')))",
            (agent_id, agent_id),
        )
        for job in jobs:
            await ctx.scheduler.cancel_job(job["id"])

    # 3. Stop the chat turn and any remaining runs, then let them write their
    #    final rows and events while the agent row (their foreign key) exists.
    # Clear the chat queue first so no queued message starts as turns stop.
    from .codex_bridge import cancel_queue  # local import: codex_bridge imports this module's peers
    cancel_queue(ctx, agent_id, "Cancelled: the agent is being deleted.")
    await ctx.turns.stop_agent(agent_id)
    if not await _wait_until_idle(ctx, agent_id, DELETE_DRAIN_SECONDS):
        logger.warning("agent %s still had active turns after %.0f s; deleting anyway",
                       agent_id, DELETE_DRAIN_SECONDS)

    # 4. Remove the container. If Docker fails we still delete the agent: its
    #    token stops working once the row is gone, and the container carries
    #    den.managed/den.agent_id labels for an operator to clean up.
    try:
        await ctx.runtime.remove(agent_id)
    except AgentRuntimeError as exc:
        logger.error("could not remove container of deleted agent %s: %s | %s",
                     agent_id, exc.message, exc.detail)

    # 5. Rows (turns, jobs, runs and events cascade), then the files.
    ctx.db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    await asyncio.to_thread(remove_agent_dirs, agent_paths(ctx.settings, agent_id))
    logger.info("agent %s deleted", agent_id)


async def agent_out(ctx: Any, agent_row: dict) -> dict:
    """The AgentOut shape from the design doc."""
    agent_id = agent_row["id"]
    try:
        container = await ctx.runtime.status(agent_id)
    except AgentRuntimeError as exc:
        logger.warning("status of agent %s failed: %s", agent_id, exc.message)
        container = "unknown"
    chat_runner = ctx.turns.chat.get(agent_id)
    return {
        "id": agent_id,
        "name": agent_row["name"],
        "model": agent_row["model"],
        "provider": agent_row.get("provider") or providers.PLATFORM,
        "provider_label": providers.provider_label(ctx.settings, agent_row.get("provider") or providers.PLATFORM),
        "setup_state": agent_row["setup_state"],
        "setup_detail": agent_row["setup_detail"],
        "container": container,
        "queued_count": ctx.db.one(
            "SELECT COUNT(*) AS n FROM turns WHERE agent_id = ? AND source = 'chat' AND status = 'queued'",
            (agent_id,),
        )["n"],
        "chat_busy": ctx.turns.chat_busy(agent_id),
        "active_turn_id": chat_runner.snapshot().get("turn_id") if chat_runner else None,
        "conversation_no": agent_row["conversation_no"],
        "created_at": agent_row["created_at"],
        "last_active_at": agent_row["last_active_at"],
    }
