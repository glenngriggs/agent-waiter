"""Scheduled jobs: schedule math, job creation, and the scheduler loop.

Jobs live in SQLite. One asyncio loop fires due jobs; each run gets a fresh
Codex thread through a TurnRunner of its own, so cancelling one run never
touches the chat or another run. The rules (design doc, "Jobs and the
scheduler"):

- firing advances `next_run_at` in the same transaction that records the run,
  so a crash can never fire one occurrence twice;
- an occurrence that comes due while the job's previous run is still queued or
  running is recorded as `skipped`;
- an occurrence late by less than `missed_run_grace_hours` runs once, marked
  late; later than that it is recorded as `missed` and the job moves on. Only
  one catch-up ever runs, because the following time is computed from now;
- at most `max_concurrent_job_runs` runs execute at once (the rest wait in
  `queued`), and a run is stopped after `job_run_timeout_minutes`.

Shared by the user API (den/jobs.py) and the agent's MCP tools
(den/mcp_server.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, CroniterError, croniter

from .agent_files import developer_instructions
from .codex_bridge import TurnOutcome, TurnRunner
from .db import iso, new_id, parse_iso, utcnow

logger = logging.getLogger("den.scheduler")

SCHEDULE_KINDS = ("once", "interval", "cron")
MAX_INTERVAL_MINUTES = 525_600  # one year
MAX_ONCE_AHEAD = timedelta(days=366)
# A `once` time this far in the past is still accepted (clock skew, slow form
# submit). The same slack decides whether a run counts as "late".
PAST_SLACK = timedelta(seconds=60)
CRON_CHECK_OCCURRENCES = 6
# How far ahead to look for two consecutive days that both match a cron
# expression's day fields: 28 years is a full cycle of weekdays and leap days.
CRON_DAY_SCAN = timedelta(days=28 * 366)
NAME_MAX = 80
PROMPT_MAX = 8000
SUMMARY_MAX = 500

# Which key of an API-style schedule dict ({kind, at|minutes|expr}) holds the value.
_RAW_KEYS = {"once": "at", "interval": "minutes", "cron": "expr"}

# TurnOutcome.status -> job_runs.status
_RUN_STATUS = {"completed": "succeeded", "failed": "failed", "interrupted": "interrupted"}

MISSED_SUMMARY = "Missed: the platform was not running at the scheduled time."
SKIPPED_SUMMARY = "Skipped: the previous run was still going."
CANCELLED_BEFORE_START = "Cancelled before it started."
CANCELLED_WHILE_RUNNING = "The job was cancelled while this run was in progress."
RESTART_RUN_MESSAGE = "The platform restarted while this run was in progress."
RESTART_TURN_MESSAGE = "The platform restarted while this was running."
SHUTDOWN_BEFORE_START = "The platform shut down before this run started."
SHUTDOWN_WHILE_RUNNING = "The platform shut down while this run was in progress."


class ScheduleError(ValueError):
    """A job request the user or agent can fix. `message` is shown verbatim.

    `status` is the HTTP status the routers answer with: 400 for validation,
    409 for conflicts (job limit, job not active, a run already in flight),
    404 for a job that does not exist.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Pure schedule math
# ---------------------------------------------------------------------------


def _zone(tz: Any) -> ZoneInfo:
    if not isinstance(tz, str) or not tz.strip():
        raise ScheduleError("A timezone is required, for example 'America/Detroit' or 'UTC'.")
    try:
        return ZoneInfo(tz.strip())
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        # ValueError covers malformed keys such as '../etc/passwd'.
        raise ScheduleError(
            f"'{tz}' is not a timezone this platform knows. Use an IANA name such as "
            "'America/Detroit' or 'UTC'."
        ) from None


def _plural_minutes(n: float) -> str:
    return "1 minute" if n == 1 else f"{n:g} minutes"


def _raw_field(kind: str, raw: Any) -> Any:
    """Accept either the bare value or the API's schedule dict."""
    if isinstance(raw, dict):
        key = _RAW_KEYS[kind]
        if key in raw:
            return raw[key]
        if "value" in raw:
            return raw["value"]
        raise ScheduleError(f"A '{kind}' schedule needs the field '{key}'.")
    return raw


def _parse_once(raw: Any, zone: ZoneInfo) -> datetime:
    if isinstance(raw, datetime):
        when = raw
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        # Python 3.12's fromisoformat accepts a trailing Z; normalise anyway
        # so a lowercase 'z' from a hand-written tool call also works.
        if text[-1] in "zZ":
            text = text[:-1] + "+00:00"
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            raise ScheduleError(
                f"'{raw}' is not a date and time this platform understands. Use ISO 8601, "
                "for example '2026-11-06T09:00' or '2026-11-06T14:00:00Z'."
            ) from None
    else:
        raise ScheduleError(
            "A one-time job needs a time in ISO 8601 form, for example '2026-11-06T09:00'."
        )
    if when.tzinfo is None:
        # A time without an offset is a wall-clock time in the job's timezone.
        when = when.replace(tzinfo=zone)
    return when.astimezone(timezone.utc)


def _parse_minutes(raw: Any, settings: Any) -> int:
    minutes: int | None = None
    if isinstance(raw, bool):  # bool is an int subclass; True is not "1 minute"
        minutes = None
    elif isinstance(raw, int):
        minutes = raw
    elif isinstance(raw, float) and raw.is_integer():
        minutes = int(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        minutes = int(raw.strip())
    if minutes is None:
        raise ScheduleError("The interval must be a whole number of minutes.")
    low = int(settings.min_job_interval_minutes)
    if not low <= minutes <= MAX_INTERVAL_MINUTES:
        raise ScheduleError(
            f"The interval must be between {_plural_minutes(low)} and "
            f"{MAX_INTERVAL_MINUTES} minutes (one year)."
        )
    return minutes


def _normalize_cron(raw: Any, zone: ZoneInfo, settings: Any, now: datetime) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ScheduleError(
            "A cron job needs a 5-field cron expression, for example '0 9 * * 1-5'."
        )
    fields = raw.split()
    # croniter also accepts 6/7-field (seconds, years) and '@daily' forms; the
    # platform documents only the classic 5 fields, so allow nothing else.
    if len(fields) != 5:
        raise ScheduleError(
            "A cron expression needs exactly 5 fields (minute hour day-of-month month "
            f"day-of-week), for example '0 9 * * 1-5'. '{raw.strip()}' has {len(fields)}."
        )
    expr = " ".join(fields)
    if not croniter.is_valid(expr):
        raise ScheduleError(f"'{expr}' is not a valid cron expression.")
    try:
        it = croniter(expr, now.astimezone(zone))
        times = [it.get_next(datetime).astimezone(timezone.utc) for _ in range(CRON_CHECK_OCCURRENCES)]
    except CroniterBadDateError:
        raise ScheduleError(f"The cron expression '{expr}' never matches a real date.") from None
    except (CroniterBadCronError, CroniterError, ValueError) as exc:
        raise ScheduleError(f"'{expr}' is not a valid cron expression ({exc}).") from None
    floor = timedelta(minutes=int(settings.min_job_interval_minutes))
    # The sample catches gaps that depend on real time near `now` (a DST change
    # this week); the wall-clock check covers every day, because six samples
    # miss an expression that is sparse first and dense later in the day.
    sampled_too_close = any(later - earlier < floor for earlier, later in zip(times, times[1:]))
    if sampled_too_close or _cron_runs_closer_than(expr, floor, now.astimezone(zone)):
        raise ScheduleError(
            f"The cron expression '{expr}' runs more often than every "
            f"{_plural_minutes(int(settings.min_job_interval_minutes))}, which is the "
            "shortest gap this platform allows."
        )
    return expr


def _cron_field_values(field: list, full: range) -> list[int]:
    return list(full) if field == ["*"] else sorted({int(v) for v in field})


def _cron_runs_closer_than(expr: str, floor: timedelta, local_now: datetime) -> bool:
    """Whether two runs of a valid cron expression are ever under `floor` apart
    on the wall clock.

    The minute and hour fields give the same times on every day that matches,
    so the only gaps are between those times within a day and from a day's
    last time to the next day's first time, if two consecutive days match.
    """
    fields = croniter(expr).expanded
    minutes = _cron_field_values(fields[0], range(60))
    hours = _cron_field_values(fields[1], range(24))
    day_times = sorted(h * 60 + m for h in hours for m in minutes)
    if any(timedelta(minutes=b - a) < floor for a, b in zip(day_times, day_times[1:])):
        return True
    overnight = timedelta(minutes=day_times[0] + 24 * 60 - day_times[-1])
    # Scanning the calendar is the slow part, so only do it when it matters.
    return overnight < floor and _consecutive_days_match(expr, day_times, local_now)


def _consecutive_days_match(expr: str, day_times: list[int], local_now: datetime) -> bool:
    """Whether some day's last run is followed by a run on the next day."""
    last_h, last_m = divmod(day_times[-1], 60)
    first_h, first_m = divmod(day_times[0], 60)
    day_fields = " ".join(expr.split()[2:])
    # Naive datetimes: this is calendar arithmetic, independent of the zone.
    start = local_now.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    it = croniter(f"{last_m} {last_h} {day_fields}", start)
    try:
        while (day := it.get_next(datetime)) < start + CRON_DAY_SCAN:
            next_day = (day + timedelta(days=1)).replace(hour=first_h, minute=first_m)
            if croniter.match(expr, next_day):
                return True
    except (CroniterBadDateError, CroniterError):
        pass  # no further matching day
    return False


def normalize_schedule(
    kind: str, raw: dict | str | int | datetime, tz: str, settings: Any, now: datetime
) -> str:
    """Validate a schedule and return the text stored in `jobs.schedule_value`.

    once     -> UTC ISO time (via db.iso); at most 60 s in the past, 366 days ahead
    interval -> whole minutes as text, min_job_interval_minutes..525600
    cron     -> the 5-field expression with whitespace normalised
    Raises ScheduleError with a sentence meant for the user (or the agent).
    """
    if kind not in SCHEDULE_KINDS:
        raise ScheduleError("The schedule kind must be 'once', 'interval' or 'cron'.")
    zone = _zone(tz)
    value = _raw_field(kind, raw)
    if kind == "once":
        when = _parse_once(value, zone)
        if when < now - PAST_SLACK:
            raise ScheduleError(
                f"The time {format_in_tz(iso(when), tz)} ({tz}) is in the past. "
                "Pick a time in the future."
            )
        if when > now + MAX_ONCE_AHEAD:
            raise ScheduleError("A one-time job can be scheduled at most 366 days ahead.")
        return iso(when)
    if kind == "interval":
        return str(_parse_minutes(value, settings))
    return _normalize_cron(value, zone, settings, now)


def compute_next_run(kind: str, value: str, tz: str, *, after: datetime) -> datetime | None:
    """The next time a job is due strictly after `after` (once: None if gone)."""
    if kind == "once":
        when = parse_iso(value)
        return when if when > after - PAST_SLACK else None
    if kind == "interval":
        return after + timedelta(minutes=int(value))
    if kind == "cron":
        # Cron is evaluated on the wall clock of the job's timezone, so
        # "0 9 * * *" stays at 09:00 local across a DST change.
        zone = ZoneInfo(tz)
        it = croniter(value, after.astimezone(zone))
        # When clocks fall back, croniter returns a wall time in the repeated
        # hour twice. Like classic cron, a fixed-time job ('30 1 * * *') runs
        # only the first time; a job with '*' in its minute or hour field
        # ('*/30 * * * *') keeps running through the repeated hour.
        minute, hour = value.split()[:2]
        fixed_time = not (minute.startswith("*") or hour.startswith("*"))
        while True:
            nxt = it.get_next(datetime).astimezone(timezone.utc)
            if not (fixed_time and nxt.astimezone(zone).fold == 1):
                return nxt
    raise ValueError(f"unknown schedule kind {kind!r}")


def format_in_tz(value_iso: str, tz: str) -> str:
    """'2026-11-06 09:00' for a stored UTC time shown in `tz`."""
    try:
        return parse_iso(value_iso).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        return value_iso


def describe_schedule(kind: str, value: str, tz: str) -> str:
    """Human description shown in the Jobs tab and returned to the agent."""
    if kind == "once":
        return f"Once at {format_in_tz(value, tz)} ({tz})"
    if kind == "interval":
        try:
            minutes = int(value)
        except ValueError:
            return f"Every {value} minutes"
        return "Every minute" if minutes == 1 else f"Every {minutes} minutes"
    if kind == "cron":
        return f"Cron `{value}` ({tz})"
    return f"{kind} {value}"


# ---------------------------------------------------------------------------
# Jobs: creation and output shapes
# ---------------------------------------------------------------------------


def _emit_jobs_changed(ctx: Any, agent_id: str, job_id: str) -> None:
    ctx.emit(
        agent_id=agent_id, turn_id=None, channel="agent", kind="jobs_changed",
        data={"job_id": job_id},
    )


def _clean_text(value: Any, label: str, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(text) <= limit:
        raise ScheduleError(f"The job needs a {label} of 1 to {limit} characters.")
    return text


def create_job(
    ctx: Any,
    *,
    agent: dict,
    name: str,
    prompt: str,
    kind: str,
    raw_value: Any,
    tz: str,
    created_by: str,
    source_turn_id: str | None = None,
) -> dict:
    """Validate and insert a job; returns the new `jobs` row.

    Raises ScheduleError (400 validation, 409 when the agent is at its job limit).
    """
    if created_by not in ("user", "agent"):
        raise ValueError(f"created_by must be 'user' or 'agent', not {created_by!r}")
    clean_name = _clean_text(name, "name", NAME_MAX)
    clean_prompt = _clean_text(prompt, "prompt", PROMPT_MAX)
    tz = tz.strip() if isinstance(tz, str) else tz
    if not tz:
        # Callers normally pass the user's timezone; fall back to the owner's.
        owner = ctx.db.one(
            "SELECT u.timezone FROM users u JOIN agents a ON a.user_id = u.id WHERE a.id = ?",
            (agent["id"],),
        )
        tz = (owner or {}).get("timezone") or "UTC"
    now = utcnow()
    value = normalize_schedule(kind, raw_value, tz, ctx.settings, now)
    if kind == "once":
        # The stored time itself is the one occurrence (even if it is up to
        # 60 s in the past, in which case it fires on the next tick).
        next_run = value
    else:
        nxt = compute_next_run(kind, value, tz, after=now)
        next_run = iso(nxt) if nxt is not None else None

    job_id = new_id()
    limit = int(ctx.settings.max_jobs_per_agent)
    # Count and insert in one transaction so two concurrent creates cannot
    # both slip under the limit.
    with ctx.db.transaction():
        active = ctx.db.one(
            "SELECT COUNT(*) AS n FROM jobs WHERE agent_id = ? AND status = 'active'",
            (agent["id"],),
        )["n"]
        if active >= limit:
            raise ScheduleError(
                f"This agent already has {limit} active jobs, the most allowed. "
                "Cancel one before scheduling another.",
                409,
            )
        ctx.db.execute(
            "INSERT INTO jobs (id, agent_id, name, prompt, schedule_kind, schedule_value, "
            "timezone, created_by, source_turn_id, status, next_run_at, last_run_id, "
            "created_at, cancelled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, NULL)",
            (
                job_id, agent["id"], clean_name, clean_prompt, kind, value, tz, created_by,
                source_turn_id, next_run, iso(now),
            ),
        )
    row = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    logger.info(
        "job %s created for agent %s by %s: %s", job_id, agent["id"], created_by,
        describe_schedule(kind, value, tz),
    )
    _emit_jobs_changed(ctx, agent["id"], job_id)
    if ctx.scheduler is not None:
        ctx.scheduler.wake()
    return row


def run_out(run_row: dict) -> dict:
    """The RunOut shape from the design doc (late as a bool)."""
    return {
        "id": run_row["id"],
        "job_id": run_row["job_id"],
        "status": run_row["status"],
        "late": bool(run_row["late"]),
        "trigger": run_row["trigger"],
        "scheduled_for": run_row["scheduled_for"],
        "started_at": run_row["started_at"],
        "finished_at": run_row["finished_at"],
        "summary": run_row["summary"],
        "error_message": run_row["error_message"],
        "turn_id": run_row["turn_id"],
    }


def job_out(ctx: Any, job_row: dict) -> dict:
    """The JobOut shape from the design doc, with the last run embedded."""
    last_run = None
    if job_row.get("last_run_id"):
        row = ctx.db.one(
            "SELECT * FROM job_runs WHERE id = ? AND job_id = ?",
            (job_row["last_run_id"], job_row["id"]),
        )
        last_run = run_out(row) if row else None
    kind, value, tz = job_row["schedule_kind"], job_row["schedule_value"], job_row["timezone"]
    return {
        "id": job_row["id"],
        "name": job_row["name"],
        "prompt": job_row["prompt"],
        "schedule": {
            "kind": kind,
            "value": value,
            "description": describe_schedule(kind, value, tz),
            "timezone": tz,
        },
        "created_by": job_row["created_by"],
        "status": job_row["status"],
        "next_run_at": job_row["next_run_at"],
        "last_run": last_run,
        "created_at": job_row["created_at"],
        "cancelled_at": job_row["cancelled_at"],
    }


async def _stop_runner(runner: Any, reason: str, message: str | None = None) -> None:
    """Stop a runner, passing `message` when its stop() accepts one.

    The shared contract is `stop(reason)`; den.codex_bridge's TurnRunner also
    takes `message=` (shown as the turn's error). Test doubles may not, so the
    keyword is passed only when the signature has it.
    """
    try:
        params = inspect.signature(runner.stop).parameters
    except (TypeError, ValueError):
        params = {}
    try:
        if message is not None and "message" in params:
            await runner.stop(reason, message=message)
        else:
            await runner.stop(reason)
    except Exception:
        logger.exception("stopping runner (%s) failed", reason)


def _platform_outcome(status: str, kind: str, message: str) -> TurnOutcome:
    """An outcome the scheduler decides itself (timeout, crash, shutdown)."""
    return TurnOutcome(
        status=status, final_response=None, error_kind=kind, error_message=message,
        thread_id=None,
    )


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    # After stop() on a timed-out or cancelled run, how long the runner gets to
    # wind down by itself before its task is cancelled. Tests may lower it.
    STOP_GRACE_SECONDS = 15.0
    # A job whose fire() raised is left alone this long, so one broken row
    # cannot spin the loop (its next_run_at stays in the past).
    FIRE_BACKOFF = timedelta(seconds=60)

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._wake = asyncio.Event()
        self._slots = asyncio.Semaphore(max(1, int(ctx.settings.max_concurrent_job_runs)))
        self._task: asyncio.Task | None = None
        self._backoff: dict[str, datetime] = {}
        # Runs stopped by cancel_job, so their result says "cancelled" rather
        # than the runner's generic "Stopped."
        self._cancelled_runs: set[str] = set()
        self._stopping = False

    # -- loop control -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = self.ctx.spawn(self._loop(), name="scheduler-loop")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def wake(self) -> None:
        """Re-check the schedule now (a job was created, cancelled or changed)."""
        self._wake.set()

    async def _loop(self) -> None:
        max_sleep = float(self.ctx.settings.scheduler_max_sleep_seconds)
        logger.info("scheduler started")
        while True:
            # Clear before reading the table: a wake() that arrives while we
            # fire is then not lost, it just makes the next sleep end at once.
            self._wake.clear()
            delay = max_sleep
            try:
                now = utcnow()
                self.run_due(now)
                delay = self._seconds_until_next(utcnow(), max_sleep)
            except Exception:
                # Never let one bad tick (a locked DB, a bug) end scheduling.
                logger.exception("scheduler tick failed; retrying in %.0f s", max_sleep)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, delay))
            except TimeoutError:
                pass

    def run_due(self, now: datetime | None = None) -> int:
        """Fire every job that is due at `now`; returns how many fired."""
        now = now or utcnow()
        due = self.ctx.db.all(
            "SELECT * FROM jobs WHERE status = 'active' AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at",
            (iso(now),),
        )
        fired = 0
        for job in due:
            until = self._backoff.get(job["id"])
            if until is not None and until > now:
                continue
            try:
                if self.fire(job, now) is not None:
                    fired += 1
                self._backoff.pop(job["id"], None)
            except Exception:
                logger.exception("could not fire job %s; leaving it for %s", job["id"], self.FIRE_BACKOFF)
                self._backoff[job["id"]] = now + self.FIRE_BACKOFF
        return fired

    def _seconds_until_next(self, now: datetime, max_sleep: float) -> float:
        rows = self.ctx.db.all(
            "SELECT id, next_run_at FROM jobs WHERE status = 'active' "
            "AND next_run_at IS NOT NULL ORDER BY next_run_at"
        )
        wake_at: datetime | None = None
        for row in rows:
            until = self._backoff.get(row["id"])
            if until is not None and until > now:
                # A backed-off job is due again only when its backoff ends.
                wake_at = until if wake_at is None else min(wake_at, until)
                continue
            due = parse_iso(row["next_run_at"])
            wake_at = due if wake_at is None else min(wake_at, due)
            break  # rows are sorted, so this is the earliest non-backed-off job
        if wake_at is None:
            return max_sleep
        return min(max_sleep, max(0.0, (wake_at - now).total_seconds()))

    # -- firing ---------------------------------------------------------------

    def fire(self, job: dict, now: datetime) -> dict | None:
        """Record one due occurrence of `job` and start its run if allowed.

        Returns the new `job_runs` row, or None if the job was no longer due
        (cancelled or already fired by the time we got the lock).
        """
        db = self.ctx.db
        grace = timedelta(hours=float(self.ctx.settings.missed_run_grace_hours))
        start_run = False
        with db.transaction():
            # Re-read under the lock: `job` came from a query outside it.
            job = db.one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
            if (
                job is None
                or job["status"] != "active"
                or job["next_run_at"] is None
                or job["next_run_at"] > iso(now)
            ):
                return None
            kind = job["schedule_kind"]
            scheduled_for = parse_iso(job["next_run_at"])
            # Advance first. Computing from max(now, scheduled_for) is what
            # makes a long outage produce one catch-up, not a burst.
            if kind == "once":
                next_run = None
            else:
                nxt = compute_next_run(
                    kind, job["schedule_value"], job["timezone"], after=max(now, scheduled_for)
                )
                next_run = iso(nxt) if nxt is not None else None
            lateness = now - scheduled_for
            job_status = job["status"]
            late = 0
            summary = None
            if lateness > grace:
                run_status, summary = "missed", MISSED_SUMMARY
                if kind == "once":
                    job_status = "finished"
            elif db.one(
                "SELECT 1 AS x FROM job_runs WHERE job_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (job["id"],),
            ):
                run_status, summary = "skipped", SKIPPED_SUMMARY
            else:
                run_status = "queued"
                late = 1 if lateness > PAST_SLACK else 0
                start_run = True
            run_id = new_id()
            at = iso(now)
            finished_at = None if run_status == "queued" else at
            db.execute(
                "INSERT INTO job_runs (id, job_id, agent_id, scheduled_for, status, late, trigger, "
                "turn_id, summary, error_message, created_at, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'schedule', NULL, ?, NULL, ?, NULL, ?)",
                (run_id, job["id"], job["agent_id"], iso(scheduled_for), run_status, late,
                 summary, at, finished_at),
            )
            db.execute(
                "UPDATE jobs SET next_run_at = ?, last_run_id = ?, status = ? WHERE id = ?",
                (next_run, run_id, job_status, job["id"]),
            )
            run = db.one("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        logger.info(
            "job %s fired for %s: run %s %s%s; next run %s", job["id"], iso(scheduled_for),
            run_id, run_status, " (late)" if late else "", next_run,
        )
        if start_run:
            # Spawned after the commit so execute() sees the committed rows.
            self.ctx.spawn(self.execute(run_id), name=f"job-run-{run_id}")
        _emit_jobs_changed(self.ctx, job["agent_id"], job["id"])
        return run

    async def run_now(self, job: dict) -> dict:
        """Start a manual run now; it does not replace the scheduled occurrence."""
        db = self.ctx.db
        now = iso(utcnow())
        with db.transaction():
            fresh = db.one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
            if fresh is None:
                raise ScheduleError("That job does not exist.", 404)
            if fresh["status"] != "active":
                raise ScheduleError(f"This job is {fresh['status']}, so it cannot run.", 409)
            if db.one(
                "SELECT 1 AS x FROM job_runs WHERE job_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (fresh["id"],),
            ):
                raise ScheduleError("A run of this job is already queued or running.", 409)
            run_id = new_id()
            db.execute(
                "INSERT INTO job_runs (id, job_id, agent_id, scheduled_for, status, late, trigger, "
                "created_at) VALUES (?, ?, ?, ?, 'queued', 0, 'manual', ?)",
                (run_id, fresh["id"], fresh["agent_id"], now, now),
            )
            db.execute("UPDATE jobs SET last_run_id = ? WHERE id = ?", (run_id, fresh["id"]))
            run = db.one("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        logger.info("job %s: manual run %s queued", fresh["id"], run_id)
        self.ctx.spawn(self.execute(run_id), name=f"job-run-{run_id}")
        _emit_jobs_changed(self.ctx, fresh["agent_id"], fresh["id"])
        return run_out(run)

    # -- executing ------------------------------------------------------------

    async def execute(self, run_id: str) -> None:
        """Run one queued job run to completion (waits for a free slot first)."""
        try:
            async with self._slots:
                started = self._start_run(run_id)
                if started is None:
                    return  # cancelled (or deleted) while it waited for a slot
                run, job, agent, turn_id = started
                runner = None
                try:
                    try:
                        runner = TurnRunner(
                            self.ctx,
                            agent=agent,
                            turn_id=turn_id,
                            prompt=job["prompt"],
                            source="job",
                            channel="job",
                            thread_id=None,  # every run is a fresh conversation
                            developer_instructions=developer_instructions(
                                agent["name"], purpose="job", job_name=job["name"]
                            ),
                            scratch_home=True,  # may run while chat uses the main home
                        )
                        self.ctx.turns.runs[run_id] = runner
                        outcome, timed_out = await self._run_with_timeout(runner, run_id)
                    except Exception as exc:
                        # A platform bug must still end the run, or it would sit
                        # in 'running' (and block the job) until the next restart.
                        logger.exception("run %s failed inside the platform", run_id)
                        outcome = _platform_outcome(
                            "failed", "other", f"The run failed because of a platform error: {exc}"
                        )
                        timed_out = False
                    self._finish_run(run, job, turn_id, outcome, timed_out=timed_out)
                finally:
                    # Removed only after the run's rows and events are final, so
                    # "no active runners" means everything is written (delete
                    # relies on this before removing the agent's rows).
                    if runner is not None and self.ctx.turns.runs.get(run_id) is runner:
                        del self.ctx.turns.runs[run_id]
                    self._cancelled_runs.discard(run_id)
        except asyncio.CancelledError:
            # Platform shutdown: leave an accurate record. Anything we cannot
            # write now is fixed by recover_on_startup() on the next start.
            self._interrupt_unfinished(run_id)
            raise

    def _start_run(self, run_id: str) -> tuple[dict, dict, dict, str] | None:
        """Move a queued run to running and create its turn; None if not queued."""
        db = self.ctx.db
        now = iso(utcnow())
        with db.transaction():
            run = db.one("SELECT * FROM job_runs WHERE id = ?", (run_id,))
            if run is None or run["status"] != "queued":
                return None
            if self._stopping:
                # Shutdown frees slots as it stops the running runs; a queued
                # run that takes one must not start a runner nobody will stop.
                db.execute(
                    "UPDATE job_runs SET status = 'interrupted', error_message = ?, finished_at = ? "
                    "WHERE id = ?",
                    (SHUTDOWN_BEFORE_START, now, run_id),
                )
                return None
            job = db.one(
                "SELECT * FROM jobs WHERE id = ? AND agent_id = ?", (run["job_id"], run["agent_id"])
            )
            agent = db.one("SELECT * FROM agents WHERE id = ?", (run["agent_id"],))
            if job is None or agent is None or job["status"] == "cancelled":
                db.execute(
                    "UPDATE job_runs SET status = 'interrupted', error_message = ?, finished_at = ? "
                    "WHERE id = ?",
                    (CANCELLED_BEFORE_START, now, run_id),
                )
                return None
            turn_id = new_id()
            db.execute(
                "INSERT INTO turns (id, agent_id, source, job_run_id, conversation_no, prompt, "
                "status, started_at) VALUES (?, ?, 'job', ?, ?, ?, 'running', ?)",
                (turn_id, agent["id"], run_id, agent["conversation_no"], job["prompt"], now),
            )
            db.execute(
                "UPDATE job_runs SET status = 'running', turn_id = ?, started_at = ? WHERE id = ?",
                (turn_id, now, run_id),
            )
            run = db.one("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        logger.info("job %s: run %s started (turn %s)", job["id"], run_id, turn_id)
        self.ctx.emit(
            agent_id=agent["id"], turn_id=None, channel="agent", kind="job_run_started",
            data={
                "job_id": job["id"], "run_id": run_id, "job_name": job["name"],
                "late": bool(run["late"]), "trigger": run["trigger"],
            },
        )
        self.ctx.emit(
            agent_id=agent["id"], turn_id=turn_id, channel="job", kind="turn_started",
            data={
                "source": "job", "prompt": job["prompt"], "job_id": job["id"],
                "run_id": run_id, "job_name": job["name"],
            },
        )
        return run, job, agent, turn_id

    async def _run_with_timeout(self, runner: Any, run_id: str) -> tuple[TurnOutcome, bool]:
        """Await runner.run(), stopping it after job_run_timeout_minutes.

        On timeout we call runner.stop() and let run() end by itself instead of
        cancelling it (what asyncio.wait_for would do), so the runner still
        closes its client and writes its turn row the normal way.
        """
        minutes = self.ctx.settings.job_run_timeout_minutes
        task = asyncio.create_task(runner.run(), name=f"job-turn-{run_id}")
        try:
            done, _ = await asyncio.wait({task}, timeout=float(minutes) * 60)
        except asyncio.CancelledError:
            await self._stop_and_drain(runner, task, "shutdown")
            raise
        if task in done:
            return self._task_outcome(task, run_id), False
        logger.warning("run %s exceeded %s minutes; stopping it", run_id, minutes)
        message = f"The run took longer than {_plural_minutes(minutes)} and was stopped."
        await self._stop_and_drain(runner, task, "timeout", message)
        return _platform_outcome("failed", "timeout", message), True

    async def _stop_and_drain(
        self, runner: Any, task: asyncio.Task, reason: str, message: str | None = None
    ) -> None:
        await _stop_runner(runner, reason, message)
        done, _ = await asyncio.wait({task}, timeout=self.STOP_GRACE_SECONDS)
        if not done:
            logger.error("runner did not end %.0f s after stop(%s); cancelling it", self.STOP_GRACE_SECONDS, reason)
            task.cancel()
            await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            logger.error("runner raised after stop(%s)", reason, exc_info=task.exception())

    def _task_outcome(self, task: asyncio.Task, run_id: str) -> TurnOutcome:
        if task.cancelled():
            return _platform_outcome("interrupted", "interrupted", "Stopped.")
        exc = task.exception()
        if exc is not None:
            # TurnRunner.run() is meant to turn every failure into an outcome;
            # if it raised anyway, record it instead of leaving the run running.
            logger.error("runner for run %s raised", run_id, exc_info=exc)
            return _platform_outcome(
                "failed", "other", f"The run failed because of a platform error: {exc}"
            )
        return task.result()

    def _close_turn_if_running(self, agent_id: str, turn_id: str, outcome: TurnOutcome) -> bool:
        """Finish a turn row the runner did not finish itself (crash, cancel)."""
        cur = self.ctx.db.execute(
            "UPDATE turns SET status = ?, final_response = ?, error_kind = ?, error_message = ?, "
            "finished_at = ? WHERE id = ? AND status = 'running'",
            (outcome.status, outcome.final_response, outcome.error_kind, outcome.error_message,
             iso(utcnow()), turn_id),
        )
        if cur.rowcount:
            self.ctx.emit(
                agent_id=agent_id, turn_id=turn_id, channel="job", kind="turn_completed",
                data={
                    "status": outcome.status, "final_response": outcome.final_response,
                    "error_kind": outcome.error_kind, "error_message": outcome.error_message,
                },
            )
        return bool(cur.rowcount)

    def _finish_run(
        self, run: dict, job: dict, turn_id: str, outcome: TurnOutcome, *, timed_out: bool
    ) -> None:
        db = self.ctx.db
        agent_id, run_id = run["agent_id"], run["id"]
        closed_here = self._close_turn_if_running(agent_id, turn_id, outcome)
        if timed_out and not closed_here:
            # The runner closed the turn itself. TurnRunner.stop("timeout")
            # records failed/timeout; a runner that did not (one that ignores
            # the reason) left "interrupted", so record the real reason.
            turn = db.one("SELECT error_kind FROM turns WHERE id = ?", (turn_id,))
            if turn is not None and turn["error_kind"] != "timeout":
                db.execute(
                    "UPDATE turns SET status = 'failed', error_kind = 'timeout', error_message = ? "
                    "WHERE id = ?",
                    (outcome.error_message, turn_id),
                )
                self.ctx.emit(
                    agent_id=agent_id, turn_id=turn_id, channel="job", kind="notice",
                    data={"level": "error", "message": outcome.error_message},
                )

        status = _RUN_STATUS.get(outcome.status, "failed")
        summary = (outcome.final_response or "").strip()[:SUMMARY_MAX] or None
        if status == "succeeded":
            error_message = None
        elif status == "interrupted" and run_id in self._cancelled_runs:
            error_message = CANCELLED_WHILE_RUNNING
        elif status == "interrupted" and self._stopping:
            # App shutdown stops every runner after stopping the scheduler;
            # say so instead of the runner's generic "Stopped.".
            error_message = SHUTDOWN_WHILE_RUNNING
        else:
            error_message = outcome.error_message or "The run did not finish."
        now = iso(utcnow())
        with db.transaction():
            db.execute(
                "UPDATE job_runs SET status = ?, summary = ?, error_message = ?, finished_at = ? "
                "WHERE id = ?",
                (status, summary, error_message, now, run_id),
            )
            # "When a run ends, the job's last result is updated" - even if an
            # occurrence was skipped meanwhile, this run is the real result.
            db.execute("UPDATE jobs SET last_run_id = ? WHERE id = ?", (run_id, job["id"]))
            # A once job is done when its one scheduled occurrence has been
            # consumed (next_run_at NULL); a manual run before then is not it.
            db.execute(
                "UPDATE jobs SET status = 'finished' WHERE id = ? AND schedule_kind = 'once' "
                "AND status = 'active' AND next_run_at IS NULL",
                (job["id"],),
            )
        logger.info("job %s: run %s %s", job["id"], run_id, status)
        self.ctx.emit(
            agent_id=agent_id, turn_id=None, channel="agent", kind="job_run_finished",
            data={
                "job_id": job["id"], "run_id": run_id, "job_name": job["name"],
                "status": status, "summary": summary, "error_message": error_message,
            },
        )
        _emit_jobs_changed(self.ctx, agent_id, job["id"])

    def _interrupt_unfinished(self, run_id: str) -> None:
        """Best-effort record for a run cut off by shutdown."""
        try:
            now = iso(utcnow())
            run = self.ctx.db.one("SELECT * FROM job_runs WHERE id = ?", (run_id,))
            if run is None or run["status"] not in ("queued", "running"):
                return
            message = SHUTDOWN_BEFORE_START if run["status"] == "queued" else SHUTDOWN_WHILE_RUNNING
            self.ctx.db.execute(
                "UPDATE job_runs SET status = 'interrupted', error_message = ?, finished_at = ? "
                "WHERE id = ? AND status IN ('queued', 'running')",
                (message, now, run_id),
            )
            if run["turn_id"]:
                self.ctx.db.execute(
                    "UPDATE turns SET status = 'interrupted', error_kind = 'platform_restart', "
                    "error_message = ?, finished_at = ? WHERE id = ? AND status = 'running'",
                    (RESTART_TURN_MESSAGE, now, run["turn_id"]),
                )
        except Exception:
            # The database may already be closed during shutdown; the next
            # start's recover_on_startup() marks the run interrupted instead.
            logger.warning("could not record shutdown of run %s; recovery will", run_id, exc_info=True)

    # -- cancelling and recovery ------------------------------------------------

    async def cancel_job(self, job_id: str, *, exclude_run_id: str | None = None) -> None:
        """Cancel a job: no more occurrences, queued run dropped, running run stopped.

        `exclude_run_id` is a run that cancelled its own job (the MCP tool). It
        is left to finish: stopping it would kill the turn while it waits for
        the tool's answer and lose whatever it meant to do after cancelling.
        """
        db = self.ctx.db
        now = iso(utcnow())
        with db.transaction():
            job = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
            if job is None:
                raise ScheduleError("That job does not exist.", 404)
            # Only an active job becomes cancelled; a finished one stays finished.
            db.execute(
                "UPDATE jobs SET status = 'cancelled', cancelled_at = ?, next_run_at = NULL "
                "WHERE id = ? AND status = 'active'",
                (now, job_id),
            )
            db.execute(
                "UPDATE job_runs SET status = 'interrupted', error_message = ?, finished_at = ? "
                "WHERE job_id = ? AND status = 'queued'",
                (CANCELLED_BEFORE_START, now, job_id),
            )
            running = db.all(
                "SELECT id FROM job_runs WHERE job_id = ? AND status = 'running' AND id IS NOT ?",
                (job_id, exclude_run_id),
            )
        logger.info("job %s cancelled (%d running run(s) to stop)", job_id, len(running))
        for row in running:
            self._cancelled_runs.add(row["id"])
            runner = self.ctx.turns.runs.get(row["id"])
            if runner is None:
                continue
            # The run's execute() records it as interrupted when run() ends.
            await _stop_runner(runner, "user", CANCELLED_WHILE_RUNNING)
        _emit_jobs_changed(self.ctx, job["agent_id"], job_id)
        self.wake()

    def recover_on_startup(self) -> None:
        """Mark work cut off by the last shutdown or crash as interrupted.

        Nothing can still be running in this new process, so every `queued` or
        `running` run and every `running` turn is stale.
        """
        db = self.ctx.db
        now = iso(utcnow())
        with db.transaction():
            runs = db.all(
                "SELECT * FROM job_runs WHERE status IN ('queued', 'running') ORDER BY created_at"
            )
            turns = db.all("SELECT * FROM turns WHERE status = 'running'")
            db.execute(
                "UPDATE job_runs SET status = 'interrupted', error_message = ?, finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (RESTART_RUN_MESSAGE, now),
            )
            db.execute(
                "UPDATE turns SET status = 'interrupted', error_kind = 'platform_restart', "
                "error_message = ?, finished_at = ? WHERE status = 'running'",
                (RESTART_TURN_MESSAGE, now),
            )
            for run in runs:  # oldest first, so the newest run per job wins
                db.execute("UPDATE jobs SET last_run_id = ? WHERE id = ?", (run["id"], run["job_id"]))
            # A once job whose one occurrence is used up (next_run_at NULL) and
            # that has nothing in flight any more can never run again.
            finished = db.execute(
                "UPDATE jobs SET status = 'finished' WHERE schedule_kind = 'once' "
                "AND status = 'active' AND next_run_at IS NULL"
            ).rowcount
            job_ids = sorted({run["job_id"] for run in runs})
            job_names = {}
            if job_ids:
                marks = ",".join("?" for _ in job_ids)
                job_names = {
                    r["id"]: r["name"]
                    for r in db.all(f"SELECT id, name FROM jobs WHERE id IN ({marks})", job_ids)
                }
        if runs or turns or finished:
            logger.warning(
                "startup recovery: %d run(s) and %d turn(s) marked interrupted, %d once job(s) finished",
                len(runs), len(turns), finished,
            )
        # Tell the timelines, so a reopened tab shows why a reply or run stopped.
        for turn in turns:
            self.ctx.emit(
                agent_id=turn["agent_id"], turn_id=turn["id"],
                channel="chat" if turn["source"] == "chat" else "job", kind="turn_completed",
                data={
                    "status": "interrupted", "final_response": None,
                    "error_kind": "platform_restart", "error_message": RESTART_TURN_MESSAGE,
                },
            )
        for run in runs:
            if run["status"] == "running":  # queued runs never posted a start card
                self.ctx.emit(
                    agent_id=run["agent_id"], turn_id=None, channel="agent", kind="job_run_finished",
                    data={
                        "job_id": run["job_id"], "run_id": run["id"],
                        "job_name": job_names.get(run["job_id"], ""), "status": "interrupted",
                        "summary": None, "error_message": RESTART_RUN_MESSAGE,
                    },
                )
