"""DockerRuntime without a Docker daemon.

`run_args` is checked flag by flag, and the lifecycle methods run against a
fake `docker` executable placed first on PATH. The fake logs every call and
answers `inspect` from a JSON state file, so the exact command lines can be
asserted.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from den.agent_files import agent_paths, init_agent_dirs
from den.config import load_settings
from den.db import Database, iso, new_id, utcnow
from den.runtime.base import AgentRuntimeError
from den.runtime.docker import DockerRuntime
from den.security import make_agent_token

SECRET = b"d" * 32
UPSTREAM_SECRET = "sk-upstream-must-never-reach-a-container"

FAKE_DOCKER = r'''#!{python}
"""Fake docker CLI for tests: logs argv, answers from a JSON state file."""
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
state_path = os.environ["FAKE_DOCKER_STATE"]
with open(state_path) as fh:
    state = json.load(fh)
containers = state.setdefault("containers", {})


def save():
    with open(state_path, "w") as fh:
        json.dump(state, fh)


def done(out=""):
    if out:
        print(out)
    sys.exit(0)


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


sub = args[0] if args else ""
key = " ".join(args[:2]) if sub in ("image", "network", "container") else sub
forced = state.get("fail", {}).get(key)
if forced:
    sys.stderr.write(forced["stderr"])
    sys.exit(forced.get("code", 1))

if sub == "info":
    done("Server Version: fake")
if key == "image inspect":
    done("[]") if args[2] in state.get("images", []) else fail(f"Error: No such image: {args[2]}")
if key == "network inspect":
    done("[]") if args[2] in state.get("networks", []) else fail(f"Error: No such network: {args[2]}")
if key == "network create":
    state.setdefault("networks", []).append(args[-1]); save(); done("netid")
if sub == "build":
    state.setdefault("images", []).append(args[args.index("-t") + 1]); save(); done()
if sub == "inspect" or key == "container inspect":
    name = args[-1]
    done(containers[name]) if name in containers else fail(f"Error: No such object: {name}")
if sub == "run":
    name = args[args.index("--name") + 1]
    if name in containers:
        fail(f'docker: Error response from daemon: Conflict. The container name "/{name}" is already in use.', 125)
    containers[name] = "running"; save(); done("0123456789ab")
if sub in ("start", "unpause", "stop", "rm"):
    name = args[-1]
    if name not in containers:
        fail(f"Error response from daemon: No such container: {name}")
    if sub == "rm":
        del containers[name]
    else:
        containers[name] = "exited" if sub == "stop" else "running"
    save(); done(name)
fail(f"fake docker: unsupported command {args}", 2)
'''


class FakeDocker:
    def __init__(self, log: Path, state: Path) -> None:
        self.log = log
        self.state = state
        self.set()

    def set(self, *, containers=None, images=None, networks=None, fail=None) -> None:
        self.state.write_text(
            json.dumps(
                {
                    "containers": containers or {},
                    "images": images or [],
                    "networks": networks or [],
                    "fail": fail or {},
                }
            )
        )
        self.log.write_text("")

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def subcommands(self) -> list[str]:
        return [c[0] for c in self.calls()]

    def containers(self) -> dict[str, str]:
        return json.loads(self.state.read_text())["containers"]


@pytest.fixture
def fake_docker(tmp_path, monkeypatch) -> FakeDocker:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "docker"
    script.write_text(FAKE_DOCKER.replace("{python}", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state.json"))
    return FakeDocker(tmp_path / "docker.log", tmp_path / "docker-state.json")


def make_runtime(tmp_path: Path, **overrides):
    settings = load_settings(
        {
            "DEN_RUNTIME": "docker",
            "DEN_DATA_DIR": str(tmp_path / "data"),
            "DEN_UPSTREAM_BASE_URL": "https://upstream.example/v1",
            "DEN_UPSTREAM_API_KEY": UPSTREAM_SECRET,
            "DEN_CHATGPT_AUTH_JSON": str(tmp_path / "operator-auth.json"),
            "DEN_CONTAINER_USER": "1234:5678",
        }
    )
    settings = dataclasses.replace(settings, **overrides)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.data_dir / "den.db")
    db.migrate()
    now = iso(utcnow())
    user_id, agent_id = new_id(), new_id()
    db.execute(
        "INSERT INTO users (id, email, password_hash, timezone, created_at) VALUES (?,?,?,?,?)",
        (user_id, "ops@example.com", "x", "UTC", now),
    )
    db.execute(
        "INSERT INTO agents (id, user_id, name, model, setup_state, token_version, created_at, "
        "last_active_at) VALUES (?,?,?,?,?,?,?,?)",
        (agent_id, user_id, "Boxed", "gpt-6-luna", "ready", 3, now, now),
    )
    init_agent_dirs(agent_paths(settings, agent_id))
    agent = db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
    return settings, db, DockerRuntime(settings, SECRET, db), agent


@pytest.fixture
def rt(tmp_path):
    settings, db, runtime, agent = make_runtime(tmp_path)
    yield settings, db, runtime, agent
    db.close()


def pairs(args: list[str]) -> set[tuple[str, str]]:
    return set(zip(args, args[1:]))


# --------------------------------------------------------------------------
# run_args and the launch command (pure)
# --------------------------------------------------------------------------


def test_run_args_have_every_isolation_flag(rt) -> None:
    settings, _, runtime, agent = rt
    args = runtime.run_args(agent)
    name = f"den-agent-{agent['id']}"
    paths = agent_paths(settings, agent["id"])
    token = make_agent_token(SECRET, agent["id"], 3)

    assert args[:2] == ["run", "-d"]
    assert args[-3:] == [settings.agent_image, "sleep", "infinity"]
    expected_pairs = {
        ("--name", name),
        ("--label", "den.managed=1"),
        ("--label", f"den.agent_id={agent['id']}"),
        ("--user", "1234:5678"),
        ("--memory", "2g"),
        ("--memory-swap", "2g"),
        ("--cpus", "1.0"),
        ("--pids-limit", "512"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--network", "den-agents"),
        ("--add-host", "host.docker.internal:host-gateway"),
        ("-e", "CODEX_HOME=/codex"),
        ("-e", "HOME=/home/agent"),
        ("-e", f"PLATFORM_TOKEN={token}"),
        ("--mount", f"type=bind,src={paths.workspace},dst=/workspace"),
        ("--mount", f"type=bind,src={paths.codex_home},dst=/codex"),
        ("-w", "/workspace"),
    }
    missing = expected_pairs - pairs(args)
    assert not missing, f"missing from docker run: {sorted(missing)}"
    assert "--privileged" not in args
    assert not any(a.startswith("--cap-add") for a in args)


def test_run_args_follow_the_limit_settings(tmp_path) -> None:
    settings, db, runtime, agent = make_runtime(
        tmp_path, container_memory="512m", container_cpus="0.5", container_pids=64
    )
    got = pairs(runtime.run_args(agent))
    assert {("--memory", "512m"), ("--memory-swap", "512m"), ("--cpus", "0.5"), ("--pids-limit", "64")} <= got
    db.close()


def test_run_args_never_carry_the_upstream_key(rt) -> None:
    _, _, runtime, agent = rt
    joined = " ".join(runtime.run_args(agent))
    assert UPSTREAM_SECRET not in joined
    for name in ("UMGPT_TOOLKIT_API_KEY", "OPENAI_API_KEY", "DEN_UPSTREAM_API_KEY", "CODEX_API_KEY"):
        assert name not in joined


def test_auth_json_is_mounted_read_only_only_in_chatgpt_mode(rt, tmp_path) -> None:
    _, _, proxy_runtime, agent = rt
    assert not any("auth.json" in a for a in proxy_runtime.run_args(agent))

    settings, db, runtime, chat_agent = make_runtime(tmp_path / "chatgpt", model_auth="chatgpt")
    mounts = [b for a, b in pairs(runtime.run_args(chat_agent)) if a == "--mount"]
    auth = [m for m in mounts if "auth.json" in m]
    assert auth == [f"type=bind,src={settings.chatgpt_auth_json},dst=/codex/auth.json,readonly"]
    db.close()


def test_codex_config_launches_app_server_through_docker_exec(rt) -> None:
    _, _, runtime, agent = rt
    config = runtime.codex_config(agent)
    assert tuple(config.launch_args_override) == (
        "docker", "exec", "-i", f"den-agent-{agent['id']}", "codex", "app-server", "--listen", "stdio://",
    )
    assert runtime.workspace_cwd(agent) == "/workspace"
    assert runtime.container_name(agent["id"]) == f"den-agent-{agent['id']}"
    assert runtime.name == "docker"


# --------------------------------------------------------------------------
# Lifecycle against the fake docker
# --------------------------------------------------------------------------


async def test_prepare_builds_the_image_and_network_once(rt, fake_docker) -> None:
    settings, _, runtime, _ = rt
    await runtime.prepare()
    calls = fake_docker.calls()
    builds = [c for c in calls if c[0] == "build"]
    assert len(builds) == 1
    assert pairs(builds[0]) >= {("-t", settings.agent_image)}
    assert Path(builds[0][-1]).resolve() == settings.container_dir.resolve()
    assert [
        "network", "create", "--driver", "bridge", "-o",
        "com.docker.network.bridge.enable_icc=false", "den-agents",
    ] in calls

    before = len(fake_docker.calls())
    await runtime.prepare()  # idempotent: nothing more to do
    assert "build" not in fake_docker.subcommands()[before:]


async def test_prepare_skips_what_already_exists(rt, fake_docker) -> None:
    settings, _, runtime, _ = rt
    fake_docker.set(images=[settings.agent_image], networks=["den-agents"])
    await runtime.prepare()
    calls = fake_docker.calls()
    assert not [c for c in calls if c[0] == "build"]
    assert not [c for c in calls if c[:2] == ["network", "create"]]


async def test_ensure_running_runs_a_missing_container(rt, fake_docker) -> None:
    settings, _, runtime, agent = rt
    fake_docker.set(images=[settings.agent_image], networks=["den-agents"])
    await runtime.ensure_running(agent)

    runs = [c for c in fake_docker.calls() if c[0] == "run"]
    assert runs == [runtime.run_args(agent)]
    assert fake_docker.containers() == {f"den-agent-{agent['id']}": "running"}
    config = (agent_paths(settings, agent["id"]).codex_home / "config.toml").read_text()
    assert 'model = "gpt-6-luna"' in config
    assert "/model-proxy/v1" in config and "/mcp" in config


@pytest.mark.parametrize("state", ["exited", "created"])
async def test_ensure_running_starts_a_stopped_container(rt, fake_docker, state) -> None:
    settings, _, runtime, agent = rt
    name = f"den-agent-{agent['id']}"
    fake_docker.set(images=[settings.agent_image], networks=["den-agents"], containers={name: state})
    await runtime.ensure_running(agent)
    calls = fake_docker.calls()
    assert ["start", name] in calls
    assert "run" not in fake_docker.subcommands()
    assert fake_docker.containers()[name] == "running"


async def test_ensure_running_leaves_a_running_container_alone(rt, fake_docker) -> None:
    settings, _, runtime, agent = rt
    name = f"den-agent-{agent['id']}"
    fake_docker.set(images=[settings.agent_image], networks=["den-agents"], containers={name: "running"})
    await runtime.ensure_running(agent)
    subs = fake_docker.subcommands()
    assert "run" not in subs and "start" not in subs and "rm" not in subs


@pytest.mark.parametrize(
    "state,expected",
    [("running", "running"), ("created", "stopped"), ("exited", "stopped"), ("paused", "stopped"), ("dead", "stopped")],
)
async def test_status_maps_container_states(rt, fake_docker, state, expected) -> None:
    _, _, runtime, agent = rt
    fake_docker.set(containers={f"den-agent-{agent['id']}": state})
    assert await runtime.status(agent["id"]) == expected


async def test_status_of_a_missing_container(rt, fake_docker) -> None:
    _, _, runtime, agent = rt
    assert await runtime.status(agent["id"]) == "missing"


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: template parsing error: something odd\n",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n",
    ],
)
async def test_status_of_any_other_failure_is_unknown(rt, fake_docker, stderr) -> None:
    _, _, runtime, agent = rt
    fake_docker.set(fail={"inspect": {"code": 1, "stderr": stderr}})
    assert await runtime.status(agent["id"]) == "unknown"


async def test_restart_bumps_the_token_version_and_recreates(rt, fake_docker) -> None:
    settings, db, runtime, agent = rt
    name = f"den-agent-{agent['id']}"
    fake_docker.set(images=[settings.agent_image], networks=["den-agents"], containers={name: "running"})
    await runtime.restart(agent)

    assert db.one("SELECT token_version FROM agents WHERE id=?", (agent["id"],))["token_version"] == 4
    calls = fake_docker.calls()
    rm_index = calls.index(["rm", "-f", name])
    [run] = [c for c in calls if c[0] == "run"]
    assert calls.index(run) > rm_index
    assert f"PLATFORM_TOKEN={make_agent_token(SECRET, agent['id'], 4)}" in run
    assert f"PLATFORM_TOKEN={make_agent_token(SECRET, agent['id'], 3)}" not in run
    assert fake_docker.containers() == {name: "running"}


async def test_stop_uses_a_short_grace_period(rt, fake_docker) -> None:
    _, _, runtime, agent = rt
    name = f"den-agent-{agent['id']}"
    fake_docker.set(containers={name: "running"})
    await runtime.stop(agent["id"])
    assert ["stop", "-t", "5", name] in fake_docker.calls()
    assert fake_docker.containers()[name] == "exited"


async def test_stop_and_remove_ignore_missing_containers(rt, fake_docker) -> None:
    _, _, runtime, agent = rt
    await runtime.stop(agent["id"])
    await runtime.remove(agent["id"])
    assert ["rm", "-f", f"den-agent-{agent['id']}"] in fake_docker.calls()


async def test_a_failing_command_raises_with_stderrs_first_line(rt, fake_docker) -> None:
    settings, _, runtime, agent = rt
    name = f"den-agent-{agent['id']}"
    fake_docker.set(
        images=[settings.agent_image],
        networks=["den-agents"],
        containers={name: "exited"},
        fail={"start": {"code": 1, "stderr": "Error response from daemon: boom happened\nsecond line\n"}},
    )
    with pytest.raises(AgentRuntimeError) as err:
        await runtime.ensure_running(agent)
    assert err.value.message == "Error response from daemon: boom happened"
    assert "second line" in err.value.detail


async def test_missing_docker_binary_is_reported_plainly(rt, tmp_path, monkeypatch) -> None:
    _, _, runtime, agent = rt
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(AgentRuntimeError) as err:
        await runtime.ensure_running(agent)
    assert err.value.message.startswith("Docker is not available on the server")
