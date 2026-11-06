"""Model proxy: agents call the model through here, never with the real key.

A model with a shell can read any key placed in its container, and anyone
can sign up, so each container only gets its agent token. Codex sends that
token as the provider key; this proxy checks it, checks the path and the
model against allowlists, swaps in the operator's upstream key and streams
the upstream response back byte for byte.
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

    def __init__(self, upstream: httpx.Response, *, agent_id: str, **kwargs: Any) -> None:
        self._upstream = upstream
        self._agent_id = agent_id
        super().__init__(self._chunks(), **kwargs)

    async def _chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            # Headers are already sent, so the status cannot change now; end
            # the stream early and Codex treats it as a dropped connection.
            logger.warning(
                "upstream stream for agent %s broke mid-response: %s", self._agent_id, exc
            )
        finally:
            await self._upstream.aclose()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._upstream.aclose()  # idempotent in httpx


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
    body = b""
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
                if not isinstance(model, str) or model not in settings.models:
                    return _error(403, f"Model '{model}' is not offered on this platform.")

    if settings.model_auth == "chatgpt" or not settings.upstream_api_key:
        return _error(502, "No upstream API key is configured on the platform.")
    if ctx.http is None:
        logger.error("model proxy called before the HTTP client was created")
        return _error(502, "The platform's model proxy is not ready yet.")

    url = f"{settings.upstream_base_url}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"  # passed through exactly as Codex sent it
    upstream_request = ctx.http.build_request(
        request.method,
        url,
        headers=_upstream_headers(request, settings.upstream_api_key),
        content=body if request.method == "POST" else None,
    )
    try:
        upstream = await ctx.http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("model upstream %s unreachable for agent %s: %s", url, agent["id"], exc)
        return _error(502, f"The model provider could not be reached: {type(exc).__name__}.")

    if upstream.status_code >= 400:
        # Worth a log line: 401 here means the operator's key is wrong.
        logger.warning(
            "model upstream answered %s for agent %s (%s %s)",
            upstream.status_code, agent["id"], request.method, path,
        )
    headers = {h: upstream.headers[h] for h in _RETURN_HEADERS if h in upstream.headers}
    # Set content-type as a header rather than media_type, which would make
    # Starlette append "; charset=utf-8" to text/event-stream.
    headers["content-type"] = upstream.headers.get("content-type", "application/octet-stream")
    return _UpstreamStreamingResponse(
        upstream,
        agent_id=agent["id"],
        status_code=upstream.status_code,
        headers=headers,
    )
