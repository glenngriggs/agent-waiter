"""Regression tests for session, account and login-limiter fixes.

Covers: open SSE streams ending once their session is revoked or expired,
a login racing a password change, delete-account racing agent creation,
lone-surrogate strings in auth bodies, and the login limiter's memory.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

import den.agents as agents_mod
import den.auth as auth_mod
import den.chat as chat_mod
from den.db import iso, new_id, utcnow
from den.security import LoginLimiter, verify_password as real_verify_password
from tests.conftest import (
    DEFAULT_PASSWORD,
    create_agent,
    create_ready_agent,
    register,
    unique_email,
    wait_for,
)

NEW_PASSWORD = "a whole new password"


# --------------------------------------------------------------------------
# Open event streams end when their session stops being valid
# --------------------------------------------------------------------------


class _StreamReader:
    """Reads an SSE stream in a thread and records when it ends."""

    def __init__(self, client, agent_id: str) -> None:
        self.lines: list[str] = []
        self.ended = threading.Event()
        self.connected = threading.Event()

        def run() -> None:
            try:
                with client.stream(
                    "GET", f"/api/agents/{agent_id}/events",
                    params={"after": 10**9}, timeout=None,
                ) as resp:
                    for line in resp.iter_lines():
                        self.lines.append(line)
                        if line.startswith(": connected"):
                            self.connected.set()
            except httpx.HTTPError:
                pass  # the client was closed under a still-open stream at teardown
            finally:
                self.ended.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


@pytest.fixture
def fast_session_checks(monkeypatch) -> None:
    monkeypatch.setattr(chat_mod, "PING_SECONDS", 0.2)
    monkeypatch.setattr(chat_mod, "SESSION_CHECK_SECONDS", 0.2)


def test_open_stream_ends_after_password_change(
    fast_session_checks, live_server, client_factory
) -> None:
    owner = client_factory(live_server)
    email = unique_email()
    register(owner, email)
    agent = create_agent(owner)
    other = client_factory(live_server)
    r = other.post("/api/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})
    assert r.status_code == 200, r.text

    kept = _StreamReader(owner, agent["id"])
    revoked = _StreamReader(other, agent["id"])
    assert kept.connected.wait(10) and revoked.connected.wait(10)

    r = owner.post(
        "/api/auth/password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 204, r.text
    assert other.get("/api/auth/me").status_code == 401

    assert revoked.ended.wait(10), "the signed-out session's stream stayed open"
    # The session that changed the password is still valid; its stream lives on.
    time.sleep(1.0)
    assert not kept.ended.is_set()


def test_open_stream_ends_when_session_expires(
    fast_session_checks, live_server, client_factory
) -> None:
    client = client_factory(live_server)
    register(client)
    agent = create_agent(client)
    reader = _StreamReader(client, agent["id"])
    assert reader.connected.wait(10)
    time.sleep(0.5)
    assert not reader.ended.is_set()

    live_server.ctx.db.execute("UPDATE sessions SET expires_at = ?", (iso(utcnow()),))
    assert reader.ended.wait(10), "the expired session's stream stayed open"


# --------------------------------------------------------------------------
# A login racing a password change does not keep a session
# --------------------------------------------------------------------------


def test_login_in_flight_during_password_change_gets_no_session(
    live_server, client_factory, monkeypatch
) -> None:
    slow_next = threading.Event()

    def gated_verify(password: str, encoded: str) -> bool:
        ok = real_verify_password(password, encoded)
        if slow_next.is_set():
            slow_next.clear()
            time.sleep(1.0)  # widen the scrypt window so the change lands inside it
        return ok

    monkeypatch.setattr(auth_mod, "verify_password", gated_verify)
    victim = client_factory(live_server)
    email = unique_email()
    register(victim, email)
    attacker = client_factory(live_server)

    result: dict = {}
    slow_next.set()
    t = threading.Thread(target=lambda: result.setdefault("login", attacker.post(
        "/api/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})))
    t.start()
    wait_for(lambda: not slow_next.is_set(), 5, 0.02, "the login to reach verify")
    r = victim.post(
        "/api/auth/password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 204, r.text
    t.join(10)

    assert result["login"].status_code == 401, result["login"].text
    assert attacker.get("/api/auth/me").status_code == 401
    assert victim.get("/api/auth/me").status_code == 200


# --------------------------------------------------------------------------
# delete-account leaves no agent behind
# --------------------------------------------------------------------------


def test_delete_account_removes_agents_created_while_it_runs(
    live_server, client_factory
) -> None:
    client = client_factory(live_server)
    user = register(client)
    first = create_ready_agent(client)
    ctx = live_server.ctx
    original_remove = ctx.runtime.remove
    removed: list[str] = []
    late_id = new_id()

    async def slow_remove(agent_id: str) -> None:
        removed.append(agent_id)
        if agent_id == first["id"]:
            # Stand-in for `docker rm`. Meanwhile another tab's create, which
            # passed auth earlier, inserts its row and makes its directory.
            now = iso(utcnow())
            ctx.db.execute(
                "INSERT INTO agents (id, user_id, name, model, provider, setup_state, "
                "setup_detail, token_version, chat_thread_id, conversation_no, created_at, "
                "last_active_at) VALUES (?, ?, 'B', 'gpt-6-luna', 'platform', 'creating', "
                "NULL, 1, NULL, 1, ?, ?)",
                (late_id, user["id"], now, now),
            )
            (ctx.settings.data_dir / "agents" / late_id / "workspace").mkdir(parents=True)
            await asyncio.sleep(1.0)
        await original_remove(agent_id)

    ctx.runtime.remove = slow_remove
    result: dict = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "r", client.post("/api/auth/delete-account", json={"password": DEFAULT_PASSWORD})))
    t.start()
    wait_for(lambda: removed, 30, 0.02, "the first agent's removal to start")
    # Sessions are revoked before agents are torn down, so a new create is refused.
    r = client_factory(live_server, cookies=client.cookies).post(
        "/api/agents", json={"name": "C", "model": "gpt-6-luna"})
    assert r.status_code == 401, r.text
    t.join(60)

    assert result["r"].status_code == 204, result["r"].text
    assert late_id in removed
    assert ctx.db.all("SELECT id FROM agents") == []
    assert ctx.db.all("SELECT id FROM users WHERE id = ?", (user["id"],)) == []
    assert not (ctx.settings.data_dir / "agents" / late_id).exists()
    assert not (ctx.settings.data_dir / "agents" / first["id"]).exists()


def test_create_agent_after_user_deleted_is_401_not_500(
    live_server, client_factory, monkeypatch
) -> None:
    client = client_factory(live_server)
    register(client)
    ctx = live_server.ctx
    original_check = agents_mod._check_model

    def check_then_delete_user(ctx_, user, model, provider):
        out = original_check(ctx_, user, model, provider)
        ctx.db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        return out

    monkeypatch.setattr(agents_mod, "_check_model", check_then_delete_user)
    r = client.post("/api/agents", json={"name": "A", "model": "gpt-6-luna"})
    assert r.status_code == 401, r.text
    assert ctx.db.all("SELECT id FROM agents") == []


# --------------------------------------------------------------------------
# Lone surrogates in auth bodies are a 400, not a 500
# --------------------------------------------------------------------------

_SURROGATE = "\\ud800"  # a JSON escape for a lone surrogate


def _post_raw(client, path: str, body: str):
    return client.post(path, content=body, headers={"Content-Type": "application/json"})


@pytest.mark.parametrize("body", [
    '{"email": "s1@example.com", "password": "abcdefgh%s"}' % _SURROGATE,
    '{"email": "s%s@example.com", "password": "abcdefghij"}' % _SURROGATE,
])
def test_register_with_surrogate_is_400(live_server, client_factory, body: str) -> None:
    client = client_factory(live_server)
    r = _post_raw(client, "/api/auth/register", body)
    assert r.status_code == 400, r.text
    assert "detail" in r.json()
    # The connection is still usable afterwards.
    assert client.get("/api/auth/me").status_code == 401


def test_login_and_password_change_with_surrogate_are_400(live_server, client_factory) -> None:
    client = client_factory(live_server)
    register(client)
    r = _post_raw(client, "/api/auth/login",
                  '{"email": "x%s@example.com", "password": "abcdefghij"}' % _SURROGATE)
    assert r.status_code == 400, r.text
    r = _post_raw(client, "/api/auth/password",
                  '{"current_password": "%s", "new_password": "abcdefgh%s"}'
                  % (DEFAULT_PASSWORD, _SURROGATE))
    assert r.status_code == 400, r.text
    assert client.get("/api/auth/me").status_code == 200


# --------------------------------------------------------------------------
# The login limiter does not keep an entry for every email ever tried
# --------------------------------------------------------------------------


def test_limiter_check_does_not_create_entries() -> None:
    limiter = LoginLimiter()
    for i in range(1000):
        assert limiter.check(f"user{i}@example.com")
    assert len(limiter._failures) == 0


def test_limiter_drops_expired_entries() -> None:
    limiter = LoginLimiter(max_failures=2, window_seconds=0.05)
    limiter.fail("a@example.com")
    limiter.fail("a@example.com")
    assert not limiter.check("a@example.com")
    time.sleep(0.1)
    assert limiter.check("a@example.com")
    assert "a@example.com" not in limiter._failures


def test_limiter_sweeps_emails_never_tried_again() -> None:
    limiter = LoginLimiter(max_failures=10, window_seconds=0.05)
    for i in range(100):
        limiter.fail(f"old{i}@example.com")
    time.sleep(0.1)
    for i in range(LoginLimiter.SWEEP_EVERY):
        limiter.fail(f"new{i}@example.com")
    assert not any(key.startswith("old") for key in limiter._failures)
    # Recent failures are still counted.
    for _ in range(9):
        limiter.fail("new0@example.com")
    assert not limiter.check("new0@example.com")


def test_login_with_malformed_or_huge_email_is_not_remembered(
    live_server, client_factory
) -> None:
    client = client_factory(live_server)
    limiter = live_server.ctx.login_limiter
    for email in ("a" * 200_000 + "@example.com", "no-at-sign", ""):
        r = client.post("/api/auth/login", json={"email": email, "password": "whatever123"})
        assert r.status_code == 401, r.text
    assert len(limiter._failures) == 0
