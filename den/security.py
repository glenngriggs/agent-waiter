"""Password hashing, session tokens, agent tokens and login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_AGENT_TOKEN = re.compile(r"^den_([0-9a-f]{32})_(\d{1,9})_([0-9a-f]{64})$")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session_token() -> tuple[str, str]:
    """Return (token for the cookie, sha256 for the database)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def load_or_create_secret(data_dir: Path) -> bytes:
    path = data_dir / "secret.key"
    if path.is_file():
        return path.read_bytes()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secret)
    return secret


def _agent_mac(secret: bytes, agent_id: str, version: int) -> str:
    return hmac.new(secret, f"{agent_id}:{version}".encode(), hashlib.sha256).hexdigest()


def make_agent_token(secret: bytes, agent_id: str, version: int) -> str:
    return f"den_{agent_id}_{version}_{_agent_mac(secret, agent_id, version)}"


def parse_agent_token(secret: bytes, token: str) -> tuple[str, int] | None:
    """Return (agent_id, version) if the token's HMAC is valid.

    The caller must still check the version against the agents table,
    because bumping token_version is how a token gets revoked.
    """
    match = _AGENT_TOKEN.match(token or "")
    if not match:
        return None
    agent_id, version_text, mac = match.groups()
    version = int(version_text)
    if not hmac.compare_digest(mac, _agent_mac(secret, agent_id, version)):
        return None
    return agent_id, version


class LoginLimiter:
    """Refuse logins for an email after too many recent failures (in memory)."""

    def __init__(self, max_failures: int = 10, window_seconds: float = 600.0) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, email: str) -> deque[float]:
        q = self._failures[email.lower()]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def check(self, email: str) -> bool:
        return len(self._trim(email)) < self.max_failures

    def fail(self, email: str) -> None:
        self._trim(email).append(time.monotonic())

    def reset(self, email: str) -> None:
        self._failures.pop(email.lower(), None)
