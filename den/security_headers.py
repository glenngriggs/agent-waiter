"""Standard browser security headers on every response.

The page gets a Content-Security-Policy that only runs this server's own
scripts (plus the one inline theme snippet in index.html, allowed by its
hash), so even if some text slipped past the safe-DOM rule it could not run
code. Responses that set their own policy (file downloads use
`default-src 'none'; sandbox`) keep it.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)


def inline_script_hashes(index_html: Path) -> list[str]:
    """CSP hash sources for every inline <script> in the page."""
    try:
        text = index_html.read_text(encoding="utf-8")
    except OSError:
        return []
    return [
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'"
        for body in _INLINE_SCRIPT.findall(text)
    ]


def content_security_policy(index_html: Path) -> str:
    scripts = " ".join(["'self'", *inline_script_hashes(index_html)])
    return "; ".join([
        "default-src 'self'",
        f"script-src {scripts}",
        # The UI sets a few style attributes (avatar hues).
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ])


class SecurityHeadersMiddleware:
    """Plain ASGI (no buffering), so the SSE stream is untouched."""

    def __init__(self, app: ASGIApp, *, csp: str, hsts: bool = False) -> None:
        self.app = app
        self.headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"same-origin"),
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
            (b"content-security-policy", csp.encode()),
        ]
        if hsts:  # only meaningful (and only safe) when served over HTTPS
            self.headers.append((b"strict-transport-security", b"max-age=31536000"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(n, v) for n, v in self.headers if n not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
