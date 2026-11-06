"""Regression tests for the shared-key allow list, chatgpt mode, custom URLs,
model-list edits and the .env loader."""

from __future__ import annotations

import dataclasses
import logging
import os
import socket
from datetime import timedelta

import pytest

from den import config, providers
from den.db import iso, utcnow
from tests.conftest import (
    agent_token,
    new_client,
    register,
    wait_setup_settled,
)
from tests.test_model_proxy import proxy, responses_body


def _provider_ids(client) -> list[str]:
    return [p["id"] for p in client.get("/api/providers").json()["providers"]]


def _insert_key(server, user_id: str, provider_id: str, *, base_url: str | None = None,
                models: str = "") -> None:
    now = iso(utcnow())
    server.ctx.db.execute(
        "INSERT INTO api_keys (user_id, provider, key_ciphertext, key_hint, base_url, models, "
        "verified_at, created_at, updated_at) VALUES (?, ?, ?, '…abcd', ?, ?, ?, ?, ?)",
        (user_id, provider_id, providers.encrypt_key(server.ctx.secret, "sk-left-over-key-abcd"),
         base_url, models, now, now, now),
    )


# --------------------------------------------------------------------------
# #5: a listed email only counts for an account that predates the server start
# --------------------------------------------------------------------------


def test_platform_allowed_needs_account_created_before_start(settings_factory) -> None:
    s = settings_factory(platform_key_users=("owner@example.com",))
    before = iso(s.started_at - timedelta(seconds=1))
    after = iso(s.started_at + timedelta(seconds=1))
    assert s.platform_allowed("Owner@Example.com", before)
    assert not s.platform_allowed("owner@example.com", after)
    assert not s.platform_allowed("owner@example.com")  # creation time unknown
    assert not s.platform_allowed("other@example.com", before)
    everyone = dataclasses.replace(s, platform_key_users=("*",))
    assert everyone.platform_allowed("anyone@example.com", after)


def test_squatter_on_listed_email_gets_no_shared_key_until_restart(
    server_factory, settings_factory, client_factory
) -> None:
    listed = ("owner@example.com",)
    server = server_factory(settings_factory(platform_key_users=listed, onboarding_probe=False))
    stranger = client_factory(server)
    register(stranger, email="owner@example.com")
    # Registered after the server started: the listed address alone is not enough.
    assert "platform" not in _provider_ids(stranger)
    r = stranger.post("/api/agents", json={"name": "A", "model": "gpt-6.1-sol"})
    assert r.status_code == 400

    # The operator's flow: sign up, then (re)start with the list. The account
    # now predates the start, so it may use the shared key.
    server.stop()
    restarted = server_factory(settings_factory(platform_key_users=listed, onboarding_probe=False))
    owner = client_factory(restarted)
    r = owner.post("/api/auth/login", json={"email": "owner@example.com",
                                             "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    assert "platform" in _provider_ids(owner)
    r = owner.post("/api/agents", json={"name": "A", "model": "gpt-6.1-sol"})
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------
# #6: chatgpt mode ignores keys saved earlier in proxy mode
# --------------------------------------------------------------------------


def test_chatgpt_mode_leftover_own_key_unlocks_nothing(
    server_factory, settings_factory, client_factory
) -> None:
    server = server_factory(settings_factory(
        model_auth="chatgpt", platform_key_users=("operator@example.com",), onboarding_probe=False,
    ))
    c = client_factory(server)
    user = register(c)  # not on the list
    _insert_key(server, user["id"], "openai")
    assert providers.resolve_credentials(server.ctx, user["id"], "openai") is None
    assert providers.models_for(server.ctx, user["id"], "openai") == []
    r = c.post("/api/agents", json={"name": "B", "model": "gpt-6-luna", "provider": "openai"})
    assert r.status_code == 400, r.text

    # An agent created on that key while the server ran in proxy mode fails
    # its turns fast (resolve_credentials is None) with a message that fits.
    msg = providers.no_key_message(server.settings, "openai")
    assert "Settings" not in msg and "ChatGPT login" in msg


# --------------------------------------------------------------------------
# #7: chatgpt mode with an empty allow list says what to do
# --------------------------------------------------------------------------


def test_chatgpt_mode_empty_list_names_the_allow_list(
    server_factory, settings_factory, client_factory
) -> None:
    server = server_factory(settings_factory(
        model_auth="chatgpt", platform_key_users=(), onboarding_probe=False,
    ))
    c = client_factory(server)
    register(c)
    r = c.post("/api/agents", json={"name": "A", "model": "gpt-6-luna"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "DEN_PLATFORM_KEY_USERS" in detail and "Settings" not in detail


def test_chatgpt_mode_empty_list_logs_a_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="den.config"):
        config.load_settings({"DEN_RUNTIME": "local", "DEN_MODEL_AUTH": "chatgpt"})
    assert any("DEN_PLATFORM_KEY_USERS" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# #8: the private-network check resolves the name httpx connects to
# --------------------------------------------------------------------------


def _fake_dns(monkeypatch, table: dict[str, str]) -> list[str]:
    asked: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        asked.append(name)
        if name not in table:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[name], port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return asked


async def test_check_base_url_resolves_idna2008_name(monkeypatch) -> None:
    # The attacker's DNS: the IDNA 2003 spelling is public, the IDNA 2008 one
    # (what httpx connects to) is private.
    asked = _fake_dns(monkeypatch, {"evilss.example": "93.184.216.34",
                                    "xn--evil-yna.example": "127.0.0.1"})
    with pytest.raises(providers.ProviderInputError, match="private"):
        await providers.check_base_url("https://evilß.example:6379/v1", allow_private=False)
    assert asked == ["xn--evil-yna.example"]


async def test_check_base_url_refuses_invalid_idna(monkeypatch) -> None:
    # getaddrinfo would map this to "localhost"; httpx refuses the name, so
    # what gets checked could never be what gets connected to.
    _fake_dns(monkeypatch, {"localhost": "93.184.216.34"})
    with pytest.raises(providers.ProviderInputError, match="not valid"):
        await providers.check_base_url("https://ⓛocalhost/v1", allow_private=False)


async def test_check_base_url_still_accepts_public_names(monkeypatch) -> None:
    _fake_dns(monkeypatch, {"api.example.com": "93.184.216.34"})
    assert await providers.check_base_url("https://API.example.com/v1/", allow_private=False) == \
        "https://API.example.com/v1"


def test_proxy_rechecks_custom_url_before_each_call(
    server_factory, settings_factory, client_factory
) -> None:
    server = server_factory(settings_factory(allow_private_provider_urls=False,
                                             onboarding_probe=False))
    c = client_factory(server)
    user = register(c)
    # As if the name was public when saved and now points at loopback.
    _insert_key(server, user["id"], "custom", base_url="https://127.0.0.1:9/v1", models="gpt-6-luna")
    r = c.post("/api/agents", json={"name": "C", "model": "gpt-6-luna", "provider": "custom"})
    assert r.status_code == 201, r.text
    agent_id = r.json()["agent"]["id"]
    r = proxy(server, agent_token(server, agent_id), "POST", "responses",
              json=responses_body("hi"))
    assert r.status_code == 403
    assert "no longer allowed" in r.json()["error"]["message"]
    r = c.post("/api/providers/custom/key/test")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "private" in r.json()["message"]


# --------------------------------------------------------------------------
# #9: a key's model list cannot drop a model an agent runs on
# --------------------------------------------------------------------------


def test_removing_a_model_in_use_is_refused(
    server_factory, settings_factory, client_factory, fake_model
) -> None:
    server = server_factory(settings_factory(platform_key_users=(), onboarding_probe=False))
    c = client_factory(server)
    register(c)
    url = fake_model.url + "/v1"
    r = c.put("/api/providers/custom/key",
              json={"api_key": "custom-key-123", "base_url": url, "models": "gpt-6-luna"})
    assert r.status_code == 200, r.text
    r = c.post("/api/agents", json={"name": "Scout", "model": "gpt-6-luna", "provider": "custom"})
    assert r.status_code == 201, r.text
    wait_setup_settled(c, r.json()["agent"]["id"])

    r = c.put("/api/providers/custom/key", json={"models": "gpt-6.1-sol"})
    assert r.status_code == 400
    assert "'Scout' (gpt-6-luna)" in r.json()["detail"]
    stored = c.get("/api/providers").json()["providers"]
    custom = next(p for p in stored if p["id"] == "custom")
    assert custom["key"]["models"] == ["gpt-6-luna"]

    # Adding a model, or keeping the one in use, still saves.
    r = c.put("/api/providers/custom/key", json={"models": "gpt-6-luna,gpt-6.1-sol"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------
# #31: inline comments in .env
# --------------------------------------------------------------------------


def test_dotenv_strips_inline_comments_but_not_quoted_hashes(tmp_path, monkeypatch) -> None:
    names = ["UMGPT_TOOLKIT_API_KEY", "DEN_PLATFORM_KEY_USERS", "DEN_PLATFORM_LABEL",
             "DEN_TEST_HASH", "DEN_TEST_EMPTY"]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UMGPT_TOOLKIT_API_KEY=abc123fake                 # or OPENAI_API_KEY=...\n"
        "DEN_PLATFORM_KEY_USERS=you@example.com    # comma separated; * = everyone\n"
        'DEN_PLATFORM_LABEL="Lab # 3"   # quoted keeps its hash\n'
        "DEN_TEST_HASH=a#b\n"
        "DEN_TEST_EMPTY=   # nothing here\n"
    )
    try:
        config.load_dotenv(env_file)
        assert os.environ["UMGPT_TOOLKIT_API_KEY"] == "abc123fake"
        assert os.environ["DEN_PLATFORM_KEY_USERS"] == "you@example.com"
        assert os.environ["DEN_PLATFORM_LABEL"] == "Lab # 3"
        assert os.environ["DEN_TEST_HASH"] == "a#b"
        assert os.environ["DEN_TEST_EMPTY"] == ""
        settings = config.load_settings({k: os.environ[k] for k in names} | {"DEN_RUNTIME": "local"})
        assert settings.upstream_api_key == "abc123fake"
        assert settings.platform_key_users == ("you@example.com",)
    finally:
        for name in names:
            os.environ.pop(name, None)
