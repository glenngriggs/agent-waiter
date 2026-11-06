"""Model proxy: agents call the model through here, never with a real key.

A model with a shell can read any key placed in its container, and anyone
can sign up, so each container only gets its agent token. Codex sends that
token as the provider key; this proxy checks it, checks the path and the
model against allowlists, and picks the key for the agent's provider (the
user's own key, or the operator's for allowed accounts; den/providers.py).

Responses API providers get the request passed through with that key and the
answer streamed back byte for byte. Anthropic gets it translated by
den/anthropic_bridge.py. Either way the token usage is recorded.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from . import anthropic_bridge, providers, usage
from .context import AppContext, get_ctx
from .mcp_server import agent_from_bearer

logger = logging.getLogger("den.model_proxy")

router = APIRouter()

# Conversation requests carry the whole context (and sometimes images), so
# allow generous bodies, but bound them so one agent cannot exhaust memory.
MAX_BODY_BYTES = 64 * 1024 * 1024

# Request headers copied to the upstream. Everything else, above all the
# agent's own Authorization header and any cookies, is dropped.
_FORWARD_HEADERS = frozenset(
    {
        "content-type",
        "accept",
        "openai-beta",
        "user-agent",
        "originator",
        "session_id",
        "conversation_id",
        "version",
    }
)
_FORWARD_PREFIXES = ("x-codex-", "x-openai-")

# Response headers copied back. content-encoding must travel with the raw
# bytes from aiter_raw(), and retry-after lets Codex back off correctly on 429.
_RETURN_HEADERS = ("content-encoding", "retry-after")

# Only path segments of plain characters: this rejects "..", "." and
# percent-decoded oddities, which httpx would otherwise normalize into a
# different upstream path than the one the allowlist checked.
_SEGMENT = re.compile(r"^[A-Za-z0-9_\-.]+$")


def _error(status: int, message: str) -> JSONResponse:
    # OpenAI-style error body, so Codex shows a readable reason.
    return JSONResponse({"error": {"message": message}}, status_code=status)


def path_allowed(path: str) -> bool:
    """True for the Responses, Models and Chat Completions endpoints only."""
    segments = path.split("/")
    if any(not _SEGMENT.match(s) or s in (".", "..") for s in segments):
        return False
    if segments[0] in ("responses", "models"):
        return True
    return segments == ["chat", "completions"]


def _upstream_headers(request: Request, api_key: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in _FORWARD_HEADERS or lower.startswith(_FORWARD_PREFIXES):
            headers[lower] = value
    headers["authorization"] = f"Bearer {api_key}"
    # Ask for an uncompressed body: we stream raw bytes, and identity keeps
    # what Codex receives identical to what it would get from the upstream.
    headers["accept-encoding"] = "identity"
    return headers


async def _read_body(request: Request) -> bytes | None:
    """Read the request body, or None when it is over MAX_BODY_BYTES."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class _UpstreamStreamingResponse(StreamingResponse):
    """Streams an upstream httpx response and always closes it.

    Starlette cancels the streaming task when the client disconnects, which
    can leave the body generator suspended and unfinalized; closing in
    `__call__`'s finally releases the upstream connection in every case
    (finished, client gone, or error), so the model stops generating for a
    reader that is no longer there.
    """

    def __init__(
        self, upstream: httpx.Response, *, agent_id: str, on_usage: Any = None, **kwargs: Any
    ) -> None:
        self._upstream = upstream
        self._agent_id = agent_id
        self._on_usage = on_usage
        # Only event streams carry usage; content-encoding is identity (asked for).
        is_sse = upstream.headers.get("content-type", "").startswith("text/event-stream")
        self._scanner = usage.ResponsesUsageScanner() if (is_sse and on_usage) else None
        super().__init__(self._chunks(), **kwargs)

    async def _chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._upstream.aiter_raw():
                if self._scanner is not None:
                    self._scanner.feed(chunk)
                yield chunk
        except httpx.HTTPError as exc:
            # Headers are already sent, so the status cannot change now; end
            # the stream early and Codex treats it as a dropped connection.
            logger.warning(
                "upstream stream for agent %s broke mid-response: %s", self._agent_id, exc
            )
        finally:
            await self._upstream.aclose()
            scanner = self._scanner
            if scanner is not None and scanner.found:
                self._scanner = None  # record once even if finally runs twice
                self._on_usage(scanner.input_tokens, scanner.cached_tokens, scanner.output_tokens)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._upstream.aclose()  # idempotent in httpx


def _strip_bridge_reasoning(payload: dict[str, Any]) -> bool:
    """Drop Claude's replay items from a Responses request; True if any were dropped.

    An agent switched from a Claude model to an OpenAI one still has Claude's
    reasoning items in its history, and OpenAI rejects reasoning it did not
    encrypt itself.
    """
    items = payload.get("input")
    if not isinstance(items, list):
        return False
    kept = [item for item in items if not anthropic_bridge.is_bridge_reasoning(item)]
    if len(kept) == len(items):
        return False
    payload["input"] = kept
    return True


@router.api_route("/model-proxy/v1/{path:path}", methods=["GET", "POST"])
async def model_proxy(
    path: str, request: Request, ctx: AppContext = Depends(get_ctx)
) -> Response:
    agent = agent_from_bearer(ctx, request.headers.get("authorization"))
    if agent is None:
        return _error(401, "Invalid or revoked agent token.")
    if not path_allowed(path):
        return _error(404, f"The path '{path}' is not available through the platform's model proxy.")

    settings = ctx.settings
    provider_id = agent.get("provider") or providers.PLATFORM
    user_id = agent["user_id"]
    offered = [m.id for m in providers.models_for(ctx, user_id, provider_id)]
    body = b""
    payload: Any = None
    if request.method == "POST":
        read = await _read_body(request)
        if read is None:
            return _error(413, "The request body is too large.")
        body = read
        if body:
            # The model allowlist can only be enforced on a body we can read,
            # so unreadable bodies are refused rather than passed through.
            try:
                payload = json.loads(body)
            except ValueError:
                return _error(400, "The request body must be JSON.")
            if isinstance(payload, dict) and "model" in payload:
                model = payload["model"]
                if not isinstance(model, str) or model not in offered:
                    return _error(403, f"Model '{model}' is not offered on this platform.")

    if settings.model_auth == "chatgpt" and provider_id == providers.PLATFORM:
        return _error(502, "No upstream API key is configured on the platform.")
    creds = providers.resolve_credentials(ctx, user_id, provider_id)
    if creds is None:
        # 401 makes Codex stop instead of retrying; the bridge words it for the user.
        return _error(401, providers.no_key_message(settings, provider_id))
    if ctx.http is None:
        logger.error("model proxy called before the HTTP client was created")
        return _error(502, "The platform's model proxy is not ready yet.")
    provider = providers.CATALOG.get(provider_id)
    if provider is not None and provider.custom_url:
        # A user-supplied name was checked when the key was saved, but its
        # DNS may have been pointed at a private address since. Re-checking
        # here narrows that to the gap between this lookup and the connect.
        try:
            await providers.check_base_url(creds.base_url, settings.allow_private_provider_urls)
        except providers.ProviderInputError as exc:
            logger.warning("custom base URL for agent %s refused: %s", agent["id"], exc)
            return _error(403, f"The server URL saved for this key is no longer allowed: {exc}")

    model_name = payload.get("model") if isinstance(payload, dict) else None

    def on_usage(input_tokens: int, cached_tokens: int, output_tokens: int) -> None:
        usage.record(
            ctx, user_id=user_id, agent_id=agent["id"], provider=provider_id,
            model=str(model_name or agent["model"]), input_tokens=input_tokens,
            cached_tokens=cached_tokens, output_tokens=output_tokens,
        )

    if creds.api == "anthropic":
        if request.method == "GET" and path == "models":
            return anthropic_bridge.models_listing(offered)
        if path != "responses" or not isinstance(payload, dict):
            return _error(404, f"The path '{path}' is not available for Claude models.")
        return await anthropic_bridge.respond(
            payload, api_key=creds.api_key, base_url=creds.base_url, on_usage=on_usage,
            agent_id=agent["id"],
        )

    if isinstance(payload, dict) and _strip_bridge_reasoning(payload):
        body = json.dumps(payload).encode("utf-8")

    url = f"{creds.base_url}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"  # passed through exactly as Codex sent it
    upstream_request = ctx.http.build_request(
        request.method,
        url,
        headers=_upstream_headers(request, creds.api_key),
        content=body if request.method == "POST" else None,
    )
    try:
        upstream = await ctx.http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("model upstream %s unreachable for agent %s: %s", creds.base_url,
                       agent["id"], exc)
        return _error(502, f"The model provider could not be reached: {type(exc).__name__}.")

    if upstream.status_code >= 400:
        # Worth a log line: 401 here means the key is wrong.
        logger.warning(
            "model upstream answered %s for agent %s (%s %s, %s key)",
            upstream.status_code, agent["id"], request.method, path, creds.source,
        )
    headers = {h: upstream.headers[h] for h in _RETURN_HEADERS if h in upstream.headers}
    # Set content-type as a header rather than media_type, which would make
    # Starlette append "; charset=utf-8" to text/event-stream.
    headers["content-type"] = upstream.headers.get("content-type", "application/octet-stream")
    return _UpstreamStreamingResponse(
        upstream,
        agent_id=agent["id"],
        on_usage=on_usage if path == "responses" and upstream.status_code < 400 else None,
        status_code=upstream.status_code,
        headers=headers,
    )
