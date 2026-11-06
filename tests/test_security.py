"""Password hashing, session tokens, agent tokens and the login limiter."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time

import pytest

from den.db import new_id
from den.security import (
    LoginLimiter,
    hash_password,
    hash_token,
    load_or_create_secret,
    make_agent_token,
    new_session_token,
    parse_agent_token,
    verify_password,
)

SECRET = b"s" * 32


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct horse battery")
    assert encoded.startswith("scrypt$16384$8$1$")
    assert verify_password("correct horse battery", encoded)


def test_wrong_password_is_rejected() -> None:
    encoded = hash_password("correct horse battery")
    assert not verify_password("correct horse batterY", encoded)
    assert not verify_password("", encoded)


def test_hashes_are_salted() -> None:
    assert hash_password("same password") != hash_password("same password")


@pytest.mark.parametrize("encoded", ["", "plain", "scrypt$1$2", "bcrypt$16384$8$1$AA==$AA=="])
def test_malformed_hash_never_verifies(encoded: str) -> None:
    assert not verify_password("anything", encoded)


# --------------------------------------------------------------------------
# Session tokens and the server secret
# --------------------------------------------------------------------------


def test_session_token_and_its_hash() -> None:
    token, digest = new_session_token()
    assert len(token) >= 40
    assert digest == hashlib.sha256(token.encode()).hexdigest() == hash_token(token)
    assert new_session_token()[0] != token


def test_secret_is_created_once_with_private_mode(tmp_path) -> None:
    first = load_or_create_secret(tmp_path)
    assert len(first) == 32
    mode = stat.S_IMODE(os.stat(tmp_path / "secret.key").st_mode)
    assert mode == 0o600
    assert load_or_create_secret(tmp_path) == first


# --------------------------------------------------------------------------
# Agent tokens
# --------------------------------------------------------------------------


def test_agent_token_round_trip() -> None:
    agent_id = new_id()
    token = make_agent_token(SECRET, agent_id, 3)
    assert re.fullmatch(rf"den_{agent_id}_3_[0-9a-f]{{64}}", token)
    assert parse_agent_token(SECRET, token) == (agent_id, 3)


def test_tampered_hmac_is_rejected() -> None:
    token = make_agent_token(SECRET, new_id(), 1)
    last = token[-1]
    tampered = token[:-1] + ("0" if last != "0" else "1")
    assert parse_agent_token(SECRET, tampered) is None


def test_token_from_another_secret_is_rejected() -> None:
    token = make_agent_token(b"x" * 32, new_id(), 1)
    assert parse_agent_token(SECRET, token) is None


def test_changing_the_version_breaks_the_hmac() -> None:
    agent_id = new_id()
    token = make_agent_token(SECRET, agent_id, 1)
    forged = token.replace(f"_{1}_", f"_{2}_", 1)
    assert parse_agent_token(SECRET, forged) is None


def test_changing_the_agent_id_breaks_the_hmac() -> None:
    token = make_agent_token(SECRET, new_id(), 1)
    other = new_id()
    forged = "den_" + other + token[len("den_") + 32 :]
    assert parse_agent_token(SECRET, forged) is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "den_",
        "Bearer den_x",
        "den_abc_1_" + "0" * 64,  # agent id too short
        f"den_{'a' * 32}_one_{'0' * 64}",  # version not a number
        f"den_{'a' * 32}_1_{'0' * 63}",  # hmac too short
        f"den_{'a' * 32}_1_{'0' * 64}_extra",
        f"DEN_{'a' * 32}_1_{'0' * 64}",
    ],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    assert parse_agent_token(SECRET, token) is None


# --------------------------------------------------------------------------
# Login limiter
# --------------------------------------------------------------------------


def test_login_limiter_blocks_after_ten_failures() -> None:
    limiter = LoginLimiter()
    email = "ana@example.com"
    for _ in range(9):
        assert limiter.check(email)
        limiter.fail(email)
    assert limiter.check(email)  # 9 failures: still allowed
    limiter.fail(email)
    assert not limiter.check(email)  # 10 failures: blocked
    assert limiter.check("ben@example.com")  # other emails are unaffected


def test_login_limiter_is_case_insensitive_and_resets() -> None:
    limiter = LoginLimiter()
    for _ in range(10):
        limiter.fail("Ana@Example.com")
    assert not limiter.check("ana@example.com")
    limiter.reset("ana@example.com")
    assert limiter.check("Ana@Example.com")


def test_login_limiter_forgets_old_failures() -> None:
    limiter = LoginLimiter(max_failures=2, window_seconds=0.1)
    limiter.fail("ana@example.com")
    limiter.fail("ana@example.com")
    assert not limiter.check("ana@example.com")
    time.sleep(0.15)
    assert limiter.check("ana@example.com")
