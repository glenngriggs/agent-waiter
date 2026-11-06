"""Per-user API keys, providers, usage, and Claude through the Anthropic bridge.

The end-to-end tests run the real Codex app-server (local runtime) against
tests/fake_anthropic.py, so the whole translation is exercised by Codex
itself: tool calls, MCP tools, history replay and the onboarding probe.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from den import anthropic_bridge, providers
from tests import fake_anthropic
from tests.conftest import (
    DEFAULT_PASSWORD,
    new_client,
    register,
    chat,
    wait_setup_settled,
)

GOOD_KEY = fake_anthropic.VALID_KEY


# --------------------------------------------------------------------------
# Translation (no servers)
# --------------------------------------------------------------------------


def _msg(role: str, text: str) -> dict:
    kind = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def test_leading_developer_messages_become_the_system_prompt() -> None:
    params, _ = anthropic_bridge.translate_request({
        "model": "claude-sonnet-5-5",
        "input": [_msg("developer", "Be brief."), _msg("user", "hi"), _msg("developer", "Later note")],
    })
    assert params["system"] == "Be brief."
    assert params["messages"][0]["role"] == "user"
    texts = [b["text"] for b in params["messages"][0]["content"]]
    assert texts[0] == "hi" and "Later note" in texts[1]


def test_namespaced_tools_are_flattened_and_mapped_back() -> None:
    tools = [
        {"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {}}},
        {"type": "namespace", "name": "mcp__platform", "tools": [
            {"type": "function", "name": "schedule_job", "parameters": {"type": "object"}}]},
        {"type": "web_search"},
    ]
    out, tool_map = anthropic_bridge.translate_tools(tools, "claude-sonnet-5-5")
    assert [t["name"] for t in out] == ["exec_command", "mcp__platform__schedule_job"]
    assert tool_map.by_name["mcp__platform__schedule_job"] == {
        "kind": "function", "name": "schedule_job", "namespace": "mcp__platform"}
    assert tool_map.web_search


def test_unanswered_tool_calls_get_a_placeholder_result() -> None:
    params, _ = anthropic_bridge.translate_request({
        "model": "claude-sonnet-5-5",
        "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        "input": [
            _msg("user", "run it"),
            {"type": "function_call", "call_id": "c1", "name": "exec_command", "arguments": "{}"},
            _msg("user", "never mind"),
        ],
    })
    first_block = params["messages"][2]["content"][0]
    assert first_block["type"] == "tool_result" and first_block["tool_use_id"] == "c1"
    assert first_block["is_error"] is True


def test_calls_to_tools_no_longer_offered_become_text() -> None:
    # History from a GPT turn (the freeform exec tool) replayed to Claude.
    params, _ = anthropic_bridge.translate_request({
        "model": "claude-sonnet-5-5",
        "tools": [],
        "input": [
            _msg("user", "run it"),
            {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "text('x')"},
            {"type": "custom_tool_call_output", "call_id": "c1", "output": "x"},
            _msg("assistant", "done"),
            _msg("user", "thanks"),
        ],
    })
    flat = json.dumps(params["messages"])
    assert "tool_use" not in flat and "tool_result" not in flat
    assert "[Called tool exec" in flat and "[Tool result: x]" in flat


def test_bridge_reasoning_replays_the_reply_exactly() -> None:
    reply = [{"type": "thinking", "thinking": "hmm", "signature": "s1"},
             {"type": "text", "text": "Calling."},
             {"type": "tool_use", "id": "toolu_1", "name": "exec_command", "input": {"cmd": "ls"}}]
    blob = anthropic_bridge.encode_blob("claude-sonnet-5-5", [reply])
    params, _ = anthropic_bridge.translate_request({
        "model": "claude-sonnet-5-5",
        "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        "input": [
            _msg("user", "list files"),
            # Codex's own copies of the reply, in its order, plus the blob.
            _msg("assistant", "Calling."),
            {"type": "function_call", "call_id": "toolu_1", "name": "exec_command",
             "arguments": '{"cmd": "ls"}'},
            {"type": "reasoning", "summary": [], "encrypted_content": blob},
            {"type": "function_call_output", "call_id": "toolu_1", "output": "a.txt"},
        ],
    })
    assert params["messages"][1] == {"role": "assistant", "content": reply}
    assert params["messages"][2]["content"][0] == {
        "type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"}


def test_openai_reasoning_is_dropped_for_claude() -> None:
    params, _ = anthropic_bridge.translate_request({
        "model": "claude-sonnet-5-5",
        "input": [_msg("user", "hi"),
                  {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA-openai"},
                  _msg("assistant", "hello"), _msg("user", "again")],
    })
    assert "gAAAA" not in json.dumps(params)


def test_extras_are_added_for_new_models_only() -> None:
    params, tool_map = anthropic_bridge.translate_request(
        {"model": "claude-opus-5-5", "input": [_msg("user", "hi")], "tools": [{"type": "web_search"}]})
    extra, betas = anthropic_bridge.add_extras(params, tool_map)
    assert extra["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert extra["fallbacks"] == "default"
    assert extra["tools"][-1]["type"] == "web_search_20260209"
    assert anthropic_bridge.BLOCK_BINDING_BETA in betas
    assert "fallbacks" not in params  # the plain request stays plain


def test_keys_are_encrypted_and_hinted() -> None:
    secret = b"x" * 32
    token = providers.encrypt_key(secret, "sk-ant-abcdefghijklmnop")
    assert "abcdefghijklmnop" not in token
    assert providers.decrypt_key(secret, token) == "sk-ant-abcdefghijklmnop"
    assert providers.decrypt_key(b"y" * 32, token) is None
    assert providers.key_hint("sk-ant-abcdefghijklmnop") == "…mnop"


@pytest.mark.asyncio
async def test_private_base_urls_are_refused_unless_allowed() -> None:
    with pytest.raises(providers.ProviderInputError):
        await providers.check_base_url("https://127.0.0.1/v1", allow_private=False)
    with pytest.raises(providers.ProviderInputError):
        await providers.check_base_url("http://example.com/v1", allow_private=False)
    with pytest.raises(providers.ProviderInputError):
        await providers.check_base_url("https://user:pw@example.com/v1", allow_private=True)
    assert await providers.check_base_url("http://127.0.0.1:9/v1/", allow_private=True) == \
        "http://127.0.0.1:9/v1"


# --------------------------------------------------------------------------
# Live servers
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anthropic_fake():
    server = fake_anthropic.run_in_thread()
    yield server
    server.stop()


@pytest.fixture
def claude_server(server_factory, settings_factory, anthropic_fake):
    return server_factory(settings_factory(
        provider_base_urls={"anthropic": anthropic_fake.url, "openai": "http://127.0.0.1:9/v1"},
    ))


def _providers(client) -> dict:
    return {p["id"]: p for p in client.get("/api/providers").json()["providers"]}


def test_saving_a_key_never_returns_it(claude_server, client_factory) -> None:
    user = client_factory(claude_server)
    register(user)
    assert _providers(user)["anthropic"]["key"] is None
    bad = user.put("/api/providers/anthropic/key", json={"api_key": "sk-ant-wrong-key-000"})
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]

    r = user.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY})
    assert r.status_code == 200, r.text
    assert GOOD_KEY not in r.text
    entry = _providers(user)["anthropic"]
    assert entry["key"]["hint"] == "…" + GOOD_KEY[-4:]
    assert entry["available"] and entry["source"] == "own"
    assert GOOD_KEY not in user.get("/api/providers").text

    # Stored encrypted, not in the clear.
    db = sqlite3.connect(claude_server.settings.data_dir / "den.db")
    stored = db.execute("SELECT key_ciphertext FROM api_keys").fetchone()[0]
    assert GOOD_KEY not in stored

    assert user.post("/api/providers/anthropic/key/test").json()["ok"] is True
    assert user.delete("/api/providers/anthropic/key").status_code == 204
    assert _providers(user)["anthropic"]["key"] is None


def test_keys_are_per_user(claude_server, client_factory) -> None:
    ana, ben = client_factory(claude_server), client_factory(claude_server)
    register(ana)
    register(ben)
    assert ana.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY}).status_code == 200
    assert _providers(ben)["anthropic"]["key"] is None
    # Ben cannot create a Claude agent on Ana's key.
    r = ben.post("/api/agents", json={"name": "B", "model": "claude-sonnet-5-5"})
    assert r.status_code == 400 and "Settings" in r.json()["detail"]
    assert ben.delete("/api/providers/anthropic/key").status_code == 404
    assert _providers(ana)["anthropic"]["key"] is not None


def test_shared_key_is_off_unless_listed(server_factory, settings_factory, client_factory) -> None:
    server = server_factory(settings_factory(platform_key_users=("someone@else.edu",)))
    user = client_factory(server)
    register(user)
    assert "platform" not in _providers(user)
    r = user.post("/api/agents", json={"name": "A", "model": "gpt-6.1-sol"})
    assert r.status_code == 400 and "API key" in r.json()["detail"]


def test_unknown_provider_and_bad_models(claude_server, client_factory) -> None:
    user = client_factory(claude_server)
    register(user)
    assert user.put("/api/providers/nope/key", json={"api_key": "x"}).status_code == 404
    r = user.put("/api/providers/custom/key", json={"api_key": "abc", "base_url": "http://127.0.0.1:9/v1"})
    assert r.status_code == 400 and "model" in r.json()["detail"]
    r = user.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY, "models": "bad id"})
    assert r.status_code == 400


def test_password_change_signs_out_other_sessions(claude_server, client_factory) -> None:
    first = client_factory(claude_server)
    creds = register(first)
    second = new_client(claude_server)
    assert second.post("/api/auth/login", json={"email": creds["email"], "password": DEFAULT_PASSWORD}).status_code == 200
    r = first.post("/api/auth/password", json={"current_password": "wrong", "new_password": "x" * 10})
    assert r.status_code == 400
    r = first.post("/api/auth/password", json={"current_password": DEFAULT_PASSWORD, "new_password": "new password 1"})
    assert r.status_code == 204
    assert first.get("/api/auth/me").status_code == 200
    assert second.get("/api/auth/me").status_code == 401
    second.close()


def test_delete_account_removes_everything(claude_server, client_factory) -> None:
    user = client_factory(claude_server)
    creds = register(user)
    user.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY})
    agent = user.post("/api/agents", json={"name": "Gone", "model": "gpt-6-luna"}).json()["agent"]
    wait_setup_settled(user, agent["id"])
    assert user.post("/api/auth/delete-account", json={"password": "nope"}).status_code == 400
    assert user.post("/api/auth/delete-account", json={"password": DEFAULT_PASSWORD}).status_code == 204
    assert user.get("/api/auth/me").status_code == 401
    db = sqlite3.connect(claude_server.settings.data_dir / "den.db")
    assert db.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0
    assert not (claude_server.settings.data_dir / "agents" / agent["id"]).exists()
    login = new_client(claude_server)
    assert login.post("/api/auth/login", json={"email": creds["email"], "password": DEFAULT_PASSWORD}).status_code == 401
    login.close()


def test_claude_agent_end_to_end(claude_server, client_factory, anthropic_fake) -> None:
    """Onboarding, a shell tool call, an MCP tool call, and exact history replay."""
    user = client_factory(claude_server)
    register(user)
    assert user.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY}).status_code == 200
    r = user.post("/api/agents", json={"name": "Claudette", "model": "claude-sonnet-5-5"})
    assert r.status_code == 201, r.text
    agent = r.json()["agent"]
    assert agent["provider"] == "anthropic" and agent["provider_label"] == "Anthropic"
    agent = wait_setup_settled(user, agent["id"])
    assert agent["setup_state"] == "ready", agent["setup_detail"]

    done = chat(user, agent["id"], "RUN:echo from-claude > claude.txt")
    assert done["data"]["status"] == "completed", done
    workspace = Path(claude_server.settings.data_dir) / "agents" / agent["id"] / "workspace"
    assert (workspace / "claude.txt").read_text().strip() == "from-claude"
    assert done["data"]["final_response"] == "Ran it."

    done = chat(user, agent["id"], "SCHEDULE:30")
    assert done["data"]["status"] == "completed", done
    jobs = user.get(f"/api/agents/{agent['id']}/jobs").json()["jobs"]
    assert [j["name"] for j in jobs] == ["follow-up"]
    assert jobs[0]["created_by"] == "agent"

    done = chat(user, agent["id"], "hello again")
    assert done["data"]["final_response"] == "Echo: hello again"
    assert fake_anthropic.refused(anthropic_fake) == []

    # Codex sent Claude's own reply back (thinking block included), so the
    # replay really went through the reasoning item, not a reconstruction.
    calls = [r for r in anthropic_fake.requests if r["body"].get("model") == "claude-sonnet-5-5"]
    replayed = [
        r for r in calls
        if any(m["role"] == "assistant" and any(b.get("type") == "thinking" for b in m["content"])
               for m in r["body"]["messages"])
    ]
    assert replayed, "no request carried a replayed thinking block"

    # The key went to Anthropic, never into the agent's files.
    assert calls and all(GOOD_KEY in (r["headers"].get("x-api-key"), r["headers"].get("authorization", "")[7:])
                         for r in calls)
    agent_dir = Path(claude_server.settings.data_dir) / "agents" / agent["id"]
    for path in agent_dir.rglob("*"):
        if path.is_file() and path.stat().st_size < 5_000_000:
            assert GOOD_KEY.encode() not in path.read_bytes(), path

    usage = user.get("/api/usage").json()
    row = next(r for r in usage["by_model"] if r["model"] == "claude-sonnet-5-5")
    assert row["requests"] >= 4 and row["input_tokens"] > 0 and row["estimated_usd"] is not None


def test_removing_the_key_explains_the_failure(claude_server, client_factory) -> None:
    user = client_factory(claude_server)
    register(user)
    user.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY})
    agent = user.post("/api/agents", json={"name": "K", "model": "claude-haiku-5-5"}).json()["agent"]
    assert wait_setup_settled(user, agent["id"])["setup_state"] == "ready"
    assert user.delete("/api/providers/anthropic/key").status_code == 204
    done = chat(user, agent["id"], "hi")
    assert done["data"]["status"] == "failed"
    assert done["data"]["error_kind"] == "no_api_key"
    assert "Settings" in done["data"]["error_message"]


def test_extras_refused_falls_back_to_plain(server_factory, settings_factory, client_factory) -> None:
    strict = fake_anthropic.run_in_thread(reject_extras=True)
    try:
        server = server_factory(settings_factory(provider_base_urls={"anthropic": strict.url}))
        user = client_factory(server)
        register(user)
        user.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY})
        agent = user.post("/api/agents", json={"name": "P", "model": "claude-opus-5-5"}).json()["agent"]
        assert wait_setup_settled(user, agent["id"])["setup_state"] == "ready"
        done = chat(user, agent["id"], "hi there")
        assert done["data"]["final_response"] == "Echo: hi there"
        betas = [r["headers"].get("anthropic-beta") for r in strict.requests]
        assert betas[0] and betas[-1] is None  # tried with extras once, then plain
    finally:
        strict.stop()


def test_switching_an_agent_between_providers(claude_server, client_factory, fake_model) -> None:
    user = client_factory(claude_server)
    register(user)
    user.put("/api/providers/anthropic/key", json={"api_key": GOOD_KEY})
    agent = user.post("/api/agents", json={"name": "S", "model": "claude-sonnet-5-5"}).json()["agent"]
    assert wait_setup_settled(user, agent["id"])["setup_state"] == "ready"
    assert chat(user, agent["id"], "first")["data"]["final_response"] == "Echo: first"
    r = user.patch(f"/api/agents/{agent['id']}", json={"model": "gpt-6-luna"})
    assert r.status_code == 200 and r.json()["agent"]["provider"] == "platform"
    marker = f"second-{time.time_ns()}"
    done = chat(user, agent["id"], marker)
    assert done["data"]["final_response"] == f"Echo: {marker}"
    # The OpenAI-side request carried none of Claude's replay items.
    sent = fake_model.requests_containing(marker)
    assert sent and anthropic_bridge.BLOB_PREFIX not in sent[-1]["raw"]
