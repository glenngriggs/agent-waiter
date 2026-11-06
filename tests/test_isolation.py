"""Two users: nobody reaches another user's agent, even by editing a URL.

Every route under /api/agents/{agent_id} is enumerated below and must answer
404 (not 403) for another user's agent, without changing anything. Ids of
jobs and runs from another agent also answer 404, and agent tokens only reach
their own agent's jobs over MCP.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from den.agent_files import agent_paths
from den.db import new_id
from den.security import make_agent_token
from tests.conftest import (
    agent_row,
    agent_token,
    create_ready_agent,
    jobs_of,
    make_settings,
    mcp_request,
    mcp_tool,
    new_client,
    register,
    running_server,
    tool_text,
    wait_for,
)

NOT_FOUND = "Agent not found"
JOB_BODY = {"name": "Nightly", "prompt": "Summarize notes.txt", "schedule": {"kind": "interval", "minutes": 60}}

# (method, path template, request kwargs). {a} is the agent id, {j} a job id,
# {r} a run id. Together these are every route under /api/agents/{agent_id}.
AGENT_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/api/agents/{a}", {}),
    ("PATCH", "/api/agents/{a}", {"json": {"name": "Hijacked"}}),
    ("DELETE", "/api/agents/{a}", {}),
    ("POST", "/api/agents/{a}/setup/retry", {}),
    ("POST", "/api/agents/{a}/container/start", {}),
    ("POST", "/api/agents/{a}/container/stop", {}),
    ("POST", "/api/agents/{a}/container/restart", {}),
    ("POST", "/api/agents/{a}/conversation/new", {}),
    ("GET", "/api/agents/{a}/history", {}),
    ("GET", "/api/agents/{a}/events", {"params": {"after": 0}}),
    ("POST", "/api/agents/{a}/messages", {"json": {"text": "hello"}}),
    ("POST", "/api/agents/{a}/stop", {}),
    ("DELETE", "/api/agents/{a}/queue/{q}", {}),
    ("DELETE", "/api/agents/{a}/queue", {}),
    ("GET", "/api/agents/{a}/files", {"params": {"path": ""}}),
    ("GET", "/api/agents/{a}/files/preview", {"params": {"path": "secret.txt"}}),
    ("GET", "/api/agents/{a}/files/download", {"params": {"path": "secret.txt"}}),
    ("POST", "/api/agents/{a}/files/upload", {"params": {"path": ""}, "files": {"files": ("x.txt", b"x")}}),
    ("POST", "/api/agents/{a}/files/mkdir", {"json": {"path": "newdir"}}),
    ("DELETE", "/api/agents/{a}/files", {"params": {"path": "secret.txt"}}),
    ("GET", "/api/agents/{a}/jobs", {}),
    ("POST", "/api/agents/{a}/jobs", {"json": JOB_BODY}),
    ("DELETE", "/api/agents/{a}/jobs/{j}", {}),
    ("POST", "/api/agents/{a}/jobs/{j}/run", {}),
    ("GET", "/api/agents/{a}/jobs/{j}/runs", {}),
    ("GET", "/api/agents/{a}/runs/{r}", {}),
]

# Routes that take a job or run id: B's own agent with A's ids must 404 too.
CHILD_ROUTES = [route for route in AGENT_ROUTES if any(k in route[1] for k in ("{j}", "{r}", "{q}"))]


class _Answer:
    """Status and JSON of a response, read without waiting on an endless stream."""

    def __init__(self, status_code: int, body: bytes, content_type: str) -> None:
        self.status_code = status_code
        self.text = body.decode("utf-8", errors="replace")
        self._content_type = content_type

    def json(self) -> Any:
        return json.loads(self.text)


def _call(client, method: str, template: str, kwargs: dict[str, Any], **ids: str) -> _Answer:
    # Streamed so that a wrongly successful SSE route (which never ends) fails
    # the test instead of hanging it.
    with client.stream(method, template.format(**ids), **kwargs) as r:
        content_type = r.headers.get("content-type", "")
        body = b"" if content_type.startswith("text/event-stream") else r.read()
        return _Answer(r.status_code, body, content_type)


@pytest.fixture(scope="module")
def world(fake_model, tmp_path_factory):
    """User A with a ready agent, a job, a run and a file; user B with an agent."""
    settings = make_settings(tmp_path_factory.mktemp("iso"), fake_model, onboarding_probe=False)
    with running_server(settings) as server:
        with new_client(server) as a, new_client(server) as b:
            register(a)
            register(b)
            agent_a = create_ready_agent(a, "Ana's agent")
            agent_b = create_ready_agent(b, "Ben's agent")
            r = a.post(
                f"/api/agents/{agent_a['id']}/files/upload",
                params={"path": ""},
                files={"files": ("secret.txt", b"Ana's secret")},
            )
            assert r.status_code == 201, r.text
            r = a.post(f"/api/agents/{agent_a['id']}/jobs", json=JOB_BODY)
            assert r.status_code == 201, r.text
            job_a = r.json()["job"]
            r = a.post(f"/api/agents/{agent_a['id']}/jobs/{job_a['id']}/run")
            assert r.status_code == 202, r.text
            run_a = r.json()["run"]
            # A busy chat with one queued message, for the queue routes.
            assert a.post(f"/api/agents/{agent_a['id']}/messages", json={"text": "SLOW keep busy"}).status_code == 202
            r = a.post(f"/api/agents/{agent_a['id']}/messages", json={"text": "queued for Ana"})
            assert r.status_code == 202 and r.json()["queued"], r.text
            queued_a = r.json()["turn_id"]
            yield {
                "server": server,
                "settings": server.settings,
                "a": a,
                "b": b,
                "agent_a": agent_a,
                "agent_b": agent_b,
                "job_a": job_a,
                "run_a": run_a,
                "queued_a": queued_a,
            }


def _ids(world: dict, agent_key: str = "agent_a") -> dict[str, str]:
    return {
        "a": world[agent_key]["id"], "j": world["job_a"]["id"],
        "r": world["run_a"]["id"], "q": world["queued_a"],
    }


@pytest.mark.parametrize("method,template,kwargs", AGENT_ROUTES, ids=[f"{m} {t}" for m, t, _ in AGENT_ROUTES])
def test_other_users_agent_is_404_on_every_route(world, method, template, kwargs) -> None:
    r = _call(world["b"], method, template, kwargs, **_ids(world))
    assert r.status_code == 404, f"{method} {template}: {r.status_code} {r.text[:200]}"
    assert r.json()["detail"] == NOT_FOUND


@pytest.mark.parametrize("method,template,kwargs", AGENT_ROUTES, ids=[f"{m} {t}" for m, t, _ in AGENT_ROUTES])
def test_missing_agent_looks_exactly_like_someone_elses(world, method, template, kwargs) -> None:
    ids = {**_ids(world), "a": new_id()}
    r = _call(world["b"], method, template, kwargs, **ids)
    assert r.status_code == 404
    assert r.json()["detail"] == NOT_FOUND


@pytest.mark.parametrize("method,template,kwargs", CHILD_ROUTES, ids=[f"{m} {t}" for m, t, _ in CHILD_ROUTES])
def test_own_agent_with_another_agents_job_or_run_id_is_404(world, method, template, kwargs) -> None:
    r = _call(world["b"], method, template, kwargs, **_ids(world, "agent_b"))
    assert r.status_code == 404, f"{method} {template}: {r.status_code} {r.text[:200]}"


def test_nothing_of_a_changed_after_b_tried_everything(world) -> None:
    # Runs after the parametrized attacks above (pytest keeps file order).
    a, agent_a = world["a"], world["agent_a"]
    agent = a.get(f"/api/agents/{agent_a['id']}").json()["agent"]
    assert agent["name"] == "Ana's agent"
    assert agent["setup_state"] == "ready"
    workspace = agent_paths(world["settings"], agent_a["id"]).workspace
    assert (workspace / "secret.txt").read_bytes() == b"Ana's secret"
    assert not (workspace / "x.txt").exists()
    assert not (workspace / "newdir").exists()
    jobs = jobs_of(a, agent_a["id"])
    assert [j["id"] for j in jobs] == [world["job_a"]["id"]]
    assert jobs[0]["status"] == "active"


def test_other_users_agents_are_not_listed(world) -> None:
    listed_b = [x["id"] for x in world["b"].get("/api/agents").json()["agents"]]
    assert listed_b == [world["agent_b"]["id"]]
    listed_a = [x["id"] for x in world["a"].get("/api/agents").json()["agents"]]
    assert listed_a == [world["agent_a"]["id"]]


# --------------------------------------------------------------------------
# Agent tokens over MCP
# --------------------------------------------------------------------------


def test_agent_token_only_reaches_its_own_jobs(world) -> None:
    server, agent_a, agent_b = world["server"], world["agent_a"], world["agent_b"]
    token_a, token_b = agent_token(server, agent_a["id"]), agent_token(server, agent_b["id"])

    created = mcp_tool(server, token_b, "schedule_job", {"name": "B's own", "prompt": "p", "every_minutes": 30})
    assert not created.get("isError"), tool_text(created)
    job_b = next(j for j in jobs_of(world["b"], agent_b["id"]) if j["name"] == "B's own")

    listed = mcp_tool(server, token_a, "list_jobs", {})
    assert job_b["id"] not in tool_text(listed)

    refused = mcp_tool(server, token_a, "cancel_job", {"job_id": job_b["id"]})
    assert refused.get("isError") is True
    assert next(j for j in jobs_of(world["b"], agent_b["id"]) if j["id"] == job_b["id"])["status"] == "active"

    # The other way round: B's token cannot cancel A's job either.
    refused = mcp_tool(server, token_b, "cancel_job", {"job_id": world["job_a"]["id"]})
    assert refused.get("isError") is True


def test_token_with_a_version_that_does_not_match_is_rejected(world) -> None:
    server, agent_b = world["server"], world["agent_b"]
    row = agent_row(server, agent_b["id"])
    forged = make_agent_token(server.ctx.secret, agent_b["id"], int(row["token_version"]) + 1)
    assert mcp_request(server, forged, "tools/list").status_code == 401


def test_restart_bumps_the_version_and_revokes_the_old_token(world) -> None:
    server, b, agent_b = world["server"], world["b"], world["agent_b"]
    old = agent_token(server, agent_b["id"])
    old_version = agent_row(server, agent_b["id"])["token_version"]
    assert mcp_request(server, old, "tools/list").status_code == 200

    r = b.post(f"/api/agents/{agent_b['id']}/container/restart")
    assert r.status_code == 200, r.text
    wait_for(lambda: agent_row(server, agent_b["id"])["token_version"] == old_version + 1,
             timeout=10, message="token_version bump")

    assert mcp_request(server, old, "tools/list").status_code == 401
    new = agent_token(server, agent_b["id"])
    assert mcp_request(server, new, "tools/list").status_code == 200
    # The model proxy applies the same check.
    proxied = b.get("/model-proxy/v1/models", headers={"Authorization": f"Bearer {old}"})
    assert proxied.status_code == 401


def test_every_agent_route_is_in_the_table() -> None:
    """A new route under /api/agents/{agent_id} must be added to AGENT_ROUTES,
    so the two-user checks above cover it; this fails until it is."""
    from den.app import create_app

    def normalize(path: str) -> str:
        # Map each route's parameter names to the table's placeholders.
        return (path.replace("{agent_id}", "{a}").replace("{job_id}", "{j}")
                .replace("{run_id}", "{r}").replace("{turn_id}", "{q}")
                .replace("/container/{action}", "/container/start"))

    # The OpenAPI schema lists every route, including those in included
    # routers (which this FastAPI version does not flatten into app.routes).
    actual = set()
    for path, ops in create_app().openapi()["paths"].items():
        if path.startswith("/api/agents/{agent_id}"):
            actual |= {(method.upper(), normalize(path)) for method in ops}
    assert len(actual) > 20, "route discovery found too few routes; the check itself is broken"
    listed = {(m, t) for m, t, _ in AGENT_ROUTES}
    assert actual - listed == set(), f"agent routes missing from AGENT_ROUTES: {sorted(actual - listed)}"
