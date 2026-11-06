"""The platform's MCP endpoint: lets an agent manage its own scheduled jobs.

Neither Codex SDK can register tools, so each agent's config.toml points
Codex at this endpoint (streamable HTTP, JSON responses only). It is a
deliberately small JSON-RPC 2.0 server: `initialize`, `ping`, `tools/list`
and `tools/call`; notifications get 202 and GET/DELETE get 405, which is
what docs/codex-sdk.md verified Codex accepts.

The bearer token identifies the agent, so every tool is scoped to that
agent's own jobs. Agents may cancel only jobs they created themselves.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from . import scheduler as sched
from .context import AppContext, get_ctx
from .db import iso, parse_iso, utcnow
from .security import parse_agent_token

logger = logging.getLogger("den.mcp_server")

router = APIRouter()

# Used when a client's initialize omits protocolVersion; otherwise we echo
# whatever the client asked for (Codex 0.163 sends this version).
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MAX_BODY_BYTES = 1024 * 1024  # tool arguments are small; refuse anything huge
MAX_IN_MINUTES = 525600  # one year

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
UNAUTHORIZED = -32001  # implementation-defined server error range


# --------------------------------------------------------------------------
# Authentication (shared with the model proxy)
# --------------------------------------------------------------------------


def agent_from_bearer(ctx: AppContext, authorization: str | None) -> dict | None:
    """Return the agent a `Bearer <agent token>` header belongs to, or None.

    The HMAC proves we issued the token; the version check against the
    database is what makes Restart (which bumps token_version) revoke it.
    Agents whose setup failed are refused so a broken agent cannot keep
    spending model calls.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    parsed = parse_agent_token(ctx.secret, token.strip())
    if parsed is None:
        return None
    agent_id, version = parsed
    agent = ctx.get_agent(agent_id)
    if agent is None or int(agent["token_version"]) != version:
        return None
    if agent["setup_state"] == "error":
        return None
    return agent


# --------------------------------------------------------------------------
# JSON-RPC helpers
# --------------------------------------------------------------------------


def _rpc_result(msg_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id: Any, code: int, message: str, *, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class ToolFailure(Exception):
    """A tool could not do what was asked; the message is shown to the model."""


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------


def _tool_definitions(ctx: AppContext) -> list[dict[str, Any]]:
    # No `annotations` on purpose: hints such as destructiveHint can make
    # Codex ask for approval, and under ApprovalMode.deny_all that would
    # silently refuse every call.
    min_interval = ctx.settings.min_job_interval_minutes
    return [
        {
            "name": "schedule_job",
            "description": (
                "Create a scheduled job for this agent. Give exactly one of in_minutes, at, "
                "every_minutes or cron. Each run starts a fresh conversation that sees only "
                "the job's prompt, so make the prompt self-contained. The user can see and "
                "cancel every job."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short name shown to the user (1-80 characters).",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Complete instructions for each run (1-8000 characters), including "
                            "any context and file paths the run will need."
                        ),
                    },
                    "in_minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_IN_MINUTES,
                        "description": "Run once, this many minutes from now.",
                    },
                    "at": {
                        "type": "string",
                        "description": (
                            "Run once at this ISO 8601 time, e.g. 2026-11-06T09:00. A time "
                            "without an offset is read in `timezone`."
                        ),
                    },
                    "every_minutes": {
                        "type": "integer",
                        "minimum": min_interval,
                        "description": f"Repeat every N minutes (at least {min_interval}).",
                    },
                    "cron": {
                        "type": "string",
                        "description": (
                            "Repeat on a 5-field cron expression evaluated in `timezone`, "
                            f"e.g. '0 9 * * 1-5'. Runs must be at least {min_interval} minutes apart."
                        ),
                    },
                    "timezone": {
                        "type": "string",
                        "description": (
                            "IANA timezone such as America/Detroit. Defaults to the user's timezone."
                        ),
                    },
                },
                "required": ["name", "prompt"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_jobs",
            "description": "List this agent's active jobs with their schedules, next run and last result.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "cancel_job",
            "description": (
                "Cancel a job this agent created. Jobs the user created can only be "
                "cancelled by the user."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The job's id from list_jobs."}
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
    ]


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def _whole_minutes(value: Any, field: str) -> int:
    """Accept 30, 30.0 or "30" (models are not always strict about types)."""
    if isinstance(value, bool):
        raise ToolFailure(f"{field} must be a whole number of minutes.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ToolFailure(f"{field} must be a whole number of minutes.")


def _require_str(args: dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str):
        raise ToolFailure(f"{field} is required and must be a string.")
    return value


def _local_time(utc_text: str | None, tz: str) -> str:
    if not utc_text:
        return "none"
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    local = parse_iso(utc_text).astimezone(zone)
    return f"{local:%Y-%m-%d %H:%M %Z} ({tz})"


def _source_turn_id(ctx: AppContext, agent: dict, meta: Any) -> str | None:
    """Map Codex's turn metadata to our turns.id, if we can.

    Codex 0.163 sends `_meta["x-codex-turn-metadata"]` with its own thread
    and turn ids; TurnRunner stores those on the turns row, so a lookup
    scoped to this agent links the job to the turn that created it.
    """
    if not isinstance(meta, dict):
        return None
    info = meta.get("x-codex-turn-metadata")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            logger.warning("unparseable x-codex-turn-metadata from agent %s", agent["id"])
            return None
    if not isinstance(info, dict):
        return None
    codex_turn = info.get("turn_id") or info.get("turnId")
    codex_thread = info.get("thread_id") or info.get("threadId")
    if isinstance(codex_turn, str) and codex_turn:
        row = ctx.db.one(
            "SELECT id FROM turns WHERE agent_id = ? AND codex_turn_id = ?",
            (agent["id"], codex_turn),
        )
        if row:
            return row["id"]
    # Fall back to the running turn on that thread, in case the runner has
    # not stored the Codex turn id yet.
    if isinstance(codex_thread, str) and codex_thread:
        row = ctx.db.one(
            "SELECT id FROM turns WHERE agent_id = ? AND codex_thread_id = ? AND status = 'running' "
            "ORDER BY started_at DESC LIMIT 1",
            (agent["id"], codex_thread),
        )
        if row:
            return row["id"]
    return None


def _owner_timezone(ctx: AppContext, agent: dict) -> str:
    row = ctx.db.one("SELECT timezone FROM users WHERE id = ?", (agent["user_id"],))
    return (row or {}).get("timezone") or "UTC"


def _schedule_job(ctx: AppContext, agent: dict, args: dict[str, Any], meta: Any) -> str:
    name = _require_str(args, "name")
    prompt = _require_str(args, "prompt")
    given = [f for f in ("in_minutes", "at", "every_minutes", "cron") if args.get(f) is not None]
    if len(given) != 1:
        raise ToolFailure(
            "Give exactly one of in_minutes, at, every_minutes or cron "
            f"(got {', '.join(given) if given else 'none'})."
        )
    field = given[0]
    tz = args.get("timezone")
    if tz is None:
        tz = _owner_timezone(ctx, agent)
    elif not isinstance(tz, str):
        raise ToolFailure("timezone must be a string such as America/Detroit.")

    if field == "in_minutes":
        minutes = _whole_minutes(args[field], field)
        if not 1 <= minutes <= MAX_IN_MINUTES:
            raise ToolFailure(f"in_minutes must be between 1 and {MAX_IN_MINUTES}.")
        kind, raw = "once", iso(utcnow() + timedelta(minutes=minutes))
    elif field == "at":
        kind, raw = "once", _require_str(args, "at")
    elif field == "every_minutes":
        kind, raw = "interval", _whole_minutes(args[field], field)
    else:
        kind, raw = "cron", _require_str(args, "cron")

    job = sched.create_job(
        ctx,
        agent=agent,
        name=name,
        prompt=prompt,
        kind=kind,
        raw_value=raw,
        tz=tz,
        created_by="agent",
        source_turn_id=_source_turn_id(ctx, agent, meta),
    )
    description = sched.describe_schedule(job["schedule_kind"], job["schedule_value"], job["timezone"])
    logger.info("agent %s scheduled job %s (%s)", agent["id"], job["id"], description)
    return (
        f"Scheduled job {job['id']} '{job['name']}': {description}. "
        f"Next run: {_local_time(job['next_run_at'], job['timezone'])}."
    )


def _list_jobs(ctx: AppContext, agent: dict) -> str:
    rows = ctx.db.all(
        "SELECT * FROM jobs WHERE agent_id = ? AND status = 'active' ORDER BY created_at DESC",
        (agent["id"],),
    )
    jobs = []
    for row in rows:
        out = sched.job_out(ctx, row)
        last = out.get("last_run")
        jobs.append(
            {
                "id": out["id"],
                "name": out["name"],
                "schedule": out["schedule"]["description"],
                "next_run_at": out["next_run_at"],
                "next_run_local": _local_time(out["next_run_at"], row["timezone"]),
                "created_by": out["created_by"],
                "last_run": (
                    {"status": last["status"], "summary": last["summary"]} if last else None
                ),
            }
        )
    return json.dumps({"jobs": jobs}, indent=2)


async def _cancel_job(ctx: AppContext, agent: dict, args: dict[str, Any], meta: Any) -> str:
    job_id = _require_str(args, "job_id").strip()
    # Scoped to this agent, so another agent's job id looks like no job at all.
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ? AND agent_id = ?", (job_id, agent["id"]))
    if job is None:
        raise ToolFailure(f"There is no job {job_id} for this agent.")
    if job["created_by"] == "user":
        raise ToolFailure("Only the user can cancel this job because they created it.")
    if job["status"] != "active":
        raise ToolFailure(f"Job {job_id} is already {job['status']}; there is nothing to cancel.")
    if ctx.scheduler is None:
        logger.error("cancel_job called before the scheduler was set on the context")
        raise ToolFailure("The platform's scheduler is not running; try again shortly.")
    # A run may cancel its own job ("check until it passes, then stop"); that
    # run must finish, or the agent never sees this answer.
    caller_run_id = None
    caller_turn = _source_turn_id(ctx, agent, meta)
    if caller_turn is not None:
        row = ctx.db.one(
            "SELECT job_run_id FROM turns WHERE id = ? AND agent_id = ?", (caller_turn, agent["id"])
        )
        caller_run_id = (row or {}).get("job_run_id")
    await ctx.scheduler.cancel_job(job_id, exclude_run_id=caller_run_id)
    logger.info("agent %s cancelled its job %s", agent["id"], job_id)
    return f"Cancelled job {job_id}."


async def _call_tool(
    ctx: AppContext, agent: dict, name: str, args: dict[str, Any], meta: Any
) -> dict[str, Any]:
    """Run one tool; every failure becomes an isError result the model can read."""
    try:
        if name == "schedule_job":
            text = _schedule_job(ctx, agent, args, meta)
        elif name == "list_jobs":
            text = _list_jobs(ctx, agent)
        else:
            text = await _cancel_job(ctx, agent, args, meta)
    except (ToolFailure, sched.ScheduleError) as exc:
        logger.info("tool %s for agent %s refused: %s", name, agent["id"], exc)
        return _tool_result(str(exc), is_error=True)
    except Exception:
        logger.exception("tool %s for agent %s crashed", name, agent["id"])
        return _tool_result(
            "The platform hit an internal error running this tool. Try again later.",
            is_error=True,
        )
    return _tool_result(text)


TOOL_NAMES = frozenset({"schedule_job", "list_jobs", "cancel_job"})


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


async def _read_body(request: Request) -> bytes | None:
    """Read the body, or None when it is over MAX_BODY_BYTES."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/mcp")
async def mcp_post(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    agent = agent_from_bearer(ctx, request.headers.get("authorization"))
    if agent is None:
        response = _rpc_error(None, UNAUTHORIZED, "Invalid or revoked agent token.", status=401)
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    body = await _read_body(request)
    if body is None:
        return _rpc_error(None, INVALID_REQUEST, "Request body is too large.", status=413)
    try:
        msg = json.loads(body)
    except ValueError:
        return _rpc_error(None, PARSE_ERROR, "Body is not valid JSON.", status=400)
    if not isinstance(msg, dict):
        # Batches (arrays) were dropped from MCP in 2025-06-18; Codex never sends them.
        return _rpc_error(None, INVALID_REQUEST, "Send a single JSON-RPC object.", status=400)
    if msg.get("jsonrpc") != "2.0":
        return _rpc_error(msg.get("id"), INVALID_REQUEST, "jsonrpc must be \"2.0\".", status=400)

    method = msg.get("method")
    if method is None and ("result" in msg or "error" in msg):
        # A response to a server-to-client request. We never send any, but
        # the transport says such messages are acknowledged with 202.
        return Response(status_code=202)
    if not isinstance(method, str):
        return _rpc_error(msg.get("id"), INVALID_REQUEST, "method must be a string.", status=400)
    if "id" not in msg:
        # Notification (e.g. notifications/initialized): no response body.
        return Response(status_code=202)

    msg_id = msg["id"]
    if isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
        return _rpc_error(None, INVALID_REQUEST, "id must be a string or an integer.", status=400)
    params = msg.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc_error(msg_id, INVALID_PARAMS, "params must be an object.")

    if method == "initialize":
        version = params.get("protocolVersion")
        return _rpc_result(
            msg_id,
            {
                "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "den-platform", "version": __version__},
            },
        )
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": _tool_definitions(ctx)})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_NAMES:
            return _rpc_error(msg_id, INVALID_PARAMS, f"Unknown tool: {name!r}.")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _rpc_error(msg_id, INVALID_PARAMS, "arguments must be an object.")
        result = await _call_tool(ctx, agent, name, args, params.get("_meta"))
        return _rpc_result(msg_id, result)
    return _rpc_error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}.")


@router.api_route("/mcp", methods=["GET", "DELETE"])
async def mcp_not_allowed() -> Response:
    # No server-initiated SSE stream and no sessions to delete; 405 tells
    # streamable-HTTP clients (Codex included) to just use POST.
    return Response(status_code=405, headers={"Allow": "POST"})
