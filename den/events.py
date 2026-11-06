"""Platform events: persisted to SQLite and fanned out to live subscribers.

`emit` is the only way events are created, so the database copy and the
live copy can never disagree. Events marked persisted get a `seq`, which
browsers use to resume a stream after reconnecting.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from .db import Database, iso, utcnow

PERSISTED_KINDS = frozenset(
    {
        "turn_started",
        "item_started",
        "item_completed",
        "notice",
        "turn_completed",
        "agent_status",
        "job_run_started",
        "job_run_finished",
    }
)

QUEUE_SIZE = 2000


class Subscription:
    def __init__(self, bus: "EventBus", agent_id: str) -> None:
        self.bus = bus
        self.agent_id = agent_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.overflowed = False

    async def __aenter__(self) -> "Subscription":
        self.bus._subs[self.agent_id].add(self)
        return self

    async def __aexit__(self, *exc: object) -> None:
        subs = self.bus._subs.get(self.agent_id)
        if subs is not None:
            subs.discard(self)
            if not subs:
                self.bus._subs.pop(self.agent_id, None)


class EventBus:
    """In-memory pub/sub per agent. Must be used from the event loop thread."""

    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = defaultdict(set)

    def subscribe(self, agent_id: str) -> Subscription:
        return Subscription(self, agent_id)

    def publish(self, event: dict[str, Any]) -> None:
        for sub in list(self._subs.get(event["agent_id"], ())):
            if sub.overflowed:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # The SSE handler ends this stream; the browser reconnects and
                # replays from the database, so nothing persisted is lost.
                sub.overflowed = True


def emit(
    db: Database,
    bus: EventBus,
    *,
    agent_id: str,
    turn_id: str | None,
    channel: str,
    kind: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    at = iso(utcnow())
    seq = None
    if kind in PERSISTED_KINDS:
        cur = db.execute(
            "INSERT INTO events (agent_id, turn_id, channel, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, turn_id, channel, kind, json.dumps(data), at),
        )
        seq = cur.lastrowid
    event = {
        "seq": seq,
        "agent_id": agent_id,
        "turn_id": turn_id,
        "channel": channel,
        "kind": kind,
        "data": data,
        "at": at,
    }
    bus.publish(event)
    return event


def row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "agent_id": row["agent_id"],
        "turn_id": row["turn_id"],
        "channel": row["channel"],
        "kind": row["kind"],
        "data": json.loads(row["payload"]),
        "at": row["created_at"],
    }
