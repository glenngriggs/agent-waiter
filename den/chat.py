"""Chat routes: history, the live event stream (SSE), send a message, Stop.

The SSE stream is how a reopened tab catches up exactly: it subscribes to
the live bus first, replays persisted events newer than the browser's last
seq from SQLite, sends a snapshot of any reply still being streamed, then
forwards live events, dropping the ones it already replayed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import codex_bridge
from .auth import owned_agent
from .context import AppContext, get_ctx
from .db import iso, utcnow
from .events import row_to_event

logger = logging.getLogger("den.chat")

router = APIRouter(prefix="/api/agents/{agent_id}")

MAX_MESSAGE_LEN = 20000
HISTORY_DEFAULT, HISTORY_MAX = 300, 1000
REPLAY_BATCH = 500
PING_SECONDS = 15.0


class MessageBody(BaseModel):
    text: str = ""


def _sse(event: dict[str, Any]) -> str:
    """One SSE frame. Persisted events carry `id:` so EventSource resumes from them.

    json.dumps escapes newlines inside strings, so the data is a single line.
    """
    data = json.dumps(event, separators=(",", ":"))
    if event.get("seq") is not None:
        return f"id: {event['seq']}\ndata: {data}\n\n"
    return f"data: {data}\n\n"


def _parse_seq(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return max(value, 0)


@router.get("/history")
async def history(
    limit: int = HISTORY_DEFAULT,
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    limit = min(max(limit, 1), HISTORY_MAX)
    # Read the high-water mark first and cap the query at it, so `last_seq`
    # and `events` describe the same moment even if events are being written.
    # last_seq spans every channel: the browser hands it to /events, and job
    # events below it are not needed on the chat timeline.
    last_seq = ctx.db.one(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE agent_id = ?", (agent["id"],)
    )["s"]
    rows = ctx.db.all(
        "SELECT * FROM (SELECT * FROM events WHERE agent_id = ? AND seq <= ? "
        "AND channel IN ('chat', 'agent') ORDER BY seq DESC LIMIT ?) ORDER BY seq",
        (agent["id"], last_seq, limit),
    )
    runner = ctx.turns.chat.get(agent["id"])
    return {
        "events": [row_to_event(r) for r in rows],
        "last_seq": last_seq,
        "active": runner.snapshot() if runner is not None else None,
    }


@router.get("/events")
async def events(
    request: Request,
    after: str | None = None,
    last_event_id: str | None = Header(default=None),
    agent: dict = Depends(owned_agent),
    ctx: AppContext = Depends(get_ctx),
) -> StreamingResponse:
    # EventSource sends Last-Event-ID on its own reconnects; it wins over the
    # ?after= the page used for the first connect.
    start = _parse_seq(last_event_id)
    if start is None:
        start = _parse_seq(after) or 0
    return StreamingResponse(
        _event_stream(ctx, request, agent["id"], start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _event_stream(
    ctx: AppContext, request: Request, agent_id: str, after: int
) -> AsyncIterator[str]:
    # (1) Subscribe before reading the database: any event written while we
    # replay is then also in the queue, and the seq check below drops the copy.
    async with ctx.bus.subscribe(agent_id) as sub:
        # A comment frame first, so a client knows the subscription is live.
        yield ": connected\n\n"

        # (2) Replay persisted events newer than `after`, all channels.
        last = after
        while True:
            rows = ctx.db.all(
                "SELECT * FROM events WHERE agent_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (agent_id, last, REPLAY_BATCH),
            )
            for row in rows:
                last = row["seq"]
                yield _sse(row_to_event(row))
            if len(rows) < REPLAY_BATCH:
                break

        # (3) Snapshot the text of replies still streaming. Drain the queue and
        # take the snapshots with no await in between: every message_delta
        # already queued is then contained in the snapshot's partial text and
        # must be dropped, or the browser would show that text twice.
        pending: list[dict[str, Any]] = []
        while True:
            try:
                pending.append(sub.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        snapshots = []
        covered: set[tuple[str | None, str]] = set()
        for runner in ctx.turns.active_for_agent(agent_id):
            snap = runner.snapshot()
            snapshots.append((runner.channel, snap))
            covered.update((snap["turn_id"], item_id) for item_id in snap["items"])
        backlog = deque(
            ev for ev in pending
            if not (
                ev["kind"] == "message_delta"
                and (ev["turn_id"], ev["data"].get("item_id")) in covered
            )
        )
        for channel, snap in snapshots:
            yield _sse({
                "seq": None,
                "agent_id": agent_id,
                "turn_id": snap["turn_id"],
                "channel": channel,
                "kind": "snapshot",
                "data": snap,
                "at": iso(utcnow()),
            })

        # (4) Live events, oldest first: what was drained, then the queue.
        while True:
            if sub.overflowed:
                # The bus stopped feeding this subscriber. End the stream; the
                # browser reconnects with Last-Event-ID and replays from SQLite.
                logger.info("SSE subscriber for agent %s overflowed; closing", agent_id)
                return
            if backlog:
                event = backlog.popleft()
            else:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), PING_SECONDS)
                except asyncio.TimeoutError:
                    # (5) Disconnects are noticed here at the latest; Starlette
                    # also cancels this generator when the client goes away.
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
            seq = event.get("seq")
            if seq is not None:
                if seq <= last:
                    continue  # already sent during replay
                last = seq
            yield _sse(event)


@router.post("/messages", status_code=202)
async def send_message(
    body: MessageBody, agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)
) -> dict:
    if not body.text.strip() or len(body.text) > MAX_MESSAGE_LEN:
        raise HTTPException(400, f"A message must be 1 to {MAX_MESSAGE_LEN} characters.")
    if agent["setup_state"] != "ready":
        raise HTTPException(409, "This agent is still being set up.")
    try:
        turn_id = await codex_bridge.start_chat_turn(ctx, agent, body.text)
    except codex_bridge.Busy:
        raise HTTPException(
            409, "The agent is already working on a message. Stop it or wait for it to finish."
        ) from None
    return {"turn_id": turn_id}


@router.post("/stop")
async def stop(agent: dict = Depends(owned_agent), ctx: AppContext = Depends(get_ctx)) -> dict:
    # Only the chat turn: a scheduled run in the same container keeps going.
    runner = ctx.turns.chat.get(agent["id"])
    if runner is None:
        return {"stopped": False}
    await runner.stop("user")
    return {"stopped": True}
