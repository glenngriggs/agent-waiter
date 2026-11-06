"""Regression tests for the Claude bridge: refusal fallbacks, plain mode,
prompt caching, mid-stream failures and empty text blocks.

They call `anthropic_bridge.respond` directly (no Codex) against the strict
fake in tests/fake_anthropic.py, or against an in-process mock transport
where a stream has to break in a way a real server cannot script.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Callable

import anthropic
import httpx2
import pytest
from fastapi.responses import StreamingResponse

from den import anthropic_bridge
from tests import fake_anthropic

KEY = fake_anthropic.VALID_KEY
EXEC_TOOL = {"type": "function", "name": "exec_command",
             "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}


@pytest.fixture(autouse=True)
def _fresh_bridge_state(monkeypatch):
    # Cached SDK clients belong to one event loop, and plain mode is global:
    # every test starts with neither.
    monkeypatch.setattr(anthropic_bridge, "_clients", OrderedDict())
    monkeypatch.setattr(anthropic_bridge, "_plain_until", {})


@pytest.fixture(scope="module")
def fake():
    server = fake_anthropic.run_in_thread()
    yield server
    server.stop()


def _user(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _body(items: list[dict[str, Any]], model: str = "claude-opus-5-5", web: bool = True) -> dict:
    tools = [EXEC_TOOL] + ([{"type": "web_search"}] if web else [])
    return {"model": model, "stream": True, "instructions": "Be brief.", "input": items,
            "tools": tools}


async def _respond(body: dict, base_url: str, on_usage: Callable | None = None):
    """(status, error body or None, Responses events) for one bridge call."""
    resp = await anthropic_bridge.respond(body, api_key=KEY, base_url=base_url, agent_id="t",
                                          on_usage=on_usage)
    if not isinstance(resp, StreamingResponse):
        return resp.status_code, json.loads(resp.body), []
    events = []
    async for chunk in resp.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        events += [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    return 200, None, events


def _items(events: list[dict]) -> list[dict]:
    return [e["item"] for e in events if e["type"] == "response.output_item.done"]


def _next_turn(history: list[dict], events: list[dict], text: str) -> list[dict]:
    """What Codex sends next: the history, this reply's items, its tool outputs, a new message."""
    items = _items(events)
    outputs = [{"type": "function_call_output", "call_id": i["call_id"], "output": "ran"}
               for i in items if i["type"] == "function_call"]
    return history + items + outputs + ([_user(text)] if text else [])


def _blob_turns(events: list[dict]) -> list[list[dict]]:
    reasoning = [i for i in _items(events) if i["type"] == "reasoning"]
    assert len(reasoning) == 1
    return anthropic_bridge.decode_blob(reasoning[0])


# --------------------------------------------------------------------------
# Mid-output refusal fallback (#0)
# --------------------------------------------------------------------------


async def test_declined_models_tool_call_is_not_run_or_replayed(fake) -> None:
    history = [_user("DECLINE:nmap -sS 10.0.0.0/8")]
    status, _, events = await _respond(_body(history), fake.url)
    assert status == 200
    items = _items(events)
    # Codex must not run a tool call the declined model made before the seam.
    assert not [i for i in items if i["type"] == "function_call"]
    texts = [c["text"] for i in items if i["type"] == "message" for c in i["content"]]
    assert texts == ["Here is a safer approach."]
    assert events[-1]["type"] == "response.completed"
    # The replay keeps the seam and the text, without the declined thinking or call.
    turn = _blob_turns(events)[0]
    assert [b["type"] for b in turn] == ["fallback", "text"]
    reasoning = next(i for i in items if i["type"] == "reasoning")
    assert reasoning["summary"] == []

    before = len(fake_anthropic.refused(fake))
    status, _, events = await _respond(_body(_next_turn(history, events, "thanks")), fake.url)
    assert status == 200 and events[-1]["type"] == "response.completed"
    assert fake_anthropic.refused(fake)[before:] == []
    sent = fake.requests[-1]["body"]["messages"]
    assert sent[1]["role"] == "assistant" and sent[1]["content"][0]["type"] == "fallback"


def test_old_blobs_with_a_declined_partial_are_filtered_on_replay() -> None:
    # A blob stored before the fix still holds the declined thinking and call,
    # and Codex holds the function_call and its output.
    turn = [
        {"type": "thinking", "thinking": "x", "signature": "s"},
        {"type": "tool_use", "id": "toolu_old", "name": "exec_command", "input": {"cmd": "ls"}},
        {"type": "fallback", "from": {"model": "claude-opus-5-5"},
         "to": {"model": "claude-opus-4-8"}, "trigger": {"type": "refusal"}},
        {"type": "text", "text": "Instead..."},
    ]
    blob = {"type": "reasoning", "summary": [],
            "encrypted_content": anthropic_bridge.encode_blob("claude-opus-5-5", [turn])}
    call = {"type": "function_call", "call_id": "toolu_old", "name": "exec_command",
            "arguments": "{\"cmd\": \"ls\"}"}
    output = {"type": "function_call_output", "call_id": "toolu_old", "output": "ran"}
    params, _ = anthropic_bridge.translate_request(
        _body([_user("hi"), blob, call, output, _user("next")]))
    assert fake_anthropic.check_request(params, {}) is None
    assistant = params["messages"][1]
    assert [b["type"] for b in assistant["content"]] == ["fallback", "text"]
    assert not any(b.get("type") == "tool_result" for m in params["messages"]
                   for b in m["content"] if isinstance(b, dict))


def test_paired_server_tool_blocks_before_the_seam_are_kept() -> None:
    blocks = [
        {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": []},
        {"type": "server_tool_use", "id": "srv_2", "name": "web_search", "input": {}},
        {"type": "text", "text": "partial"},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "after", "signature": "s"},
        {"type": "tool_use", "id": "toolu_after", "name": "exec_command", "input": {}},
    ]
    assert anthropic_bridge._replayable(blocks) == [True, True, False, True, True, True, True]


# --------------------------------------------------------------------------
# Plain mode only for refusals of the extras (#1)
# --------------------------------------------------------------------------


async def test_prompt_too_long_does_not_switch_to_plain_mode(fake) -> None:
    before = len(fake.requests)
    status, error, _ = await _respond(_body([_user("TOOLONG")]), fake.url)
    assert status == 400 and error["error"]["code"] == "context_length_exceeded"
    assert len(fake.requests) - before == 1  # no pointless plain retry
    assert not anthropic_bridge._plain_mode(KEY, "claude-opus-5-5")

    status, _, events = await _respond(_body([_user("hello")]), fake.url)
    assert status == 200 and events[-1]["type"] == "response.completed"
    sent = fake.requests[-1]
    assert sent["headers"].get("anthropic-beta")
    assert sent["body"].get("fallbacks") == "default"
    assert "web_search" in {t["name"] for t in sent["body"]["tools"]}


async def test_refusal_naming_the_extras_retries_plain_and_remembers() -> None:
    strict = fake_anthropic.run_in_thread(reject_extras=True)
    try:
        status, _, events = await _respond(_body([_user("hi there")]), strict.url)
        assert status == 200 and events[-1]["type"] == "response.completed"
        betas = [r["headers"].get("anthropic-beta") for r in strict.requests]
        assert betas[0] and betas[1] is None
        assert anthropic_bridge._plain_mode(KEY, "claude-opus-5-5")
        await _respond(_body([_user("again")]), strict.url)
        assert len(strict.requests) == 3  # plain from the start now
    finally:
        strict.stop()


async def test_unexplained_400_retries_plain_once_without_remembering() -> None:
    strict = fake_anthropic.run_in_thread(reject_extras=True,
                                          reject_message="messages.0: something odd")
    try:
        status, _, events = await _respond(_body([_user("hi there")]), strict.url)
        assert status == 200 and events[-1]["type"] == "response.completed"
        assert len(strict.requests) == 2
        assert not anthropic_bridge._plain_mode(KEY, "claude-opus-5-5")
        await _respond(_body([_user("again")]), strict.url)
        assert strict.requests[2]["headers"].get("anthropic-beta")  # extras tried again
    finally:
        strict.stop()


# --------------------------------------------------------------------------
# Prompt caching (#2)
# --------------------------------------------------------------------------


def test_requests_ask_for_automatic_prompt_caching() -> None:
    body = _body([_user("hi")])
    params, tool_map = anthropic_bridge.translate_request(body)
    assert params["cache_control"] == {"type": "ephemeral"}
    assert anthropic_bridge.translate_request(body)[0] == params  # still deterministic
    extra, _ = anthropic_bridge.add_extras(params, tool_map)
    assert extra["cache_control"] == {"type": "ephemeral"}


async def test_cached_tokens_reach_the_usage_report(fake) -> None:
    usage: list[tuple] = []
    status, _, events = await _respond(_body([_user("hello")]), fake.url,
                                       on_usage=lambda *a: usage.append(a))
    assert status == 200
    assert fake.requests[-1]["body"]["cache_control"] == {"type": "ephemeral"}
    done = events[-1]
    assert done["response"]["usage"]["input_tokens_details"]["cached_tokens"] == 40
    assert usage == [(140, 40, 20)]


# --------------------------------------------------------------------------
# Mid-stream failures end with response.failed (#3)
# --------------------------------------------------------------------------


def _sse(kind: str, data: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()


class _BreakingStream(httpx2.AsyncByteStream):
    """A Messages stream that starts a text block, then fails with `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aiter__(self):
        yield _sse("message_start", {"message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 1}}})
        yield _sse("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
        yield _sse("content_block_delta", {"index": 0, "delta": {"type": "text_delta",
                                                                  "text": "partial"}})
        raise self.error


def _mock_base(error: Exception) -> str:
    base = "http://mock.invalid"

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               stream=_BreakingStream(error))

    client = anthropic.AsyncAnthropic(
        api_key=KEY, base_url=base, max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    anthropic_bridge._clients[(base, anthropic_bridge._key_id(KEY))] = client
    return base


@pytest.mark.parametrize("error, message", [
    (httpx2.RemoteProtocolError("peer closed connection"), "The connection to Anthropic dropped."),
    (ValueError("Unable to parse tool parameter JSON from model"), "Claude's reply could not be read."),
])
async def test_a_broken_stream_ends_with_response_failed(error, message) -> None:
    usage: list[tuple] = []
    base = _mock_base(error)
    status, _, events = await _respond(_body([_user("hi")]), base,
                                       on_usage=lambda *a: usage.append(a))
    assert status == 200
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["message"] == message
    assert usage == [(100, 0, 1)]  # the broken round's tokens still count


# --------------------------------------------------------------------------
# Empty text blocks are not replayed (#4)
# --------------------------------------------------------------------------


async def test_empty_text_block_is_not_replayed(fake) -> None:
    history = [_user("EMPTY:ls")]
    status, _, events = await _respond(_body(history), fake.url)
    assert status == 200
    assert [b["type"] for b in _blob_turns(events)[0]] == ["tool_use"]
    assert [i["type"] for i in _items(events)][-2:] == ["function_call", "reasoning"]

    before = len(fake_anthropic.refused(fake))
    status, _, events = await _respond(_body(_next_turn(history, events, "")), fake.url)
    assert status == 200 and events[-1]["type"] == "response.completed"
    assert fake_anthropic.refused(fake)[before:] == []
    texts = [c["text"] for i in _items(events) if i["type"] == "message" for c in i["content"]]
    assert texts == ["Ran it."]


def test_empty_text_in_a_stored_blob_is_dropped_on_replay() -> None:
    turn = [{"type": "text", "text": ""},
            {"type": "tool_use", "id": "toolu_e", "name": "exec_command", "input": {"cmd": "ls"}}]
    blob = {"type": "reasoning", "summary": [],
            "encrypted_content": anthropic_bridge.encode_blob("claude-opus-5-5", [turn, [
                {"type": "text", "text": ""}]])}
    output = {"type": "function_call_output", "call_id": "toolu_e", "output": "ran"}
    params, _ = anthropic_bridge.translate_request(_body([_user("hi"), blob, output]))
    assert fake_anthropic.check_request(params, {}) is None
    assert params["messages"][1]["content"] == [turn[1]]
