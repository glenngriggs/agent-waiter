"""A scripted fake of the OpenAI Responses API.

Codex talks to it exactly as it would to a real provider (through Den's model
proxy in the tests), so the whole chain can run with the real app-server and
no credentials. What it answers is chosen from the text of the last real user
message of the request:

- ``SCHEDULE:<n>``      calls ``tools.mcp__platform__schedule_job`` from the
                        ``exec`` tool, then answers "Scheduled it."
- ``RUN:<command>``     calls ``tools.exec_command`` from ``exec``, then "Ran it."
- ``SLOW``              streams one delta every 0.5 s for 60 s (Stop tests)
- ``FAIL401``           HTTP 401 with an OpenAI-style error body
- the probe prompt      "ready"
- anything else         "Echo: <first 200 chars of the user text>"

A request for a model it does not know gets a 404 ``model_not_found``, which is
how tests make a model fail during onboarding.

Run it by hand for free local trials:
    python -m tests.fake_model --port 9100
    DEN_UPSTREAM_BASE_URL=http://127.0.0.1:9100/v1 DEN_UPSTREAM_API_KEY=fake python -m den
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import re
import socket
import threading
import time
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

KNOWN_MODELS = ("gpt-6.1-sol", "gpt-6-luna")
PROBE_PHRASE = "reply with the single word: ready"

_SCHEDULE = re.compile(r"SCHEDULE:(\d+)")
_RUN = re.compile(r"RUN:(.+)", re.DOTALL)
_TOOL_OUTPUT_TYPES = {"custom_tool_call_output", "function_call_output"}


def free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for a port nobody is listening on (racy, but fine for tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------
# Reading the request
# --------------------------------------------------------------------------


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") in ("input_text", "text", "output_text"):
            parts.append(part.get("text") or "")
    return "".join(parts)


def _is_injected_context(text: str) -> bool:
    # Codex adds role "user" messages of its own (environment context, AGENTS.md
    # instructions, aborted-turn markers). They are wrapped in a single XML-ish
    # tag, unlike anything the tests send, so they can be told apart.
    stripped = text.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def last_user_message(items: list[Any]) -> tuple[int, str]:
    """Return (index, text) of the last real user message, or (-1, "")."""
    fallback: tuple[int, str] = (-1, "")
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if not isinstance(item, dict):
            continue
        if item.get("type", "message") != "message" or item.get("role") != "user":
            continue
        text = _message_text(item)
        if not _is_injected_context(text):
            return index, text
        if fallback[0] == -1:
            fallback = (index, text)
    return fallback


def tool_output_after(items: list[Any], index: int) -> bool:
    """True when this turn already got the result of the scripted tool call."""
    return any(
        isinstance(item, dict) and item.get("type") in _TOOL_OUTPUT_TYPES
        for item in items[index + 1 :]
    )


# --------------------------------------------------------------------------
# Building the SSE stream
# --------------------------------------------------------------------------


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def _created(response_id: str) -> dict[str, Any]:
    return {"type": "response.created", "response": {"id": response_id}}


def _completed(response_id: str, output_tokens: int) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": {
                "input_tokens": 10,
                "output_tokens": output_tokens,
                "total_tokens": 10 + output_tokens,
            },
        },
    }


def split_deltas(text: str) -> list[str]:
    """Split text into word-sized deltas whose concatenation is the text."""
    pieces = re.findall(r"\s*\S+\s*", text)
    return pieces or [text]


async def message_stream(
    response_id: str, item_id: str, deltas: list[str], delay: float
) -> AsyncIterator[str]:
    text = "".join(deltas)
    yield _sse(_created(response_id))
    yield _sse(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "role": "assistant", "id": item_id, "content": []},
        }
    )
    for delta in deltas:
        if delay:
            await asyncio.sleep(delay)
        yield _sse(
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "item_id": item_id,
                "delta": delta,
            }
        )
    yield _sse(
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "message",
                "role": "assistant",
                "id": item_id,
                "content": [{"type": "output_text", "text": text}],
            },
        }
    )
    yield _sse(_completed(response_id, len(deltas)))


async def tool_call_stream(response_id: str, n: int, javascript: str) -> AsyncIterator[str]:
    # Codex 0.163 exposes MCP and shell tools as nested tools of its freeform
    # `exec` tool, so a tool call is a custom_tool_call carrying JavaScript.
    item = {
        "type": "custom_tool_call",
        "id": f"ctc_{n}",
        "call_id": f"call_{n}",
        "name": "exec",
        "input": javascript,
    }
    yield _sse(_created(response_id))
    yield _sse({"type": "response.output_item.added", "output_index": 0, "item": item})
    yield _sse({"type": "response.output_item.done", "output_index": 0, "item": item})
    yield _sse(_completed(response_id, 1))


def schedule_js(minutes: int) -> str:
    args = {"name": "follow-up", "prompt": "Check on the thing", "in_minutes": minutes}
    return (
        f"const r = await tools.mcp__platform__schedule_job({json.dumps(args)});\n"
        "text(JSON.stringify(r));"
    )


def run_js(command: str) -> str:
    return (
        f"const r = await tools.exec_command({json.dumps({'cmd': command})});\n"
        "text(JSON.stringify(r));"
    )


def _error(status: int, message: str, code: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": kind, "param": None, "code": code}},
        status_code=status,
    )


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


def build_fake_app(
    *,
    known_models: tuple[str, ...] = KNOWN_MODELS,
    delta_delay: float = 0.02,
    slow_interval: float = 0.5,
    slow_seconds: float = 60.0,
) -> FastAPI:
    """Build the fake. Every request is recorded in ``app.state.requests``."""
    app = FastAPI(title="Den fake model")
    app.state.requests = []
    counter = itertools.count(1)

    async def record(request: Request) -> dict[str, Any]:
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        entry = {
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": body,
            "raw": raw.decode("utf-8", errors="replace"),
            "at": time.time(),
        }
        app.state.requests.append(entry)  # list.append is atomic under the GIL
        return entry

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        await record(request)
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "den-fake"}
                for m in known_models
            ],
        }

    @app.post("/v1/responses")
    async def responses(request: Request):
        entry = await record(request)
        body = entry["body"] or {}
        model = body.get("model")
        if model not in known_models:
            return _error(
                404,
                f"The model `{model}` does not exist or you do not have access to it.",
                "model_not_found",
            )

        items = body.get("input") or []
        if isinstance(items, str):  # plain-string input is allowed by the API
            items = [{"type": "message", "role": "user", "content": items}]
        index, user_text = last_user_message(items)
        has_tool_output = tool_output_after(items, index)

        n = next(counter)
        response_id, item_id = f"resp_{n}", f"msg_{n}"

        def reply(text: str, delay: float = delta_delay) -> StreamingResponse:
            return StreamingResponse(
                message_stream(response_id, item_id, split_deltas(text), delay),
                media_type="text/event-stream",
            )

        if "FAIL401" in user_text:
            return _error(401, "Incorrect API key provided.", "invalid_api_key")

        schedule = _SCHEDULE.search(user_text)
        run = _RUN.search(user_text)
        if schedule:
            if has_tool_output:
                return reply("Scheduled it.")
            return StreamingResponse(
                tool_call_stream(response_id, n, schedule_js(int(schedule.group(1)))),
                media_type="text/event-stream",
            )
        if run:
            if has_tool_output:
                return reply("Ran it.")
            return StreamingResponse(
                tool_call_stream(response_id, n, run_js(run.group(1).strip())),
                media_type="text/event-stream",
            )
        if "SLOW" in user_text:
            ticks = max(1, int(slow_seconds / slow_interval))
            deltas = [f"tick{i} " for i in range(ticks)]
            return StreamingResponse(
                message_stream(response_id, item_id, deltas, slow_interval),
                media_type="text/event-stream",
            )
        if PROBE_PHRASE in user_text.lower():
            return reply("ready")
        return reply("Echo: " + user_text[:200])

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def anything_else(path: str, request: Request) -> JSONResponse:
        await record(request)
        return _error(404, f"Unknown path /v1/{path}", "not_found")

    return app


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


class FakeModelServer:
    """Handle for a fake model running in a background thread."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        config = uvicorn.Config(
            app, host=host, port=port, log_level="warning", lifespan="off",
            timeout_graceful_shutdown=2,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name=f"fake-model-{port}", daemon=True
        )

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.app.state.requests

    def requests_containing(self, marker: str, path: str = "/v1/responses") -> list[dict[str, Any]]:
        """Recorded requests to ``path`` whose raw body contains ``marker``."""
        return [r for r in list(self.requests) if r["path"] == path and marker in r["raw"]]

    def start(self, timeout: float = 15.0) -> "FakeModelServer":
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError(f"fake model failed to start on port {self.port}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"fake model did not start on port {self.port}")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def run_in_thread(port: int = 0, host: str = "127.0.0.1", **app_options: Any) -> FakeModelServer:
    """Start the fake on ``port`` (0 picks a free one) and return its handle."""
    return FakeModelServer(build_fake_app(**app_options), host, port or free_port(host)).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scripted fake of the Responses API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    uvicorn.run(build_fake_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
