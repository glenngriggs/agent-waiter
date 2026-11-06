"""Run Claude models under Codex: Responses API in, Anthropic Messages API out.

Codex speaks only the OpenAI Responses API. For an agent on the Anthropic
provider the model proxy hands the Responses request to this module, which
translates it into a Messages API call (through the official `anthropic`
SDK, with the user's own key) and streams the answer back as Responses
server-sent events that Codex understands.

The translation keeps three promises:

1. Deterministic. The same Responses input always becomes the same Messages
   request, so Claude's prompt cache keeps hitting across a conversation.
2. Exact replay. Claude's thinking blocks are only valid if the history they
   were produced in comes back unchanged. Codex keeps the history, but in
   its own item format. So every Claude reply also travels inside one opaque
   `reasoning` item (Codex stores and replays reasoning items untouched) that
   holds the reply's raw content blocks. When that item comes back, the
   reply is rebuilt from it exactly, instead of from Codex's text and tool
   call items. The request also asks the API to drop, rather than reject,
   thinking blocks whose history did change (Codex compaction can rewrite
   it).
3. Graceful. The optional extras (thinking-block binding, refusal fallbacks,
   web search) are betas or org settings that an account may not have. If
   the first attempt is refused with a 400, the request is sent once more
   without them, and that model and key stay in plain mode for an hour.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

import anthropic
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("den.anthropic")

BLOB_PREFIX = "den.anthropic.v1:"
MAX_TOKENS = 64000
PLAIN_MODE_SECONDS = 3600
MAX_PAUSE_CONTINUATIONS = 4
MAX_CLIENTS = 32
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Betas for the optional extras (see the module docstring, point 3).
BLOCK_BINDING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Models with adaptive thinking and `effort`. Older ones get neither.
_ADAPTIVE_PREFIXES = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5",
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6",
)
# Models whose thinking blocks are bound to the conversation (preserved thinking).
_BOUND_THINKING = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5",
                   "claude-sonnet-5-5", "claude-haiku-5-5")
# Models that take the server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
# Models with the dynamic-filtering web search tool; other models get the basic one.
_NEW_WEB_SEARCH = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-opus-4-6",
                   "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6")

_EFFORTS = {"minimal": "low", "none": "low", "low": "low", "medium": "medium",
            "high": "high", "xhigh": "xhigh", "max": "max"}


# --------------------------------------------------------------------------
# Clients and plain mode
# --------------------------------------------------------------------------

_clients: "OrderedDict[tuple[str, str], anthropic.AsyncAnthropic]" = OrderedDict()
_plain_until: dict[tuple[str, str], float] = {}


def _key_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def get_client(api_key: str, base_url: str) -> anthropic.AsyncAnthropic:
    """A cached SDK client per (base URL, key). Codex retries on its own."""
    cache_key = (base_url, _key_id(api_key))
    client = _clients.get(cache_key)
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=1)
        _clients[cache_key] = client
        while len(_clients) > MAX_CLIENTS:
            _clients.popitem(last=False)  # dropped clients close when collected
    else:
        _clients.move_to_end(cache_key)
    return client


async def close_clients() -> None:
    while _clients:
        _, client = _clients.popitem()
        try:
            await client.close()
        except Exception:  # shutdown cleanup must not raise
            logger.debug("closing an Anthropic client failed", exc_info=True)


def _plain_mode(api_key: str, model: str) -> bool:
    until = _plain_until.get((_key_id(api_key), model))
    return until is not None and until > time.monotonic()


def _set_plain_mode(api_key: str, model: str) -> None:
    _plain_until[(_key_id(api_key), model)] = time.monotonic() + PLAIN_MODE_SECONDS


# --------------------------------------------------------------------------
# Request translation: Responses -> Messages
# --------------------------------------------------------------------------


@dataclass
class ToolMap:
    """Anthropic tool name -> how to report a call back to Codex."""

    by_name: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (namespace or "", Codex name) -> Anthropic name, for replaying history.
    reverse: dict[tuple[str, str], str] = field(default_factory=dict)
    web_search: bool = False


def _safe_tool_name(raw: str, taken: set[str]) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64] or "tool"
    if name in taken or not TOOL_NAME_RE.match(name):
        suffix = hashlib.sha256(raw.encode()).hexdigest()[:6]
        name = f"{name[:57]}_{suffix}"
    return name


def _schema(parameters: Any) -> dict[str, Any]:
    if isinstance(parameters, dict) and parameters.get("type") == "object":
        return parameters
    return {"type": "object", "properties": {}}


def translate_tools(tools: list[Any], model: str) -> tuple[list[dict[str, Any]], ToolMap]:
    out: list[dict[str, Any]] = []
    tool_map = ToolMap()
    taken: set[str] = set()

    def add_function(fn: dict[str, Any], namespace: str | None) -> None:
        codex_name = fn.get("name") or ""
        raw = f"{namespace}__{codex_name}" if namespace else codex_name
        name = _safe_tool_name(raw, taken)
        taken.add(name)
        tool: dict[str, Any] = {"name": name, "input_schema": _schema(fn.get("parameters"))}
        if fn.get("description"):
            tool["description"] = fn["description"]
        out.append(tool)
        tool_map.by_name[name] = {"kind": "function", "name": codex_name, "namespace": namespace}
        tool_map.reverse[(namespace or "", codex_name)] = name

    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if kind == "function":
            add_function(tool, None)
        elif kind == "namespace":
            for inner in tool.get("tools") or []:
                if isinstance(inner, dict) and inner.get("type") == "function":
                    add_function(inner, tool.get("name"))
        elif kind == "custom":
            # A freeform tool takes raw text; Claude gets it as one string field.
            codex_name = tool.get("name") or ""
            name = _safe_tool_name(codex_name, taken)
            taken.add(name)
            description = tool.get("description") or ""
            fmt = tool.get("format") or {}
            if fmt.get("definition"):
                description += f"\n\nThe input must follow this {fmt.get('syntax', '')} grammar:\n"
                description += str(fmt["definition"])[:4000]
            out.append({
                "name": name,
                "description": description.strip() or codex_name,
                "input_schema": {
                    "type": "object",
                    "properties": {"input": {"type": "string", "description": "The raw tool input."}},
                    "required": ["input"],
                },
            })
            tool_map.by_name[name] = {"kind": "custom", "name": codex_name, "namespace": None}
            tool_map.reverse[("", codex_name)] = name
        elif kind == "web_search":
            tool_map.web_search = True
        # Other hosted tools (file search, image generation…) have no Claude
        # equivalent and are left out.
    return out, tool_map


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text"):
            parts.append(part.get("text") or "")
    return "".join(parts)


_DATA_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)


def _image_block(url: str | None) -> dict[str, Any] | None:
    if not url:
        return None
    match = _DATA_URL.match(url)
    if match:
        return {"type": "image", "source": {"type": "base64", "media_type": match.group(1),
                                             "data": match.group(2)}}
    if url.startswith(("https://", "http://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    """Responses message/tool-output content -> Anthropic text and image blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
        elif kind == "input_image":
            image = _image_block(part.get("image_url"))
            if image:
                blocks.append(image)
        elif kind == "input_file":
            blocks.append({"type": "text", "text": f"[Attached file: {part.get('filename') or 'file'}]"})
    return blocks


def encode_blob(model: str, turns: list[list[dict[str, Any]]]) -> str:
    raw = json.dumps({"model": model, "turns": turns}, separators=(",", ":"), ensure_ascii=False)
    return BLOB_PREFIX + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def decode_blob(item: dict[str, Any]) -> list[list[dict[str, Any]]] | None:
    data = item.get("encrypted_content")
    if not isinstance(data, str) or not data.startswith(BLOB_PREFIX):
        return None
    try:
        turns = json.loads(base64.b64decode(data[len(BLOB_PREFIX):]))["turns"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(turns, list) or not all(isinstance(t, list) for t in turns):
        return None
    return turns


def is_bridge_reasoning(item: Any) -> bool:
    """True for reasoning items this module made (OpenAI cannot read them)."""
    return (
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and str(item.get("encrypted_content") or "").startswith(BLOB_PREFIX)
    )


_ASSISTANT_SIDE = {"reasoning", "function_call", "custom_tool_call", "local_shell_call",
                   "web_search_call", "tool_search_call", "image_generation_call"}
_TOOL_OUTPUTS = {"function_call_output", "custom_tool_call_output", "local_shell_call_output",
                 "tool_search_output"}


def _side(item: dict[str, Any], leading: bool) -> str:
    kind = item.get("type", "message")
    if kind == "message":
        role = item.get("role")
        if role == "assistant":
            return "assistant"
        if role in ("developer", "system"):
            return "system" if leading else "user"
        return "user"
    if kind in _ASSISTANT_SIDE:
        return "assistant"
    if kind in _TOOL_OUTPUTS:
        return "user"
    return "skip"


def _tool_output_blocks(output: Any) -> list[dict[str, Any]] | str:
    if isinstance(output, str):
        return output or "(no output)"
    blocks = _content_blocks(output)
    return blocks or "(no output)"


def _assistant_messages(
    run: list[dict[str, Any]], tool_map: ToolMap
) -> tuple[list[dict[str, Any]], set[str]]:
    """One run of assistant-side items -> (assistant messages, call ids kept as text)."""
    messages: list[dict[str, Any]] = []
    text_calls: set[str] = set()
    covered: set[str] = set()
    has_blob = False
    for item in run:
        if item.get("type") == "reasoning":
            turns = decode_blob(item)
            if turns is None:
                continue  # OpenAI's encrypted reasoning, or a summary: unusable here
            has_blob = True
            for turn in turns:
                messages.append({"role": "assistant", "content": turn})
                covered.update(b.get("id") for b in turn if b.get("type") == "tool_use")
    content: list[dict[str, Any]] = []
    for item in run:
        kind = item.get("type", "message")
        if kind == "message":
            if has_blob:
                continue  # the blob already holds this reply's text, exactly
            content += [{"type": "text", "text": b["text"]}
                        for b in _content_blocks(item.get("content")) if b["type"] == "text"]
        elif kind in ("function_call", "custom_tool_call"):
            call_id = item.get("call_id") or ""
            if call_id in covered:
                continue
            key = (item.get("namespace") or "", item.get("name") or "")
            name = tool_map.reverse.get(key)
            if kind == "function_call":
                try:
                    tool_input = json.loads(item.get("arguments") or "{}")
                except ValueError:
                    tool_input = {"arguments": item.get("arguments")}
                if not isinstance(tool_input, dict):
                    tool_input = {"value": tool_input}
            else:
                tool_input = {"input": item.get("input") or ""}
            if name is None:
                # A call to a tool this model is not offered (made by another
                # model earlier in the conversation): keep it as plain text.
                label = f"{key[0]}.{key[1]}" if key[0] else key[1]
                content.append({"type": "text", "text": f"[Called tool {label}: "
                                f"{json.dumps(tool_input, ensure_ascii=False)[:4000]}]"})
                text_calls.add(call_id)
                continue
            content.append({"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
    if content:
        messages.append({"role": "assistant", "content": content})
    return messages, text_calls


def _user_message(run: list[dict[str, Any]], text_calls: set[str]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for item in run:
        kind = item.get("type", "message")
        if kind in _TOOL_OUTPUTS:
            call_id = item.get("call_id") or ""
            output = _tool_output_blocks(item.get("output"))
            if call_id in text_calls:
                text = output if isinstance(output, str) else _text_of(
                    [{"type": "text", "text": b.get("text", "")} for b in output])
                other.append({"type": "text", "text": f"[Tool result: {text[:8000]}]"})
            else:
                results.append({"type": "tool_result", "tool_use_id": call_id, "content": output})
        elif kind == "message":
            blocks = _content_blocks(item.get("content"))
            if item.get("role") in ("developer", "system"):
                text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
                if text:
                    other.append({"type": "text",
                                  "text": f"<developer_instructions>\n{text}\n</developer_instructions>"})
            else:
                other += blocks
    return {"role": "user", "content": results + other}


def _result_text(block: dict[str, Any]) -> str:
    body = block.get("content")
    if isinstance(body, str):
        return body
    return " ".join(b.get("text", "") for b in body or [] if isinstance(b, dict))


def _pair_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Answer every tool_use in the very next user message, as the API requires.

    A missing result (an interrupted turn) gets a placeholder error result; a
    result whose call is not right before it becomes plain text.
    """
    out: list[dict[str, Any]] = []
    pending: list[str] = []
    for message in messages:
        if message["role"] == "assistant":
            out.append(message)
            pending += [b["id"] for b in message["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_use"]
            continue
        results, stray, other = [], [], []
        answered: set[str] = set()
        for block in message["content"]:
            if block.get("type") != "tool_result":
                other.append(block)
            elif block.get("tool_use_id") in pending and block["tool_use_id"] not in answered:
                answered.add(block["tool_use_id"])
                results.append(block)
            else:
                stray.append({"type": "text",
                              "text": f"[Earlier tool result: {_result_text(block)[:8000]}]"})
        missing = [{"type": "tool_result", "tool_use_id": t, "is_error": True,
                    "content": "The tool call did not return a result."}
                   for t in pending if t not in answered]
        out.append({"role": "user", "content": results + missing + stray + other})
        pending = []
    if pending:
        out.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": t, "is_error": True,
             "content": "The tool call did not return a result."} for t in pending]})
    return out


def translate_request(body: dict[str, Any]) -> tuple[dict[str, Any], ToolMap]:
    """A Responses request body -> (Messages API params, tool map)."""
    model = str(body.get("model") or "")
    tools, tool_map = translate_tools(body.get("tools") or [], model)

    items = body.get("input") or []
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    system_parts: list[str] = []
    if body.get("instructions"):
        system_parts.append(str(body["instructions"]))

    # Group consecutive items by side, then convert each group.
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    leading = True
    for item in items:
        if not isinstance(item, dict):
            continue
        side = _side(item, leading)
        if side == "skip":
            continue
        if side == "system":
            text = _text_of(item.get("content"))
            if text:
                system_parts.append(text)
            continue
        leading = False
        if groups and groups[-1][0] == side:
            groups[-1][1].append(item)
        else:
            groups.append((side, [item]))

    messages: list[dict[str, Any]] = []
    text_calls: set[str] = set()
    for side, run in groups:
        if side == "assistant":
            converted, as_text = _assistant_messages(run, tool_map)
            messages += converted
            text_calls |= as_text  # their outputs are answered with text too
        else:
            message = _user_message(run, text_calls)
            if message["content"]:
                messages.append(message)

    messages = [m for m in messages if m["content"]]
    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "(The conversation so far follows.)"}]})
    messages = _pair_tool_results(messages)
    if messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})

    params: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages}
    system = "\n\n".join(p for p in system_parts if p)
    if system:
        params["system"] = system
    if tools:
        params["tools"] = tools
        choice: dict[str, Any] = {"type": "none" if body.get("tool_choice") == "none" else "auto"}
        if body.get("parallel_tool_calls") is False and choice["type"] == "auto":
            choice["disable_parallel_tool_use"] = True
        params["tool_choice"] = choice
    if model.startswith(_ADAPTIVE_PREFIXES):
        params["thinking"] = {"type": "adaptive", "display": "summarized"}
        effort = ((body.get("reasoning") or {}).get("effort") or "medium")
        params["output_config"] = {"effort": _EFFORTS.get(str(effort), "medium")}
    return params, tool_map


def add_extras(params: dict[str, Any], tool_map: ToolMap) -> tuple[dict[str, Any], list[str]]:
    """The optional extras on top of the plain request: (params, betas)."""
    params = dict(params)
    betas: list[str] = []
    model = params["model"]
    if model.startswith(_BOUND_THINKING) and "thinking" in params:
        params["thinking"] = {**params["thinking"],
                              "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        betas.append(BLOCK_BINDING_BETA)
    if model.startswith(_FALLBACK_MODELS):
        params["fallbacks"] = "default"
        betas.append(FALLBACK_BETA)
    if tool_map.web_search:
        kind = "web_search_20260209" if model.startswith(_NEW_WEB_SEARCH) else "web_search_20250305"
        params["tools"] = list(params.get("tools") or []) + [
            {"type": kind, "name": "web_search", "max_uses": 5}]
        params.setdefault("tool_choice", {"type": "auto"})
    return params, betas


# --------------------------------------------------------------------------
# Response translation: Messages stream -> Responses SSE
# --------------------------------------------------------------------------


def _sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def _openai_error(status: int, message: str, code: str | None = None,
                  headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status_code=status, headers=headers,
    )


def _error_response(exc: anthropic.APIStatusError) -> JSONResponse:
    status = exc.status_code
    message = _api_message(exc)
    headers = {}
    if status == 429 and exc.response.headers.get("retry-after"):
        headers["retry-after"] = exc.response.headers["retry-after"]
    if status == 401:
        return _openai_error(401, "Anthropic rejected the API key.", "invalid_api_key")
    if status == 403:
        return _openai_error(403, f"Anthropic refused the request: {message}", "permission_denied")
    if status == 404:
        return _openai_error(404, f"Anthropic does not offer this model to your key: {message}",
                             "model_not_found")
    if status == 400 and "prompt is too long" in message.lower():
        return _openai_error(400, message, "context_length_exceeded")
    if status == 529:
        return _openai_error(503, "Anthropic is overloaded right now.", "server_is_overloaded")
    return _openai_error(status if 400 <= status < 600 else 502, message, None, headers)


def _api_message(exc: anthropic.APIStatusError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    return str(error.get("message") or exc.message or "error")[:500]


@dataclass
class _Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        created = getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.input_tokens += (getattr(usage, "input_tokens", 0) or 0) + cached + created
        self.cached_tokens += cached
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0


UsageCallback = Callable[[int, int, int], Awaitable[None] | None]


class _Translator:
    """Turns one Claude reply (possibly several pause_turn rounds) into SSE."""

    def __init__(self, model: str, tool_map: ToolMap, response_id: str) -> None:
        self.model = model
        self.tool_map = tool_map
        self.response_id = response_id
        self.output_index = 0
        self.turns: list[list[dict[str, Any]]] = []
        self.summaries: list[str] = []
        self.wrote_text = False
        self.usage = _Usage()
        self._open_text: dict[int, str] = {}  # block index -> item id
        self._counter = 0

    def _item_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self.response_id[5:]}_{self._counter}"

    def _done(self, item: dict[str, Any]) -> list[bytes]:
        index = self.output_index
        self.output_index += 1
        return [
            _sse({"type": "response.output_item.added", "output_index": index, "item": item}),
            _sse({"type": "response.output_item.done", "output_index": index, "item": item}),
        ]

    def event(self, event: Any) -> list[bytes]:
        kind = getattr(event, "type", None)
        out: list[bytes] = []
        if kind == "content_block_start":
            block = event.content_block
            if block.type == "text":
                item_id = self._item_id("msg")
                self._open_text[event.index] = item_id
                out.append(_sse({
                    "type": "response.output_item.added", "output_index": self.output_index,
                    "item": {"type": "message", "role": "assistant", "id": item_id,
                             "status": "in_progress", "content": []},
                }))
        elif kind == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta" and event.index in self._open_text:
                out.append(_sse({
                    "type": "response.output_text.delta", "item_id": self._open_text[event.index],
                    "output_index": self.output_index, "content_index": 0, "delta": delta.text,
                }))
        elif kind == "content_block_stop":
            block = event.content_block
            if block.type == "text" and event.index in self._open_text:
                item_id = self._open_text.pop(event.index)
                item = {"type": "message", "role": "assistant", "id": item_id, "status": "completed",
                        "content": [{"type": "output_text", "text": block.text, "annotations": []}]}
                out.append(_sse({"type": "response.output_item.done",
                                 "output_index": self.output_index, "item": item}))
                self.output_index += 1
                self.wrote_text = self.wrote_text or bool(block.text)
            elif block.type == "tool_use":
                out += self._done(self._tool_call(block))
            elif block.type == "server_tool_use" and block.name == "web_search":
                query = (block.input or {}).get("query") if isinstance(block.input, dict) else None
                out += self._done({"type": "web_search_call", "id": self._item_id("ws"),
                                   "status": "completed",
                                   "action": {"type": "search", "query": str(query or "")}})
            elif block.type == "thinking" and getattr(block, "thinking", ""):
                self.summaries.append(block.thinking)
        return out

    def _tool_call(self, block: Any) -> dict[str, Any]:
        info = self.tool_map.by_name.get(block.name, {"kind": "function", "name": block.name,
                                                       "namespace": None})
        tool_input = block.input if isinstance(block.input, dict) else {}
        if info["kind"] == "custom":
            return {"type": "custom_tool_call", "id": self._item_id("ctc"), "call_id": block.id,
                    "name": info["name"], "input": str(tool_input.get("input") or ""),
                    "status": "completed"}
        item = {"type": "function_call", "id": self._item_id("fc"), "call_id": block.id,
                "name": info["name"], "arguments": json.dumps(tool_input, ensure_ascii=False),
                "status": "completed"}
        if info.get("namespace"):
            item["namespace"] = info["namespace"]
        return item

    def finish_round(self, message: Any) -> None:
        self.turns.append([b.to_dict(mode="json", exclude_none=True) for b in message.content])
        self.usage.add(getattr(message, "usage", None))

    def closing(self, stop_reason: str | None) -> list[bytes]:
        out: list[bytes] = []
        if stop_reason == "refusal" and not self.wrote_text:
            out += self._done({"type": "message", "role": "assistant", "id": self._item_id("msg"),
                               "status": "completed", "content": [{
                                   "type": "output_text", "annotations": [],
                                   "text": "Claude declined to continue with this request."}]})
        if self.turns:
            summary = [{"type": "summary_text", "text": s} for s in self.summaries[-3:]]
            out += self._done({"type": "reasoning", "id": self._item_id("rs"), "summary": summary,
                               "encrypted_content": encode_blob(self.model, self.turns)})
        usage = self.usage
        out.append(_sse({"type": "response.completed", "response": {
            "id": self.response_id, "status": "completed", "model": self.model,
            "usage": {
                "input_tokens": usage.input_tokens,
                "input_tokens_details": {"cached_tokens": usage.cached_tokens},
                "output_tokens": usage.output_tokens,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": usage.input_tokens + usage.output_tokens,
            },
        }}))
        return out


def _failed(response_id: str, message: str, code: str = "server_error") -> bytes:
    return _sse({"type": "response.failed", "response": {
        "id": response_id, "status": "failed", "error": {"code": code, "message": message}}})


async def _open_stream(client: anthropic.AsyncAnthropic, params: dict[str, Any], betas: list[str]):
    manager = client.beta.messages.stream(**params, betas=betas) if betas else \
        client.beta.messages.stream(**params)
    stream = await manager.__aenter__()
    return manager, stream


async def respond(
    body: dict[str, Any],
    *,
    api_key: str,
    base_url: str,
    on_usage: UsageCallback | None = None,
    agent_id: str = "",
):
    """Answer one Responses request with Claude: a streaming or error response."""
    params, tool_map = translate_request(body)
    model = params["model"]
    client = get_client(api_key, base_url)
    plain = _plain_mode(api_key, model)
    attempt_params, betas = (params, []) if plain else add_extras(params, tool_map)
    has_extras = attempt_params != params
    try:
        try:
            manager, stream = await _open_stream(client, attempt_params, betas)
        except anthropic.BadRequestError as exc:
            if not has_extras:
                raise  # nothing optional to take away: the 400 stands
            logger.info("Claude request with extras refused for agent %s (%s); retrying plain",
                        agent_id, _api_message(exc)[:200])
            _set_plain_mode(api_key, model)
            attempt_params, betas = params, []
            manager, stream = await _open_stream(client, params, [])
    except anthropic.APIStatusError as exc:
        logger.warning("Anthropic answered %s for agent %s: %s", exc.status_code, agent_id,
                       _api_message(exc)[:200])
        return _error_response(exc)
    except anthropic.APIConnectionError as exc:
        logger.warning("Anthropic unreachable for agent %s: %s", agent_id, exc)
        return _openai_error(502, "Anthropic could not be reached.", "server_error")

    response_id = "resp_" + hashlib.sha256(f"{time.time_ns()}{agent_id}".encode()).hexdigest()[:24]
    translator = _Translator(model, tool_map, response_id)

    async def events() -> AsyncIterator[bytes]:
        nonlocal manager, stream
        yield _sse({"type": "response.created", "response": {"id": response_id, "model": model}})
        rounds = 0
        try:
            while True:
                try:
                    async for event in stream:
                        for chunk in translator.event(event):
                            yield chunk
                    message = await stream.get_final_message()
                finally:
                    await manager.__aexit__(None, None, None)
                translator.finish_round(message)
                rounds += 1
                if message.stop_reason != "pause_turn" or rounds > MAX_PAUSE_CONTINUATIONS:
                    break
                # A server tool (web search) paused the turn: send the reply so
                # far back as-is and let Claude carry on in the same response.
                attempt_params["messages"] = attempt_params["messages"] + [
                    {"role": "assistant", "content": translator.turns[-1]}]
                manager, stream = await _open_stream(client, attempt_params, betas)
            for chunk in translator.closing(message.stop_reason):
                yield chunk
        except anthropic.APIStatusError as exc:
            message_text = _api_message(exc)
            logger.warning("Claude stream for agent %s failed: %s", agent_id, message_text[:200])
            code = "server_is_overloaded" if exc.status_code in (529, 503) or \
                "overloaded" in message_text.lower() else "server_error"
            yield _failed(response_id, message_text, code)
        except anthropic.APIConnectionError as exc:
            logger.warning("Claude stream for agent %s dropped: %s", agent_id, exc)
            yield _failed(response_id, "The connection to Anthropic dropped.")
        finally:
            usage = translator.usage
            if on_usage is not None and (usage.input_tokens or usage.output_tokens):
                try:
                    result = on_usage(usage.input_tokens, usage.cached_tokens, usage.output_tokens)
                    if result is not None:
                        await result
                except Exception:
                    logger.exception("recording Claude usage failed")

    return StreamingResponse(events(), headers={"content-type": "text/event-stream"})


def models_listing(models: list[str]) -> JSONResponse:
    """GET /models for an Anthropic agent: the models Den offers, OpenAI-shaped."""
    return JSONResponse({"object": "list", "data": [
        {"id": m, "object": "model", "created": 0, "owned_by": "anthropic"} for m in models]})


async def verify_key(api_key: str, base_url: str) -> str | None:
    """None when Anthropic accepts the key (listing models is free)."""
    client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0,
                                      timeout=20.0)
    try:
        await client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return "Anthropic rejected this key."
    except anthropic.APIConnectionError:
        return "Could not reach Anthropic. Try again in a moment."
    except anthropic.APIStatusError:
        return None  # the key got past authentication
    finally:
        await client.close()
    return None
