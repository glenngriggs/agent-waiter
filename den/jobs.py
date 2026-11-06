"""Job and run routes: list, create, cancel, run now, run history, transcripts.

The scheduling rules live in den/scheduler.py and are shared with the MCP
tools; this router only checks ownership and translates errors. Every job
and run is looked up together with the agent id that `owned_agent` already
proved belongs to the user, so another agent's job id is a 404 too.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import scheduler as sched
from .auth import current_user, owned_agent
from .context import AppContext, get_ctx
from .events import row_to_event

logger = logging.getLogger("den.jobs")

router = APIRouter(prefix="/api/agents/{agent_id}")

RUNS_DEFAULT, RUNS_MAX = 20, 200

# Which field of the `schedule` object carries the value for each kind.
SCHEDULE_FIELDS = {"once": "at", "interval": "minutes", "cron": "expr"}


class CreateJobBody(BaseModel):
    name: str = ""
    prompt: str = ""
    schedule: dict[str, Any] = {}
    timezone: str | None = None


def _job_or_404(ctx: AppContext, agent_id: str, job_id: str) -> dict:
    row = ctx.db.one("SELECT * FROM jobs WHERE id = ? AND agent_id = ?", (job_id, agent_id))
    if row is None:
        raise HTTPException(404, "Job not found")
    return row


def _scheduler(ctx: AppContext) -> sched.Scheduler:
    if ctx.scheduler is None:  # only possible if the lifespan did not run
        raise HTTPException(503, "The scheduler is not running.")
    return ctx.scheduler


@router.get("/jobs")
async def list_jobs(agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)) -> dict:
    rows = ctx.db.all(
        "SELECT * FROM jobs WHERE agent_id = ? "
        "ORDER BY (status = 'active') DESC, created_at DESC",
        (agent["id"],),
    )
    return {"jobs": [sched.job_out(ctx, r) for r in rows]}


@router.post("/jobs", status_code=201)
async def create_job(
    body: CreateJobBody,
    agent: dict = Depends(owned_agent),
    user: dict = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    kind = body.schedule.get("kind")
    field = SCHEDULE_FIELDS.get(kind) if isinstance(kind, str) else None
    if field is None:
        raise HTTPException(400, "The schedule kind must be 'once', 'interval' or 'cron'.")
    if body.schedule.get(field) in (None, ""):
        raise HTTPException(400, f"A '{kind}' schedule needs a value for '{field}'.")
    # Cron expressions and naive 'once' times are read in this timezone;
    # default to the one the user's browser reported at sign-up.
    tz = (body.timezone or "").strip() or user["timezone"]
    try:
        row = sched.create_job(
            ctx,
            agent=agent,
            name=body.name,
            prompt=body.prompt,
            kind=kind,
            raw_value=body.schedule[field],
            tz=tz,
            created_by="user",
            source_turn_id=None,
        )
    except sched.ScheduleError as exc:
        # 400 for validation, 409 for the active-job limit (set by the scheduler).
        raise HTTPException(exc.status, str(exc)) from None
    logger.info("user created job %s on agent %s", row["id"], agent["id"])
    return {"job": sched.job_out(ctx, row)}


@router.delete("/jobs/{job_id}")
async def cancel_job(
    job_id: str, agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)
) -> dict:
    job = _job_or_404(ctx, agent["id"], job_id)
    # Cancelling a finished or already-cancelled job changes nothing, so the
    # request is idempotent instead of an error.
    if job["status"] == "active":
        try:
            await _scheduler(ctx).cancel_job(job_id)
        except sched.ScheduleError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        job = _job_or_404(ctx, agent["id"], job_id)
    return {"job": sched.job_out(ctx, job)}


@router.post("/jobs/{job_id}/run", status_code=202)
async def run_now(
    job_id: str, agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)
) -> dict:
    job = _job_or_404(ctx, agent["id"], job_id)
    try:
        run = await _scheduler(ctx).run_now(job)
    except sched.ScheduleError as exc:
        # 409 when the job is not active or a run is already queued or running.
        raise HTTPException(exc.status, str(exc)) from None
    return {"run": run}


@router.get("/jobs/{job_id}/runs")
async def list_runs(
    job_id: str,
    limit: int = RUNS_DEFAULT,
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    _job_or_404(ctx, agent["id"], job_id)
    limit = min(max(limit, 1), RUNS_MAX)
    rows = ctx.db.all(
        "SELECT * FROM job_runs WHERE job_id = ? AND agent_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (job_id, agent["id"], limit),
    )
    return {"runs": [sched.run_out(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str, agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)
) -> dict:
    run = ctx.db.one(
        "SELECT * FROM job_runs WHERE id = ? AND agent_id = ?", (run_id, agent["id"])
    )
    if run is None:
        raise HTTPException(404, "Run not found")
    # Skipped and missed runs never had a turn, so they have no transcript.
    events = []
    if run["turn_id"]:
        events = ctx.db.all(
            "SELECT * FROM events WHERE turn_id = ? AND agent_id = ? ORDER BY seq",
            (run["turn_id"], agent["id"]),
        )
    return {"run": sched.run_out(run), "events": [row_to_event(e) for e in events]}
