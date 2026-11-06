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
from pydantic import BaseModel

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


class RegisterBody(BaseModel):
    email: str = ""
    password: str = ""
    timezone: str | None = None


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


class PasswordBody(BaseModel):
    current_password: str = ""
    new_password: str = ""


class DeleteAccountBody(BaseModel):
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
    ctx.login_limiter.reset(email)
    # Opportunistic cleanup so expired sessions do not pile up forever.
    ctx.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso(utcnow()),))
    _start_session(ctx, response, row["id"])
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

    for agent in ctx.db.all("SELECT * FROM agents WHERE user_id = ?", (user["id"],)):
        await lifecycle.delete_agent(ctx, agent)
    ctx.db.execute("DELETE FROM users WHERE id = ?", (user["id"],))  # cascades the rest
    logger.info("user %s deleted their account", user["id"])
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=ctx.settings.secure_cookies
    )
    return response
