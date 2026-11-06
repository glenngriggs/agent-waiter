"""Accounts: register, log in, log out, and the dependencies every route uses.

`current_user` turns the session cookie into a user row. `owned_agent` is the
single gate for every route under /api/agents/{agent_id}: it loads the agent
only together with the signed-in user's id, and answers 404 (never 403) so a
URL edited to someone else's agent reveals nothing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from .context import AppContext, get_ctx
from .db import iso, new_id, utcnow
from .security import hash_password, hash_token, new_session_token, verify_password

logger = logging.getLogger("den.auth")

router = APIRouter(prefix="/api/auth")

COOKIE_NAME = "den_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LEN = 254
MIN_PASSWORD_LEN, MAX_PASSWORD_LEN = 8, 200

# Login for an unknown email still runs scrypt against this hash, so the
# response time does not reveal which emails have accounts. Built lazily
# because hashing costs tens of milliseconds.
_dummy_hash: str | None = None


class _TextBody(BaseModel):
    """JSON may carry lone UTF-16 surrogates ("\\ud800") that Python accepts
    but cannot encode, so scrypt and SQLite would raise and the request end
    as a bare 500. Reject them here as ordinary bad input (a 400)."""

    @field_validator("*")
    @classmethod
    def _encodable(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("contains characters that are not valid text") from None
        return value


class RegisterBody(_TextBody):
    email: str = ""
    password: str = ""
    timezone: str | None = None


class LoginBody(_TextBody):
    email: str = ""
    password: str = ""


class PasswordBody(_TextBody):
    current_password: str = ""
    new_password: str = ""


class DeleteAccountBody(_TextBody):
    password: str = ""


def user_out(row: dict) -> dict:
    return {"id": row["id"], "email": row["email"], "timezone": row["timezone"]}


def valid_timezone(name: str | None) -> bool:
    """True when `name` is a zoneinfo key such as 'America/Detroit'."""
    if not name or not isinstance(name, str):
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _normalize_email(raw: str) -> str:
    return raw.strip().lower()


def _start_session(ctx: AppContext, response: Response, user_id: str) -> None:
    """Create a session row (only the token's hash is stored) and set the cookie."""
    token, token_hash = new_session_token()
    now = utcnow()
    expires = now + timedelta(days=ctx.settings.session_days)
    ctx.db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, user_id, iso(now), iso(expires)),
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=ctx.settings.session_days * 86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=ctx.settings.secure_cookies,
    )


async def current_user(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    """The signed-in user's row, or 401."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Not signed in.")
    row = ctx.db.one(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
        (hash_token(token), iso(utcnow())),
    )
    if row is None:
        raise HTTPException(401, "Not signed in.")
    return row


async def owned_agent(
    agent_id: str,
    user: dict = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    """The agent row if it exists AND belongs to the signed-in user, else 404."""
    row = ctx.db.one(
        "SELECT * FROM agents WHERE id = ? AND user_id = ?", (agent_id, user["id"])
    )
    if row is None:
        raise HTTPException(404, "Agent not found")
    return row


@router.post("/register", status_code=201)
async def register(
    body: RegisterBody, response: Response, ctx: AppContext = Depends(get_ctx)
) -> dict:
    email = _normalize_email(body.email)
    if len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    if not (MIN_PASSWORD_LEN <= len(body.password) <= MAX_PASSWORD_LEN):
        raise HTTPException(
            400, f"The password must be {MIN_PASSWORD_LEN} to {MAX_PASSWORD_LEN} characters."
        )
    tz = body.timezone.strip() if isinstance(body.timezone, str) else None
    tz = tz if valid_timezone(tz) else "UTC"

    if ctx.db.one("SELECT id FROM users WHERE email = ?", (email,)) is not None:
        raise HTTPException(409, "An account with this email already exists.")
    # scrypt is deliberately slow; keep it off the event loop.
    password_hash = await asyncio.to_thread(hash_password, body.password)
    user = {
        "id": new_id(),
        "email": email,
        "password_hash": password_hash,
        "timezone": tz,
        "created_at": iso(utcnow()),
    }
    try:
        ctx.db.execute(
            "INSERT INTO users (id, email, password_hash, timezone, created_at) "
            "VALUES (:id, :email, :password_hash, :timezone, :created_at)",
            user,
        )
    except sqlite3.IntegrityError:
        # Two registrations raced past the SELECT above; the UNIQUE index decides.
        raise HTTPException(409, "An account with this email already exists.") from None
    _start_session(ctx, response, user["id"])
    logger.info("registered user %s", user["id"])
    return {"user": user_out(user)}


@router.post("/login")
async def login(body: LoginBody, response: Response, ctx: AppContext = Depends(get_ctx)) -> dict:
    global _dummy_hash
    email = _normalize_email(body.email)
    # No account can have a malformed or over-long email, so refuse it before
    # it becomes a login-limiter key (login needs no account, and every
    # distinct key costs memory until it ages out).
    if len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
        raise HTTPException(401, "Wrong email or password.")
    if not ctx.login_limiter.check(email):
        raise HTTPException(429, "Too many failed logins for this email. Try again in a few minutes.")
    row = ctx.db.one("SELECT * FROM users WHERE email = ?", (email,))
    if row is None:
        if _dummy_hash is None:
            _dummy_hash = await asyncio.to_thread(hash_password, "not-a-real-password")
        encoded = _dummy_hash
    else:
        encoded = row["password_hash"]
    ok = await asyncio.to_thread(verify_password, body.password, encoded)
    if row is None or not ok:
        ctx.login_limiter.fail(email)
        raise HTTPException(401, "Wrong email or password.")
    # scrypt ran off the event loop, so a password change may have committed
    # meanwhile. Create the session only if the hash we verified is still the
    # current one, in the same transaction: change_password's UPDATE+DELETE
    # then lands either before (we refuse) or after (it deletes this session).
    with ctx.db.transaction() as db:
        current = db.one("SELECT password_hash FROM users WHERE id = ?", (row["id"],))
        if current is None or current["password_hash"] != encoded:
            raise HTTPException(401, "Wrong email or password.")
        # Opportunistic cleanup so expired sessions do not pile up forever.
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso(utcnow()),))
        _start_session(ctx, response, row["id"])
    ctx.login_limiter.reset(email)
    return {"user": user_out(row)}


@router.post("/logout", status_code=204)
async def logout(request: Request, ctx: AppContext = Depends(get_ctx)) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        ctx.db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
    # A Response returned directly does not pick up cookies set on an injected
    # one, so the cookie is cleared on this object.
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=ctx.settings.secure_cookies
    )
    return response


@router.get("/me")
async def me(user: dict = Depends(current_user)) -> dict:
    return {"user": user_out(user)}


async def _check_password(ctx: AppContext, user: dict, password: str) -> None:
    if not ctx.login_limiter.check(user["email"]):
        raise HTTPException(429, "Too many wrong passwords. Try again in a few minutes.")
    if not await asyncio.to_thread(verify_password, password, user["password_hash"]):
        ctx.login_limiter.fail(user["email"])
        raise HTTPException(400, "The current password is wrong.")
    ctx.login_limiter.reset(user["email"])


@router.post("/password", status_code=204)
async def change_password(
    body: PasswordBody,
    request: Request,
    user: dict = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    """Change the password and sign out every other session."""
    await _check_password(ctx, user, body.current_password)
    if not (MIN_PASSWORD_LEN <= len(body.new_password) <= MAX_PASSWORD_LEN):
        raise HTTPException(
            400, f"The new password must be {MIN_PASSWORD_LEN} to {MAX_PASSWORD_LEN} characters."
        )
    password_hash = await asyncio.to_thread(hash_password, body.new_password)
    current = hash_token(request.cookies.get(COOKIE_NAME, ""))
    with ctx.db.transaction() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user["id"], current))
    logger.info("user %s changed their password", user["id"])
    return Response(status_code=204)


@router.post("/delete-account", status_code=204)
async def delete_account(
    body: DeleteAccountBody,
    user: dict = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    """Delete the account: every agent (containers, files, jobs), keys and usage."""
    await _check_password(ctx, user, body.password)
    from . import lifecycle  # local: lifecycle imports the bridge, which imports more

    # Sign out every session (this one included) first: deleting agents takes
    # seconds each, and another tab must not create a new agent meanwhile.
    ctx.db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    # A create that passed auth before that can still insert a row, and the
    # users cascade would drop it without removing its files or container.
    # So repeat until no agents are left, and delete the user only in the
    # same transaction that sees none (create_agent checks the user exists
    # inside its own transaction, so nothing can slip in after).
    while True:
        for agent in ctx.db.all("SELECT * FROM agents WHERE user_id = ?", (user["id"],)):
            await lifecycle.delete_agent(ctx, agent)
        with ctx.db.transaction() as db:
            if db.one("SELECT 1 FROM agents WHERE user_id = ? LIMIT 1", (user["id"],)) is None:
                db.execute("DELETE FROM users WHERE id = ?", (user["id"],))  # cascades the rest
                break
    logger.info("user %s deleted their account", user["id"])
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=ctx.settings.secure_cookies
    )
    return response
