"""Accounts: register, login, logout, sessions, CSRF and the login limiter."""

from __future__ import annotations

import pytest

from tests.conftest import (
    DEFAULT_PASSWORD,
    make_settings,
    new_client,
    register,
    running_server,
    unique_email,
)


@pytest.fixture(scope="module")
def server(fake_model, tmp_path_factory):
    settings = make_settings(tmp_path_factory.mktemp("auth"), fake_model, onboarding_probe=False)
    with running_server(settings) as s:
        yield s


@pytest.fixture
def anon(server, client_factory):
    return client_factory(server)


def test_config_needs_no_login(anon) -> None:
    r = anon.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert [m["id"] for m in body["models"]] == ["gpt-6.1-sol", "gpt-6-luna"]
    assert all(m["label"] for m in body["models"])
    assert body["runtime"] == "local"
    assert body["model_auth"] == "proxy"
    assert set(body["limits"]) >= {"min_job_interval_minutes", "max_upload_mb", "max_agents_per_user"}


def test_register_sets_a_session_cookie_and_me_works(anon) -> None:
    email = unique_email("ana")
    r = anon.post(
        "/api/auth/register",
        json={"email": email, "password": DEFAULT_PASSWORD, "timezone": "America/Detroit"},
    )
    assert r.status_code == 201, r.text
    user = r.json()["user"]
    assert set(user) == {"id", "email", "timezone"}
    assert user["email"] == email and user["timezone"] == "America/Detroit"

    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("den_session=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "secure" not in cookie  # secure_cookies is off in tests

    me = anon.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"] == user


def test_email_is_trimmed_and_lowercased(anon) -> None:
    local = unique_email("Mixed").split("@")[0]
    user = register(anon, f"  {local.upper()}@Example.COM ")
    assert user["email"] == f"{local.lower()}@example.com"


def test_unknown_timezone_falls_back_to_utc(anon) -> None:
    assert register(anon, timezone="Mars/Olympus_Mons")["timezone"] == "UTC"


def test_duplicate_email_is_409_case_insensitive(server, client_factory) -> None:
    email = unique_email("dup")
    register(client_factory(server), email)
    r = client_factory(server).post(
        "/api/auth/register", json={"email": email.upper(), "password": DEFAULT_PASSWORD}
    )
    assert r.status_code == 409


@pytest.mark.parametrize(
    "email,password",
    [
        (None, "short"),  # under 8 characters
        (None, "x" * 201),  # over 200 characters
        ("not-an-email", DEFAULT_PASSWORD),
        ("two@@example.com", DEFAULT_PASSWORD),
        ("a" * 250 + "@example.com", DEFAULT_PASSWORD),  # over 254 characters
    ],
)
def test_invalid_registration_is_400(anon, email, password) -> None:
    r = anon.post("/api/auth/register", json={"email": email or unique_email(), "password": password})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]


def test_login_with_the_right_password(server, client_factory) -> None:
    email = unique_email("login")
    register(client_factory(server), email)
    c = client_factory(server)
    r = c.post("/api/auth/login", json={"email": email.upper(), "password": DEFAULT_PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == email
    assert c.get("/api/auth/me").status_code == 200


def test_bad_login_is_401(server, client_factory) -> None:
    email = unique_email("bad")
    register(client_factory(server), email)
    c = client_factory(server)
    assert c.post("/api/auth/login", json={"email": email, "password": "wrong password"}).status_code == 401
    unknown = {"email": unique_email("nobody"), "password": DEFAULT_PASSWORD}
    assert c.post("/api/auth/login", json=unknown).status_code == 401
    assert c.get("/api/auth/me").status_code == 401


def test_login_is_refused_after_ten_failures(server, client_factory) -> None:
    email = unique_email("throttle")
    register(client_factory(server), email)
    c = client_factory(server)
    for _ in range(10):
        r = c.post("/api/auth/login", json={"email": email, "password": "wrong password"})
        assert r.status_code == 401
    # Even the right password is refused while the email is throttled.
    r = c.post("/api/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})
    assert r.status_code == 429


def test_logout_ends_the_session_on_the_server(server, client_factory) -> None:
    c = client_factory(server)
    register(c)
    token = c.cookies.get("den_session")
    assert token

    r = c.post("/api/auth/logout")
    assert r.status_code == 204
    assert c.get("/api/auth/me").status_code == 401

    # Replaying the old cookie does not work either: the session row is gone.
    replay = client_factory(server)
    replay.cookies.set("den_session", token)
    assert replay.get("/api/auth/me").status_code == 401


def test_a_forged_session_cookie_is_401(server, client_factory) -> None:
    c = client_factory(server)
    c.cookies.set("den_session", "forged-token-value")
    assert c.get("/api/auth/me").status_code == 401
    assert c.get("/api/agents").status_code == 401


def test_missing_csrf_header_is_403(server) -> None:
    with new_client(server, csrf=False) as c:
        r = c.post("/api/auth/register", json={"email": unique_email(), "password": DEFAULT_PASSWORD})
        assert r.status_code == 403
        assert r.json()["detail"] == "Missing CSRF header."
        assert c.post("/api/auth/login", json={"email": "a@b.co", "password": "x" * 8}).status_code == 403
        # GET needs no header.
        assert c.get("/api/config").status_code == 200


def test_missing_csrf_header_is_403_even_when_logged_in(server, client_factory) -> None:
    c = client_factory(server)
    register(c)
    with new_client(server, csrf=False) as bare:
        bare.cookies.set("den_session", c.cookies.get("den_session"))
        assert bare.get("/api/agents").status_code == 200
        r = bare.post("/api/agents", json={"name": "A", "model": "gpt-6-luna"})
        assert r.status_code == 403
        assert bare.post("/api/auth/logout").status_code == 403
    assert c.get("/api/agents").json()["agents"] == []


def test_unauthenticated_agents_request_is_401(anon) -> None:
    assert anon.get("/api/agents").status_code == 401
    assert anon.post("/api/agents", json={"name": "A", "model": "gpt-6-luna"}).status_code == 401
