"""The platform MCP endpoint, called over HTTP the way Codex calls it."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from den.db import iso, parse_iso, utcnow
from den.security import make_agent_token
from tests.conftest import (
    agent_token,
    create_ready_agent,
    jobs_of,
    make_settings,
    mcp_request,
    mcp_tool,
    register,
    running_server,
    tool_text,
)


@pytest.fixture(scope="module")
def server(fake_model, tmp_path_factory):
    settings = make_settings(tmp_path_factory.mktemp("mcp"), fake_model, onboarding_probe=False)
    with running_server(settings) as s:
        yield s


@pytest.fixture
def agent(server, client_factory):
    """A fresh user (in Detroit) with a ready agent; returns (client, agent, token)."""
    c = client_factory(server)
    register(c, timezone="America/Detroit")
    a = create_ready_agent(c, "Planner")
    return c, a, agent_token(server, a["id"])


def _job_named(c, agent_id: str, name: str) -> dict:
    matches = [j for j in jobs_of(c, agent_id) if j["name"] == name]
    assert len(matches) == 1, [j["name"] for j in jobs_of(c, agent_id)]
    return matches[0]


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26"])
def test_initialize_echoes_the_protocol_version(server, agent, version) -> None:
    _, _, token = agent
    r = mcp_request(
        server,
        token,
        "initialize",
        {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "codex", "version": "0"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    result = body["result"]
    assert result["protocolVersion"] == version
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "den-platform"


def test_notifications_get_202_and_no_body(server, agent) -> None:
    _, _, token = agent
    r = mcp_request(server, token, "notifications/initialized", request_id=None)
    assert r.status_code == 202
    assert r.content == b""


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_are_405(server, agent, method) -> None:
    _, _, token = agent
    r = httpx.request(method, server.url + "/mcp", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 405


def test_ping(server, agent) -> None:
    _, _, token = agent
    assert mcp_request(server, token, "ping").json()["result"] == {}


def test_tools_list_has_the_three_tools(server, agent) -> None:
    _, _, token = agent
    tools = mcp_request(server, token, "tools/list").json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["cancel_job", "list_jobs", "schedule_job"]
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
    schedule = next(t for t in tools if t["name"] == "schedule_job")
    assert set(schedule["inputSchema"]["properties"]) >= {
        "name", "prompt", "in_minutes", "at", "every_minutes", "cron", "timezone",
    }


def test_unknown_method_is_32601(server, agent) -> None:
    _, _, token = agent
    assert mcp_request(server, token, "resources/list").json()["error"]["code"] == -32601


def test_batch_arrays_are_32600(server, agent) -> None:
    _, _, token = agent
    r = httpx.post(
        server.url + "/mcp",
        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.json()["error"]["code"] == -32600


def test_unknown_tool_or_bad_params_are_errors(server, agent) -> None:
    _, _, token = agent
    r = mcp_request(server, token, "tools/call", {"name": "rm_rf", "arguments": {}})
    body = r.json()
    assert ("error" in body and body["error"]["code"] in (-32601, -32602)) or body["result"].get("isError")
    r = mcp_request(server, token, "tools/call", {"arguments": {}})
    body = r.json()
    assert ("error" in body and body["error"]["code"] == -32602) or body["result"].get("isError")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def test_missing_token_is_401(server) -> None:
    assert mcp_request(server, None, "tools/list").status_code == 401


@pytest.mark.parametrize("token", ["", "nonsense", "den_" + "0" * 32 + "_1_" + "0" * 64])
def test_bad_token_is_401(server, token) -> None:
    r = mcp_request(server, token, "tools/list")
    assert r.status_code == 401
    assert "error" in r.json()


def test_token_signed_with_another_secret_is_401(server, agent) -> None:
    _, a, _ = agent
    forged = make_agent_token(b"not the server secret".ljust(32, b"!"), a["id"], 1)
    assert mcp_request(server, forged, "tools/list").status_code == 401


def test_non_bearer_authorization_is_401(server, agent) -> None:
    _, _, token = agent
    r = httpx.post(
        server.url + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Basic {token}"},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------
# schedule_job
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_fields,kind",
    [
        (lambda: {"in_minutes": 30}, "once"),
        (lambda: {"at": iso(utcnow() + timedelta(days=2))}, "once"),
        (lambda: {"every_minutes": 60}, "interval"),
        (lambda: {"cron": "0 9 * * 1-5"}, "cron"),
    ],
    ids=["in_minutes", "at", "every_minutes", "cron"],
)
def test_schedule_job_creates_an_agent_job_the_user_sees(server, agent, make_fields, kind) -> None:
    c, a, token = agent
    fields = make_fields()
    name = f"via {next(iter(fields))}"
    result = mcp_tool(server, token, "schedule_job", {"name": name, "prompt": "Water the plants.", **fields})
    assert not result.get("isError"), tool_text(result)
    text = tool_text(result)
    assert text.startswith("Scheduled job ") and name in text and "Next run:" in text

    job = _job_named(c, a["id"], name)
    assert job["created_by"] == "agent"
    assert job["status"] == "active"
    assert job["schedule"]["kind"] == kind
    assert job["schedule"]["timezone"] == "America/Detroit"  # the owner's timezone
    assert job["next_run_at"]
    assert job["id"] in text


def test_in_minutes_is_now_plus_n(server, agent) -> None:
    c, a, token = agent
    before = utcnow()
    mcp_tool(server, token, "schedule_job", {"name": "soon", "prompt": "p", "in_minutes": 30})
    next_run = parse_iso(_job_named(c, a["id"], "soon")["next_run_at"])
    assert before + timedelta(minutes=29) < next_run < utcnow() + timedelta(minutes=31)


def test_schedule_job_takes_an_explicit_timezone(server, agent) -> None:
    c, a, token = agent
    mcp_tool(server, token, "schedule_job", {"name": "tz", "prompt": "p", "cron": "0 8 * * *", "timezone": "Europe/Paris"})
    assert _job_named(c, a["id"], "tz")["schedule"]["timezone"] == "Europe/Paris"


@pytest.mark.parametrize(
    "fields",
    [
        {"in_minutes": 5, "every_minutes": 60},
        {"cron": "0 9 * * *", "at": "2030-01-01T09:00:00"},
        {},
        {"every_minutes": 0},
        {"in_minutes": 0},
        {"cron": "not a cron"},
        {"at": "2001-01-01T00:00:00Z"},
        {"every_minutes": 60, "timezone": "Mars/Base"},
    ],
)
def test_invalid_schedules_are_tool_errors_the_model_can_read(server, agent, fields) -> None:
    c, a, token = agent
    result = mcp_tool(server, token, "schedule_job", {"name": "bad", "prompt": "p", **fields})
    assert result.get("isError") is True
    assert tool_text(result).strip()
    assert [j for j in jobs_of(c, a["id"]) if j["name"] == "bad"] == []


def test_schedule_job_needs_a_name_and_prompt(server, agent) -> None:
    _, _, token = agent
    assert mcp_tool(server, token, "schedule_job", {"prompt": "p", "every_minutes": 60}).get("isError")
    assert mcp_tool(server, token, "schedule_job", {"name": "n", "every_minutes": 60}).get("isError")


# --------------------------------------------------------------------------
# list_jobs and cancel_job
# --------------------------------------------------------------------------


def test_list_jobs_shows_only_this_agents_active_jobs(server, agent) -> None:
    c, a, token = agent
    other = create_ready_agent(c, "Other")
    other_token = agent_token(server, other["id"])
    mcp_tool(server, token, "schedule_job", {"name": "mine", "prompt": "p", "every_minutes": 60})
    mcp_tool(server, other_token, "schedule_job", {"name": "theirs", "prompt": "p", "every_minutes": 60})
    gone = mcp_tool(server, token, "schedule_job", {"name": "gone", "prompt": "p", "every_minutes": 60})
    assert not gone.get("isError")
    gone_id = _job_named(c, a["id"], "gone")["id"]
    mcp_tool(server, token, "cancel_job", {"job_id": gone_id})

    text = tool_text(mcp_tool(server, token, "list_jobs", {}))
    data = json.loads(text)
    jobs = data if isinstance(data, list) else data["jobs"]
    names = sorted(j["name"] for j in jobs)
    assert names == ["mine"]
    [job] = jobs
    assert job["id"] == _job_named(c, a["id"], "mine")["id"]
    assert job["created_by"] == "agent"
    assert job["next_run_at"]
    assert "Every 60 minutes" in json.dumps(job)


def test_agent_can_cancel_its_own_job(server, agent) -> None:
    c, a, token = agent
    mcp_tool(server, token, "schedule_job", {"name": "temp", "prompt": "p", "every_minutes": 60})
    job_id = _job_named(c, a["id"], "temp")["id"]
    result = mcp_tool(server, token, "cancel_job", {"job_id": job_id})
    assert not result.get("isError"), tool_text(result)
    assert tool_text(result) == f"Cancelled job {job_id}."
    job = _job_named(c, a["id"], "temp")
    assert job["status"] == "cancelled" and job["next_run_at"] is None


def test_agent_cannot_cancel_a_job_the_user_created(server, agent) -> None:
    c, a, token = agent
    r = c.post(
        f"/api/agents/{a['id']}/jobs",
        json={"name": "user job", "prompt": "p", "schedule": {"kind": "interval", "minutes": 60}},
    )
    assert r.status_code == 201, r.text
    job_id = r.json()["job"]["id"]
    result = mcp_tool(server, token, "cancel_job", {"job_id": job_id})
    assert result.get("isError") is True
    assert "Only the user can cancel" in tool_text(result)
    assert _job_named(c, a["id"], "user job")["status"] == "active"


def test_cancel_of_an_unknown_job_is_a_tool_error(server, agent) -> None:
    _, _, token = agent
    result = mcp_tool(server, token, "cancel_job", {"job_id": "0" * 32})
    assert result.get("isError") is True
