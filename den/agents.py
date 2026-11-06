"""Agent routes: public config, create/list/rename/delete, setup retry,
container start/stop/restart, and starting a new conversation.

Chat, files and jobs live in their own routers. Every route under
/api/agents/{agent_id} goes through `owned_agent`, so another user's agent
id is a 404.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from . import agent_files, lifecycle, providers
from . import codex_bridge
from .auth import current_user, owned_agent
from .context import AppContext, get_ctx
from .db import iso, new_id, utcnow
from .runtime.base import AgentRuntimeError

logger = logging.getLogger("den.agents")

router = APIRouter(prefix="/api")

MAX_NAME_LEN = 40
CONTAINER_ACTIONS = ("start", "stop", "restart")


class CreateAgentBody(BaseModel):
    name: str = ""
    model: str = ""
    provider: str | None = None


class PatchAgentBody(BaseModel):
    name: str | None = None
    model: str | None = None
    provider: str | None = None


def _clean_name(raw: str) -> str:
    name = raw.strip()
    if not (1 <= len(name) <= MAX_NAME_LEN):
        raise HTTPException(400, f"The agent's name must be 1 to {MAX_NAME_LEN} characters.")
    return name


def _provider_order(ctx: AppContext) -> list[str]:
    # When a request names only a model: the user's own keys first, then
    # the shared key.
    return [p.id for p in providers.user_providers(ctx.settings)] + [providers.PLATFORM]


def _check_model(
    ctx: AppContext, user: dict, model: str, provider: str | None
) -> tuple[str, str]:
    """(provider, model) after checking the user may run this model right now."""
    model = model.strip()
    candidates = [provider.strip()] if provider else _provider_order(ctx)
    offering = [p for p in candidates if providers.model_offered(ctx, user["id"], p, model)]
    if not offering:
        raise HTTPException(400, f"Unknown model '{model}'. Pick one of the offered models.")
    for p in offering:
        if providers.resolve_credentials(ctx, user["id"], p) is not None:
            return p, model
    raise HTTPException(400, providers.no_key_message(ctx.settings, offering[0]))


def _agent_status(ctx: AppContext, agent_id: str, state: str, message: str) -> None:
    """Container phase change, shown in the agent header and the timeline."""
    ctx.emit(
        agent_id=agent_id,
        turn_id=None,
        channel="agent",
        kind="agent_status",
        data={"state": state, "step": "container", "message": message},
    )


async def _fresh_out(ctx: AppContext, agent_id: str) -> dict:
    row = ctx.get_agent(agent_id)
    if row is None:  # deleted concurrently by another request
        raise HTTPException(404, "Agent not found")
    return await lifecycle.agent_out(ctx, row)


@router.get("/config")
async def public_config(ctx: AppContext = Depends(get_ctx)) -> dict:
    s = ctx.settings
    return {
        "models": [{"id": m, "label": s.label_for(m)} for m in s.models],
        "runtime": s.runtime,
        "model_auth": s.model_auth,
        "limits": {
            "min_job_interval_minutes": s.min_job_interval_minutes,
            "max_upload_mb": s.max_upload_mb,
            "max_agents_per_user": s.max_agents_per_user,
        },
    }


@router.get("/agents")
async def list_agents(
    user: dict = Depends(current_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    rows = ctx.db.all(
        "SELECT * FROM agents WHERE user_id = ? ORDER BY created_at", (user["id"],)
    )
    # agent_out asks the runtime for container status (a `docker inspect` per
    # agent), so build them concurrently.
    agents = await asyncio.gather(*(lifecycle.agent_out(ctx, r) for r in rows))
    return {"agents": list(agents)}


@router.post("/agents", status_code=201)
async def create_agent(
    body: CreateAgentBody, user: dict = Depends(current_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    name = _clean_name(body.name)
    provider, model = _check_model(ctx, user, body.model, body.provider)
    limit = ctx.settings.max_agents_per_user
    now = iso(utcnow())
    agent_id = new_id()
    # Count and insert in one transaction so two parallel creates cannot both
    # slip under the per-user limit.
    with ctx.db.transaction() as db:
        count = db.one("SELECT COUNT(*) AS n FROM agents WHERE user_id = ?", (user["id"],))["n"]
        if count >= limit:
            raise HTTPException(409, f"You can have at most {limit} agents. Delete one first.")
        db.execute(
            "INSERT INTO agents (id, user_id, name, model, provider, setup_state, setup_detail, "
            "token_version, chat_thread_id, conversation_no, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, 'creating', NULL, 1, NULL, 1, ?, ?)",
            (agent_id, user["id"], name, model, provider, now, now),
        )
    logger.info("user %s created agent %s (%s on %s)", user["id"], agent_id, model, provider)
    # Setup is owned by the backend, not this request, so it keeps going if
    # the browser leaves the onboarding page.
    ctx.spawn(lifecycle.setup_agent(ctx, agent_id), name=f"setup-agent-{agent_id}")
    return {"agent": await _fresh_out(ctx, agent_id)}


@router.get("/agents/{agent_id}")
async def get_agent(agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)) -> dict:
    return {"agent": await lifecycle.agent_out(ctx, agent)}


@router.patch("/agents/{agent_id}")
async def patch_agent(
    body: PatchAgentBody,
    agent: dict = Depends(owned_agent),
    user: dict = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    updates: dict[str, str] = {}
    if body.name is not None:
        updates["name"] = _clean_name(body.name)
    if body.model is not None:
        # A model alone keeps the agent's provider when that still offers it.
        provider = body.provider
        if provider is None and providers.model_offered(ctx, user["id"], agent["provider"], body.model.strip()):
            provider = agent["provider"]
        updates["provider"], updates["model"] = _check_model(ctx, user, body.model, provider)
    if updates:
        assignments = ", ".join(f"{col} = ?" for col in updates)
        ctx.db.execute(
            f"UPDATE agents SET {assignments} WHERE id = ?", (*updates.values(), agent["id"])
        )
    if "model" in updates and updates["model"] != agent["model"]:
        # The new model applies from the next turn: app-server reads
        # config.toml at launch, and the bridge also passes the model to
        # thread_resume. Skip agents whose directory does not exist yet;
        # setup writes the config itself.
        paths = agent_files.agent_paths(ctx.settings, agent["id"])
        if paths.root.is_dir():
            try:
                agent_files.write_codex_config(ctx.settings, paths, updates["model"])
            except OSError:
                # Not fatal: the runtime rewrites config.toml before every start.
                logger.exception("could not rewrite config.toml for agent %s", agent["id"])
    return {"agent": await _fresh_out(ctx, agent["id"])}


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)
) -> Response:
    await lifecycle.delete_agent(ctx, agent)
    logger.info("deleted agent %s", agent["id"])
    return Response(status_code=204)


@router.post("/agents/{agent_id}/setup/retry", status_code=202)
async def retry_setup(agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)) -> dict:
    # Conditional update: two Retry clicks cannot start two setups.
    cur = ctx.db.execute(
        "UPDATE agents SET setup_state = 'creating', setup_detail = NULL "
        "WHERE id = ? AND setup_state = 'error'",
        (agent["id"],),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, "Setup can only be retried after it has failed.")
    ctx.spawn(lifecycle.setup_agent(ctx, agent["id"]), name=f"setup-agent-{agent['id']}")
    return {"agent": await _fresh_out(ctx, agent["id"])}


@router.post("/agents/{agent_id}/container/{action}")
async def container_action(
    action: str, agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)
) -> dict:
    if action not in CONTAINER_ACTIONS:
        raise HTTPException(400, f"Unknown container action '{action}'. Use start, stop or restart.")
    if agent["setup_state"] != "ready":
        raise HTTPException(409, "The agent is not set up yet.")
    agent_id = agent["id"]
    try:
        if action == "start":
            if await ctx.runtime.status(agent_id) != "running":
                _agent_status(ctx, agent_id, "starting", "Starting the agent's container…")
            await ctx.runtime.ensure_running(agent)
            # Count a manual start as activity so the idle reaper does not
            # stop the container again within a minute.
            ctx.db.execute(
                "UPDATE agents SET last_active_at = ? WHERE id = ?", (iso(utcnow()), agent_id)
            )
            _agent_status(ctx, agent_id, "running", "The container is running.")
        elif action == "stop":
            # Clear the queue first, or the next message would start (and
            # restart the container) the moment the running turn ends. Then
            # stop every turn: closing their clients ends the Codex processes
            # cleanly and records the turns as interrupted.
            codex_bridge.cancel_queue(ctx, agent_id, "Cancelled: the container was stopped.")
            await ctx.turns.stop_agent(agent_id)
            await ctx.runtime.stop(agent_id)
            _agent_status(
                ctx, agent_id, "stopped",
                "The container was stopped. It starts again by itself when needed.",
            )
        else:
            _agent_status(ctx, agent_id, "restarting", "Recreating the agent's container…")
            codex_bridge.cancel_queue(ctx, agent_id, "Cancelled: the container was restarted.")
            await ctx.turns.stop_agent(agent_id)
            # restart() bumps token_version, revoking the old container's token.
            await ctx.runtime.restart(agent)
            _agent_status(ctx, agent_id, "running", "The container was recreated and is running.")
    except AgentRuntimeError as exc:
        logger.warning("container %s failed for agent %s: %s | %s",
                       action, agent_id, exc.message, exc.detail)
        ctx.emit(
            agent_id=agent_id,
            turn_id=None,
            channel="agent",
            kind="notice",
            data={"level": "error", "message": f"Could not {action} the container: {exc.message}"},
        )
        raise HTTPException(502, exc.message) from None
    return {"agent": await _fresh_out(ctx, agent_id)}


@router.post("/agents/{agent_id}/conversation/new")
async def new_conversation(
    agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)
) -> dict:
    # No await between this check and the update, so a message cannot start
    # in between on the single event loop.
    if ctx.turns.chat_busy(agent["id"]):
        raise HTTPException(409, "The agent is working on a message. Stop it or wait for it to finish.")
    ctx.db.execute(
        "UPDATE agents SET chat_thread_id = NULL, conversation_no = conversation_no + 1 "
        "WHERE id = ?",
        (agent["id"],),
    )
    ctx.emit(
        agent_id=agent["id"],
        turn_id=None,
        channel="chat",
        kind="notice",
        data={"level": "info", "message": "Started a new conversation."},
    )
    return {"agent": await _fresh_out(ctx, agent["id"])}
