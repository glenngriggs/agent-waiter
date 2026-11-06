"""Browser security headers, and the inline theme script still allowed by CSP."""

from __future__ import annotations

import base64
import hashlib
import re

from den.app import STATIC_DIR


def test_page_has_a_strict_policy_that_allows_its_inline_script(client) -> None:
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "object-src 'none'" in csp
    for body in re.findall(r"<script>(.*?)</script>", (STATIC_DIR / "index.html").read_text(), re.DOTALL):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"


def test_api_responses_get_the_headers_too(client) -> None:
    r = client.get("/api/config")
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "strict-transport-security" not in r.headers  # only with secure cookies (HTTPS)
