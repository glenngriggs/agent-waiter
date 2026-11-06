"""A scripted, strict fake of the Anthropic Messages API (streaming only).

Den's Claude bridge talks to it through the real `anthropic` SDK. Like
tests/fake_model.py it answers from the last real user text:

- ``RUN:<command>``   a thinking block, then a ``tool_use`` of ``exec_command``,
                      then (once the result is back) "Ran it."
- ``SCHEDULE:<n>``    a ``tool_use`` of ``mcp__platform__schedule_job``, then
                      "Scheduled it."
- ``WEB``             (with the web_search server tool offered) a
                      ``server_tool_use`` + ``web_search_tool_result``, then text
- the probe prompt    "ready"
- anything else       "Echo: <first 200 chars>"

It is strict where the real API is, so translation bugs fail loudly with a
400: the first message must be from the user and the last must not be from
the assistant, every ``tool_use`` must be answered first thing in the next
user message, text blocks may not be empty, and tool names must match the
API's pattern. It also checks exact replay: every assistant message that
carries a thinking block it produced must come back byte-for-byte as it was
sent (that is what keeps real thinking blocks valid).

``reject_extras`` makes it refuse requests that use the optional betas, to
test the bridge's plain-mode retry.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tests.fake_model import PROBE_PHRASE, FakeModelServer, free_port

KNOWN_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")
VALID_KEY = "sk-ant-test-0123456789"
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RUN = re.compile(r"RUN:(.+)", re.DOTALL)
_SCHEDULE = re.compile(r"SCHEDULE:(\d+)")


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}},
                        status_code=status)


def _sse(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]


def check_request(body: dict[str, Any], produced: dict[str, str]) -> str | None:
    """Return why the real API would refuse this request, or None."""
    messages = body.get("messages") or []
    if not messages or messages[0].get("role") != "user":
        return "messages: the first message must use the user role"
    if messages[-1].get("role") == "assistant":
        return "messages: this model does not support assistant message prefill"
    for tool in body.get("tools") or []:
        if "input_schema" in tool and not TOOL_NAME.match(tool.get("name", "")):
            return f"tools: invalid tool name {tool.get('name')!r}"
    pending: set[str] = set()
    for i, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not content:
            return f"messages.{i}: content must not be empty"
        for block in content:
            if block.get("type") == "text" and not block.get("text"):
                return f"messages.{i}: text content blocks must be non-empty"
        if message["role"] == "user":
            results = [b for b in content if b.get("type") == "tool_result"]
            if pending:
                leading = [b.get("tool_use_id") for b in content[: len(pending)]]
                if set(leading) != pending:
                    return f"messages.{i}: tool_use ids without tool_result blocks right after them"
            for block in results:
                if block.get("tool_use_id") not in pending:
                    return f"messages.{i}: unexpected tool_use_id in tool_result"
            pending = set()
        else:
            pending |= {b["id"] for b in content if b.get("type") == "tool_use"}
            signatures = [b.get("signature") for b in content if b.get("type") == "thinking"]
            for signature in signatures:
                original = produced.get(signature)
                if original is not None and original != json.dumps(content, sort_keys=True):
                    return f"messages.{i}: thinking block replayed in an edited message"
    return None


def build_fake_app(*, reject_extras: bool = False, delay: float = 0.0) -> FastAPI:
    app = FastAPI(title="Den fake Anthropic")
    app.state.requests = []
    app.state.produced = {}  # thinking signature -> the exact content it was sent in
    counter = itertools.count(1)

    def authorized(request: Request) -> bool:
        # SDK 2.x sends the key as a bearer token; older clients use x-api-key.
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return VALID_KEY in (request.headers.get("x-api-key"), bearer)

    @app.get("/v1/models")
    async def models(request: Request):
        if not authorized(request):
            return _error(401, "authentication_error", "invalid x-api-key")
        data = [{"id": m, "type": "model", "display_name": m, "created_at": "2026-01-01T00:00:00Z"}
                for m in KNOWN_MODELS]
        return {"data": data, "has_more": False, "first_id": data[0]["id"], "last_id": data[-1]["id"]}

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()
        entry = {"body": body, "headers": dict(request.headers), "at": time.time()}
        app.state.requests.append(entry)
        if not authorized(request):
            return _error(401, "authentication_error", "invalid x-api-key")
        if body.get("model") not in KNOWN_MODELS:
            return _error(404, "not_found_error", f"model: {body.get('model')}")
        betas = request.headers.get("anthropic-beta", "")
        if reject_extras and (betas or "fallbacks" in body):
            return _error(400, "invalid_request_error", "Unexpected beta or fallbacks field")
        if not body.get("stream"):
            return _error(400, "invalid_request_error", "the fake only streams")
        problem = check_request(body, app.state.produced)
        if problem:
            entry["refused"] = problem
            return _error(400, "invalid_request_error", problem)

        n = next(counter)
        messages = body["messages"]
        last = messages[-1]
        has_result = any(b.get("type") == "tool_result" for b in last["content"]) \
            if isinstance(last["content"], list) else False
        user_text = ""
        for message in reversed(messages):
            if message["role"] != "user":
                continue
            texts = [t for t in _texts(message["content"]) if not t.strip().startswith("<")]
            if texts:
                user_text = texts[-1]
                break
        tool_names = {t.get("name") for t in body.get("tools") or []}

        blocks: list[dict[str, Any]] = []
        stop = "end_turn"
        run, schedule = _RUN.search(user_text), _SCHEDULE.search(user_text)
        if run and not has_result:
            blocks = [
                {"type": "thinking", "thinking": "I should run the command.", "signature": f"sig{n}"},
                {"type": "text", "text": "Running it."},
                {"type": "tool_use", "id": f"toolu_{n}", "name": "exec_command",
                 "input": {"cmd": run.group(1).strip()}},
            ]
            stop = "tool_use"
        elif schedule and not has_result and "mcp__platform__schedule_job" in tool_names:
            blocks = [{"type": "tool_use", "id": f"toolu_{n}", "name": "mcp__platform__schedule_job",
                       "input": {"name": "follow-up", "prompt": "Check on the thing",
                                 "in_minutes": int(schedule.group(1))}}]
            stop = "tool_use"
        elif run:
            blocks = [{"type": "text", "text": "Ran it."}]
        elif schedule:
            blocks = [{"type": "text", "text": "Scheduled it."}]
        elif "WEB" in user_text and "web_search" in tool_names:
            blocks = [
                {"type": "server_tool_use", "id": f"srvtoolu_{n}", "name": "web_search",
                 "input": {"query": "den platform"}},
                {"type": "web_search_tool_result", "tool_use_id": f"srvtoolu_{n}", "content": []},
                {"type": "text", "text": "Searched."},
            ]
        elif PROBE_PHRASE in user_text.lower():
            blocks = [{"type": "text", "text": "ready"}]
        else:
            blocks = [{"type": "thinking", "thinking": "Echo it.", "signature": f"sig{n}"},
                      {"type": "text", "text": "Echo: " + user_text[:200]}]
        for block in blocks:
            if block["type"] == "thinking":
                app.state.produced[block["signature"]] = json.dumps(blocks, sort_keys=True)

        async def stream() -> AsyncIterator[str]:
            yield _sse("message_start", {"message": {
                "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body["model"],
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 40,
                          "cache_creation_input_tokens": 0}}})
            for index, block in enumerate(blocks):
                if delay:
                    await asyncio.sleep(delay)
                kind = block["type"]
                if kind == "thinking":
                    yield _sse("content_block_start", {"index": index, "content_block": {
                        "type": "thinking", "thinking": "", "signature": ""}})
                    yield _sse("content_block_delta", {"index": index, "delta": {
                        "type": "thinking_delta", "thinking": block["thinking"]}})
                    yield _sse("content_block_delta", {"index": index, "delta": {
                        "type": "signature_delta", "signature": block["signature"]}})
                elif kind == "text":
                    yield _sse("content_block_start", {"index": index, "content_block": {
                        "type": "text", "text": ""}})
                    for word in re.findall(r"\s*\S+", block["text"]):
                        yield _sse("content_block_delta", {"index": index, "delta": {
                            "type": "text_delta", "text": word}})
                elif kind in ("tool_use", "server_tool_use"):
                    yield _sse("content_block_start", {"index": index, "content_block": {
                        "type": kind, "id": block["id"], "name": block["name"], "input": {}}})
                    yield _sse("content_block_delta", {"index": index, "delta": {
                        "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
                else:
                    yield _sse("content_block_start", {"index": index, "content_block": block})
                yield _sse("content_block_stop", {"index": index})
            yield _sse("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None},
                                         "usage": {"output_tokens": 20}})
            yield _sse("message_stop", {})

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def run_in_thread(port: int = 0, host: str = "127.0.0.1", **options: Any) -> FakeModelServer:
    """Start the fake on a free port; the handle's `requests` lists every call."""
    return FakeModelServer(build_fake_app(**options), host, port or free_port(host)).start()


def refused(server: FakeModelServer) -> list[str]:
    return [r["refused"] for r in server.requests if "refused" in r]


__all__ = ["run_in_thread", "VALID_KEY", "KNOWN_MODELS", "refused"]


def main() -> None:
    """Run by hand for UI work: python -m tests.fake_anthropic --port 9200
    then DEN_ANTHROPIC_BASE_URL=http://127.0.0.1:9200 and use the key VALID_KEY."""
    import argparse

    parser = argparse.ArgumentParser(description="Strict fake of the Anthropic Messages API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9200)
    args = parser.parse_args()
    uvicorn.run(build_fake_app(delay=0.05), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
