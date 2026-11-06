"""Token usage, counted by the model proxy and shown on the Settings page.

Each model reply is one row update in `usage_daily` (per user, agent, day,
provider and model). Passed-through replies are scanned for the Responses
API's `response.completed` event, which carries the usage; Claude replies
report it from the Messages API directly.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from .db import utcnow

logger = logging.getLogger("den.usage")

# List prices in USD per million tokens (input, output, cached input), for
# the estimate on the Settings page only. Anthropic's published rates as of
# October 2026; models without a row show no estimate.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

MAX_SCAN_BUFFER = 8 * 1024 * 1024


def record(
    ctx: Any, *, user_id: str, agent_id: str, provider: str, model: str,
    input_tokens: int, cached_tokens: int, output_tokens: int,
) -> None:
    day = utcnow().date().isoformat()
    try:
        ctx.db.execute(
            "INSERT INTO usage_daily (user_id, agent_id, day, provider, model, requests, "
            "input_tokens, cached_input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT (user_id, agent_id, day, provider, model) DO UPDATE SET "
            "requests = requests + 1, input_tokens = input_tokens + excluded.input_tokens, "
            "cached_input_tokens = cached_input_tokens + excluded.cached_input_tokens, "
            "output_tokens = output_tokens + excluded.output_tokens",
            (user_id, agent_id, day, provider, model, int(input_tokens), int(cached_tokens),
             int(output_tokens)),
        )
    except Exception:
        # Usage is informational; never let it break a model reply.
        logger.exception("could not record usage for agent %s", agent_id)


def estimate_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    full_input = max(0, input_tokens - cached_tokens)
    return (full_input * price[0] + output_tokens * price[1] + cached_tokens * price[2]) / 1_000_000


def summary(ctx: Any, user_id: str, days: int = 30) -> dict[str, Any]:
    since = (utcnow() - timedelta(days=days - 1)).date().isoformat()
    rows = ctx.db.all(
        "SELECT provider, model, SUM(requests) AS requests, SUM(input_tokens) AS input_tokens, "
        "SUM(cached_input_tokens) AS cached_input_tokens, SUM(output_tokens) AS output_tokens "
        "FROM usage_daily WHERE user_id = ? AND day >= ? GROUP BY provider, model "
        "ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC",
        (user_id, since),
    )
    daily = ctx.db.all(
        "SELECT day, SUM(requests) AS requests, SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens FROM usage_daily WHERE user_id = ? AND day >= ? "
        "GROUP BY day ORDER BY day",
        (user_id, since),
    )
    total_cost = 0.0
    any_cost = False
    for row in rows:
        cost = estimate_usd(row["model"], row["input_tokens"], row["cached_input_tokens"],
                            row["output_tokens"])
        row["estimated_usd"] = round(cost, 4) if cost is not None else None
        if cost is not None:
            total_cost += cost
            any_cost = True
    return {
        "days": days,
        "since": since,
        "by_model": rows,
        "daily": daily,
        "estimated_usd": round(total_cost, 4) if any_cost else None,
    }


class ResponsesUsageScanner:
    """Finds the usage in a passed-through Responses API event stream."""

    def __init__(self) -> None:
        self._buffer = b""
        self.found = False
        self.input_tokens = self.cached_tokens = self.output_tokens = 0

    def feed(self, chunk: bytes) -> None:
        if self.found:
            return
        self._buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            if b"response.completed" in event:
                self._parse(event)
                if self.found:
                    self._buffer = b""
                    return
        if len(self._buffer) > MAX_SCAN_BUFFER:
            self._buffer = b""  # an absurd event; give up on this one

    def _parse(self, event: bytes) -> None:
        data = b"\n".join(
            line[5:].lstrip() for line in event.split(b"\n") if line.startswith(b"data:")
        )
        try:
            payload = json.loads(data)
        except ValueError:
            return
        if not isinstance(payload, dict) or payload.get("type") != "response.completed":
            return
        usage = (payload.get("response") or {}).get("usage") or {}
        self.found = True
        self.input_tokens = int(usage.get("input_tokens") or 0)
        self.cached_tokens = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        self.output_tokens = int(usage.get("output_tokens") or 0)
