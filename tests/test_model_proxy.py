"""The model proxy: agent token in, real (here: fake) upstream key out."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.conftest import (
    FAKE_UPSTREAM_KEY,
    agent_token,
    create_ready_agent,
    make_settings,
    new_client,
    register,
    running_server,
)
from tests.fake_model import free_port


def responses_body(text: str, model: str = "gpt-6-luna") -> dict:
    return {
        "model": model,
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
    }


def parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.strip().split("\n\n"):
        data = [line[len("data: ") :] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


@pytest.fixture(scope="module")
def server(fake_model, tmp_path_factory):
    settings = make_settings(tmp_path_factory.mktemp("proxy"), fake_model, onboarding_probe=False)
    with running_server(settings) as s:
        yield s


@pytest.fixture(scope="module")
def token(server):
    with new_client(server) as c:
        register(c)
        agent = create_ready_agent(c, "Proxied")
    return agent_token(server, agent["id"])


def proxy(server, token: str | None, method: str, path: str, **kwargs) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    if token is not None:
        headers["Authorization"] = f"Bearer {token}".strip()  # "" -> bare "Bearer"
    return httpx.request(method, f"{server.url}/model-proxy/v1/{path}", headers=headers, timeout=30, **kwargs)


def test_request_is_forwarded_with_the_upstream_key(server, token, fake_model) -> None:
    marker = uuid.uuid4().hex
    r = proxy(
        server,
        token,
        "POST",
        "responses",
        json=responses_body(f"hello {marker}"),
        headers={
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "x-codex-turn-metadata": "abc",
            "session_id": "sess-1",
            "Cookie": "den_session=should-not-travel",
            "X-Unlisted-Header": "nope",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")

    # The SSE body arrives intact: same events, same text.
    events = parse_sse(r.text)
    types = [e["type"] for e in events]
    assert types[0] == "response.created" and types[-1] == "response.completed"
    assert "response.output_item.added" in types and "response.output_item.done" in types
    deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    assert deltas == f"Echo: hello {marker}"

    [seen] = fake_model.requests_containing(marker)
    headers = seen["headers"]
    assert headers["authorization"] == f"Bearer {FAKE_UPSTREAM_KEY}"
    assert token not in json.dumps(headers), "the agent token must not reach the upstream"
    assert headers["openai-beta"] == "responses=experimental"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["x-codex-turn-metadata"] == "abc"
    assert headers["session_id"] == "sess-1"
    assert "cookie" not in headers
    assert "x-unlisted-header" not in headers
    assert seen["body"] == responses_body(f"hello {marker}")


def test_get_models_and_query_string_are_forwarded(server, token, fake_model) -> None:
    marker = uuid.uuid4().hex
    r = proxy(server, token, "GET", "models", params={"client_version": marker})
    assert r.status_code == 200
    assert {m["id"] for m in r.json()["data"]} == {"gpt-6.1-sol", "gpt-6-luna"}
    seen = [q for q in fake_model.requests if q["path"] == "/v1/models" and marker in q["query"]]
    assert len(seen) == 1
    assert seen[0]["headers"]["authorization"] == f"Bearer {FAKE_UPSTREAM_KEY}"


def test_upstream_errors_pass_through(server, token) -> None:
    r = proxy(server, token, "POST", "responses", json=responses_body("FAIL401"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.parametrize("bad", [None, "", "den_garbage", "sk-real-looking-key"])
def test_bad_or_missing_token_is_401(server, bad, fake_model) -> None:
    marker = uuid.uuid4().hex
    r = proxy(server, bad, "POST", "responses", json=responses_body(marker))
    assert r.status_code == 401
    assert fake_model.requests_containing(marker) == []


def test_model_not_offered_is_403(server, token, fake_model) -> None:
    marker = uuid.uuid4().hex
    r = proxy(server, token, "POST", "responses", json=responses_body(marker, model="gpt-9-ultra"))
    assert r.status_code == 403
    assert r.json()["error"]["message"] == "Model 'gpt-9-ultra' is not offered on this platform."
    assert fake_model.requests_containing(marker) == []


@pytest.mark.parametrize("path", ["files", "embeddings", "images/generations", "audio/speech", "fine_tuning/jobs"])
def test_paths_outside_the_allow_list_are_404(server, token, path) -> None:
    assert proxy(server, token, "POST", path, json={"model": "gpt-6-luna"}).status_code == 404
    assert proxy(server, token, "GET", path).status_code == 404


def test_responses_subpaths_are_allowed(server, token, fake_model) -> None:
    marker = uuid.uuid4().hex
    # The fake answers 404 for paths it does not script, but the request must
    # have been forwarded rather than refused by the proxy.
    proxy(server, token, "GET", f"responses/resp_{marker}")
    assert any(marker in q["path"] for q in fake_model.requests)


@pytest.mark.parametrize(
    "overrides",
    [{"upstream_api_key": None}, {"model_auth": "chatgpt"}],
    ids=["no upstream key", "chatgpt mode"],
)
def test_no_usable_upstream_key_is_502(fake_model, tmp_path, overrides) -> None:
    settings = make_settings(tmp_path / "data", fake_model, onboarding_probe=False, **overrides)
    with running_server(settings) as server, new_client(server) as c:
        register(c)
        agent = create_ready_agent(c, "Keyless")
        r = proxy(server, agent_token(server, agent["id"]), "POST", "responses", json=responses_body("hi"))
        assert r.status_code == 502
        assert r.json()["error"]["message"] == "No upstream API key is configured on the platform."


def test_unreachable_upstream_is_502(fake_model, tmp_path) -> None:
    dead = f"http://127.0.0.1:{free_port()}"  # nothing listens here
    settings = make_settings(tmp_path / "data", dead, onboarding_probe=False)
    with running_server(settings) as server, new_client(server) as c:
        register(c)
        agent = create_ready_agent(c, "Stranded")
        r = proxy(server, agent_token(server, agent["id"]), "POST", "responses", json=responses_body("hi"))
        assert r.status_code == 502
