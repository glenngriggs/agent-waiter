"""The one test that proves real containers work. Opt-in: `pytest -m docker`.

It needs a working Docker daemon (on WSL: Docker Desktop with WSL integration
enabled for this distro; if your user is not in the docker group yet, run
`sg docker -c ".venv/bin/python -m pytest -m docker"`). The first run builds
the agent image, which can take several minutes.

Containers reach the backend at host.docker.internal; set DEN_TEST_AGENT_HOST
if your Docker setup needs a different address for the host.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from den.agent_files import agent_paths
from tests.conftest import (
    FAKE_UPSTREAM_KEY,
    agent_row,
    chat,
    create_ready_agent,
    history,
    item_events,
    jobs_of,
    make_settings,
    register,
    running_server,
    wait_for,
)

pytestmark = pytest.mark.docker


def docker(*args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def _docker_works() -> bool:
    try:
        return docker("info", check=False, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_docker() -> None:
    if not _docker_works():
        pytest.skip("`docker info` failed: no usable Docker daemon here")


def inspect(name: str) -> dict:
    return json.loads(docker("inspect", name).stdout)[0]


def test_real_container_end_to_end(fake_model, tmp_path, client_factory) -> None:
    settings = make_settings(tmp_path / "data", fake_model, runtime="docker", onboarding_probe=True)
    with running_server(settings) as server:
        c = client_factory(server)
        register(c)
        # Onboarding builds the image on first use, then probes through the container.
        agent = create_ready_agent(c, "Contained", timeout=1500)
        name = f"den-agent-{agent['id']}"
        assert agent["container"] == "running"

        # ---- The container has the documented limits ----
        info = inspect(name)
        host = info["HostConfig"]
        assert host["Memory"] == 2 * 1024**3
        assert host["NanoCpus"] == 1_000_000_000
        assert host["PidsLimit"] == 512
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["NetworkMode"] == "den-agents"
        assert not host.get("Privileged")
        assert info["Config"]["User"] == server.settings.container_uid_gid
        mounts = {m["Destination"]: m for m in info["Mounts"]}
        paths = agent_paths(server.settings, agent["id"])
        assert mounts["/workspace"]["Source"] == str(paths.workspace)
        assert mounts["/codex"]["Source"] == str(paths.codex_home)

        # ---- The upstream key never enters the container ----
        env = docker("exec", name, "env").stdout
        assert FAKE_UPSTREAM_KEY not in env
        for var in ("UMGPT_TOOLKIT_API_KEY", "OPENAI_API_KEY", "DEN_UPSTREAM_API_KEY"):
            assert var not in env
        assert "PLATFORM_TOKEN=den_" in env

        # ---- Chat through the container, the proxy and the fake model ----
        first = uuid.uuid4().hex[:10]
        done = chat(c, agent["id"], f"hello from docker {first}", timeout=180)
        assert done["data"]["status"] == "completed", done["data"]
        assert done["data"]["final_response"] == f"Echo: hello from docker {first}"

        # ---- MCP from inside the container ----
        done = chat(c, agent["id"], "SCHEDULE:30", timeout=180)
        assert done["data"]["status"] == "completed", done["data"]
        events = [e for e in history(c, agent["id"])["events"] if e["turn_id"] == done["turn_id"]]
        [call] = item_events(events, "mcp_tool_call")
        assert call["data"]["status"] == "completed"
        assert [j for j in jobs_of(c, agent["id"]) if j["created_by"] == "agent"]

        # ---- A file written in the container shows up in the Files API ----
        done = chat(c, agent["id"], "RUN:echo hi-from-container > from-container.txt", timeout=180)
        assert done["data"]["status"] == "completed", done["data"]
        r = c.get(f"/api/agents/{agent['id']}/files/preview", params={"path": "from-container.txt"})
        assert r.status_code == 200 and r.json()["text"] == "hi-from-container\n"
        # Files the agent writes belong to the backend's user, so it can serve and delete them.
        assert c.delete(f"/api/agents/{agent['id']}/files", params={"path": "from-container.txt"}).status_code == 204

        # ---- Stop, then Restart, and the conversation continues ----
        thread_id = agent_row(server, agent["id"])["chat_thread_id"]
        r = c.post(f"/api/agents/{agent['id']}/container/stop")
        assert r.status_code == 200, r.text
        assert r.json()["agent"]["container"] == "stopped"
        assert inspect(name)["State"]["Status"] == "exited"

        old_container_id = inspect(name)["Id"]
        r = c.post(f"/api/agents/{agent['id']}/container/restart")
        assert r.status_code == 200, r.text
        wait_for(lambda: inspect(name)["State"]["Status"] == "running", timeout=60, message="container up")
        assert inspect(name)["Id"] != old_container_id, "restart must recreate the container"

        second = uuid.uuid4().hex[:10]
        done = chat(c, agent["id"], f"still there {second}", timeout=180)
        assert done["data"]["status"] == "completed", done["data"]
        assert agent_row(server, agent["id"])["chat_thread_id"] == thread_id
        requests = fake_model.requests_containing(second)
        assert requests and all(first in r["raw"] for r in requests)

        # ---- Delete removes the container and the directory ----
        assert c.delete(f"/api/agents/{agent['id']}").status_code == 204
        gone = docker("inspect", name, check=False)
        assert gone.returncode != 0 and "no such object" in gone.stderr.lower()
        assert not paths.root.exists()


def test_stop_kills_a_command_running_inside_the_container(fake_model, tmp_path, client_factory) -> None:
    """Stop must end the agent's running command, not just the docker exec client.

    docs/codex-sdk.md saw a command survive an aborted `docker exec` (with the
    TypeScript SDK), so this checks the Python path end to end.
    """
    import time

    settings = make_settings(tmp_path / "data", fake_model, runtime="docker", onboarding_probe=False)
    with running_server(settings) as server:
        c = client_factory(server)
        register(c)
        agent = create_ready_agent(c, "Stopper", timeout=1500)
        name = f"den-agent-{agent['id']}"
        try:
            r = c.post(
                f"/api/agents/{agent['id']}/messages",
                json={"text": "RUN:sleep 12; touch /workspace/after-stop.txt"},
            )
            assert r.status_code == 202, r.text
            turn_id = r.json()["turn_id"]

            def command_started() -> bool:
                events = history(c, agent["id"])["events"]
                return any(
                    e["kind"] == "item_started" and e["data"].get("item_type") == "command"
                    for e in events
                    if e["turn_id"] == turn_id
                )

            wait_for(command_started, timeout=120, message="command started")
            assert c.post(f"/api/agents/{agent['id']}/stop").json()["stopped"] is True

            def turn_done() -> dict | None:
                for e in history(c, agent["id"])["events"]:
                    if e["kind"] == "turn_completed" and e["turn_id"] == turn_id:
                        return e
                return None

            done = wait_for(turn_done, timeout=30, message="turn completed")
            assert done["data"]["status"] == "interrupted", done["data"]

            time.sleep(16)  # longer than the command would have taken
            workspace = agent_paths(server.settings, agent["id"]).workspace
            assert not (workspace / "after-stop.txt").exists(), "Stop left the command running"
            ps = docker("exec", name, "ps", "-eo", "args").stdout
            assert "sleep 12" not in ps, ps
        finally:
            c.delete(f"/api/agents/{agent['id']}")
