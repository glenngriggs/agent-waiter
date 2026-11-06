"""Chat and onboarding through the real harness.

Local runtime, the real bundled `codex app-server`, the real SDK, Den's MCP
endpoint and model proxy, and the scripted fake model. No Docker, no keys.
"""

from __future__ import annotations

import time
import uuid

import pytest

from den.agent_files import agent_paths
from tests.conftest import (
    FAKE_UPSTREAM_KEY,
    EventStream,
    agent_row,
    chat,
    create_agent,
    create_ready_agent,
    events_of,
    get_agent,
    history,
    item_events,
    jobs_of,
    make_settings,
    open_synced_stream,
    register,
    running_server,
    send_message,
    wait_for,
    wait_setup_settled,
    wait_turn_completed,
)

# A model the server offers but the (fake) upstream refuses, the way a listed
# model sometimes does not work.
BROKEN_MODEL = "gpt-ghost"
AGENT_OUT_KEYS = {
    "id", "name", "model", "setup_state", "setup_detail", "container", "chat_busy",
    "active_turn_id", "conversation_no", "created_at", "last_active_at", "queued_count",
}


@pytest.fixture(scope="module")
def server(fake_model, tmp_path_factory):
    settings = make_settings(
        tmp_path_factory.mktemp("chat"),
        fake_model,
        onboarding_probe=True,
        models=("gpt-6.1-sol", "gpt-6-luna", BROKEN_MODEL),
    )
    with running_server(settings) as s:
        yield s


@pytest.fixture
def user(server, client_factory):
    c = client_factory(server)
    register(c)
    return c


@pytest.fixture
def agent(user):
    return create_ready_agent(user, "Chatty")


def marker() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Onboarding
# --------------------------------------------------------------------------


def test_onboarding_reaches_ready_through_every_step(server, user, fake_model) -> None:
    agent = create_agent(user, "Fresh", "gpt-6-luna")
    assert agent["setup_state"] == "creating"
    assert set(agent) >= AGENT_OUT_KEYS
    agent = wait_setup_settled(user, agent["id"])
    assert agent["setup_state"] == "ready", agent["setup_detail"]
    assert agent["container"] == "running"  # the local runtime is always "running"
    assert agent["chat_busy"] is False and agent["active_turn_id"] is None

    statuses = events_of(history(user, agent["id"])["events"], "agent_status")
    assert all(e["channel"] == "agent" for e in statuses)
    steps = [e["data"]["step"] for e in statuses]
    for earlier, later in [("files", "container"), ("container", "probe"), ("probe", "ready")]:
        assert earlier in steps and later in steps, steps
        assert steps.index(earlier) < steps.index(later), steps
    assert statuses[-1]["data"]["state"] == "ready"

    # The probe reached the model through the proxy, carrying only the
    # platform's upstream key (Codex's request includes the agent's cwd).
    probe_requests = fake_model.requests_containing(agent["id"])
    assert probe_requests, "the probe never reached the model"
    assert {r["headers"]["authorization"] for r in probe_requests} == {f"Bearer {FAKE_UPSTREAM_KEY}"}


def test_probe_failure_ends_in_error_and_retry_with_another_model_works(server, user) -> None:
    agent = create_agent(user, "Doomed", BROKEN_MODEL)
    agent = wait_setup_settled(user, agent["id"])
    assert agent["setup_state"] == "error"
    assert agent["setup_detail"].startswith("probe:")
    last = events_of(history(user, agent["id"])["events"], "agent_status")[-1]
    assert last["data"]["state"] == "error"
    assert last["data"]["step"] == "probe"
    assert last["data"]["message"]

    r = user.post(f"/api/agents/{agent['id']}/messages", json={"text": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"] == "This agent is still being set up."

    # The user picks the other model and presses Retry.
    r = user.patch(f"/api/agents/{agent['id']}", json={"model": "gpt-6-luna"})
    assert r.status_code == 200, r.text
    r = user.post(f"/api/agents/{agent['id']}/setup/retry")
    assert r.status_code == 202, r.text
    agent = wait_setup_settled(user, agent["id"])
    assert agent["setup_state"] == "ready", agent["setup_detail"]
    # Retry is only for agents in the error state.
    assert user.post(f"/api/agents/{agent['id']}/setup/retry").status_code == 409


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


def test_message_streams_deltas_and_lands_in_history(user, agent) -> None:
    text = f"hello there {marker()}"
    with open_synced_stream(user, agent["id"]) as stream:
        turn_id = send_message(user, agent["id"], text)
        live = stream.read_until(
            lambda e: e["kind"] == "turn_completed" and e["turn_id"] == turn_id, timeout=60
        )

    mine = [e for e in live if e["turn_id"] == turn_id]
    assert all(e["channel"] == "chat" for e in mine)
    started = events_of(mine, "turn_started")[0]
    assert started["data"]["source"] == "chat" and started["data"]["prompt"] == text
    deltas = events_of(mine, "message_delta")
    assert deltas, "no message_delta arrived over SSE"
    assert all(d["seq"] is None and d["_sse_id"] is None for d in deltas)  # live only
    [message] = item_events(mine, "agent_message")
    assert message["data"]["text"] == f"Echo: {text}"
    assert "".join(d["data"]["delta"] for d in deltas if d["data"]["item_id"] == message["data"]["item_id"]) == f"Echo: {text}"
    done = events_of(mine, "turn_completed")[0]
    assert done["data"]["status"] == "completed"
    assert done["data"]["final_response"] == f"Echo: {text}"
    assert done["data"]["error_kind"] is None
    assert done["seq"] == done["_sse_id"]

    saved = history(user, agent["id"])
    kinds = [e["kind"] for e in saved["events"] if e["turn_id"] == turn_id]
    assert kinds[0] == "turn_started" and kinds[-1] == "turn_completed"
    assert "message_delta" not in kinds  # deltas are never persisted
    assert item_events(saved["events"], "agent_message")[-1]["data"]["text"] == f"Echo: {text}"
    assert saved["active"] is None
    seqs = [e["seq"] for e in saved["events"]]
    assert seqs == sorted(seqs) and saved["last_seq"] == seqs[-1]
    assert get_agent(user, agent["id"])["chat_busy"] is False


def test_second_message_continues_the_same_thread(server, user, agent, fake_model) -> None:
    first, second = marker(), marker()
    assert chat(user, agent["id"], f"remember {first}")["data"]["status"] == "completed"
    thread_id = agent_row(server, agent["id"])["chat_thread_id"]
    assert thread_id

    assert chat(user, agent["id"], f"and now {second}")["data"]["status"] == "completed"
    assert agent_row(server, agent["id"])["chat_thread_id"] == thread_id
    turns = server.ctx.db.all(
        "SELECT codex_thread_id FROM turns WHERE agent_id=? AND source='chat'", (agent["id"],)
    )
    assert {t["codex_thread_id"] for t in turns} == {thread_id}
    # The model saw the first exchange when answering the second.
    requests = fake_model.requests_containing(second)
    assert requests and all(first in r["raw"] for r in requests)


def test_shell_command_item_and_the_file_it_wrote(user, agent) -> None:
    done = chat(user, agent["id"], "RUN:echo hi > note.txt")
    assert done["data"]["status"] == "completed"
    assert done["data"]["final_response"] == "Ran it."
    events = [e for e in history(user, agent["id"])["events"] if e["turn_id"] == done["turn_id"]]
    [command] = item_events(events, "command")
    assert "echo hi" in command["data"]["command"]
    assert command["data"]["exit_code"] == 0
    assert command["data"]["status"] == "completed"
    assert "output" in command["data"] and "duration_ms" in command["data"]
    assert item_events(events, "command", kind="item_started")

    files = user.get(f"/api/agents/{agent['id']}/files", params={"path": ""}).json()["entries"]
    assert "note.txt" in [f["name"] for f in files]
    preview = user.get(f"/api/agents/{agent['id']}/files/preview", params={"path": "note.txt"}).json()
    assert preview["text"] == "hi\n"


def test_failing_command_reports_its_exit_code(user, agent) -> None:
    done = chat(user, agent["id"], "RUN:exit 3")
    events = [e for e in history(user, agent["id"])["events"] if e["turn_id"] == done["turn_id"]]
    [command] = item_events(events, "command")
    assert command["data"]["exit_code"] == 3


def test_agent_schedules_a_job_through_mcp(user, agent) -> None:
    done = chat(user, agent["id"], "SCHEDULE:30")
    assert done["data"]["status"] == "completed"
    assert done["data"]["final_response"] == "Scheduled it."
    events = [e for e in history(user, agent["id"])["events"] if e["turn_id"] == done["turn_id"]]
    [call] = item_events(events, "mcp_tool_call")
    assert call["data"]["server"] == "platform"
    assert call["data"]["tool"] == "schedule_job"
    assert call["data"]["status"] == "completed"
    assert "Scheduled job" in call["data"]["result_text"]
    assert not call["data"].get("error")

    [job] = [j for j in jobs_of(user, agent["id"]) if j["name"] == "follow-up"]
    assert job["created_by"] == "agent"
    assert job["prompt"] == "Check on the thing"
    assert job["schedule"]["kind"] == "once"
    assert job["status"] == "active"


# --------------------------------------------------------------------------
# Busy, Stop, reconnect
# --------------------------------------------------------------------------


def test_slow_message_can_be_stopped(user, agent) -> None:
    with open_synced_stream(user, agent["id"]) as stream:
        turn_id = send_message(user, agent["id"], "SLOW please")
        stream.read_until(lambda e: e["kind"] == "message_delta" and e["turn_id"] == turn_id, timeout=60)
        assert get_agent(user, agent["id"])["chat_busy"] is True

        stopped_at = time.monotonic()
        r = user.post(f"/api/agents/{agent['id']}/stop")
        assert r.status_code == 200 and r.json() == {"stopped": True}
        done = stream.read_until(
            lambda e: e["kind"] == "turn_completed" and e["turn_id"] == turn_id, timeout=10
        )[-1]
        assert time.monotonic() - stopped_at < 10

    assert done["data"]["status"] == "interrupted"
    assert done["data"]["error_kind"] == "interrupted"
    # The text that streamed before Stop is persisted, so a reload shows it.
    saved = [
        e for e in history(user, agent["id"])["events"]
        if e["turn_id"] == turn_id and e["kind"] == "item_completed"
        and e["data"]["item_type"] == "agent_message"
    ]
    assert saved and saved[-1]["data"]["partial"] is True and saved[-1]["data"]["text"].startswith("tick0")
    wait_for(lambda: get_agent(user, agent["id"])["chat_busy"] is False, timeout=5, message="chat free")
    # The composer is free again: the next message runs normally.
    assert chat(user, agent["id"], "after the stop")["data"]["status"] == "completed"
    assert user.post(f"/api/agents/{agent['id']}/stop").json() == {"stopped": False}


def test_new_conversation_is_refused_while_a_turn_runs(user, agent) -> None:
    turn_id = send_message(user, agent["id"], "SLOW one")
    busy = get_agent(user, agent["id"])
    assert busy["chat_busy"] is True and busy["active_turn_id"] == turn_id
    assert user.post(f"/api/agents/{agent['id']}/conversation/new").status_code == 409

    user.post(f"/api/agents/{agent['id']}/stop")
    assert wait_turn_completed(user, agent["id"], turn_id, timeout=15)["data"]["status"] == "interrupted"


@pytest.mark.parametrize("text", ["", "x" * 20001])
def test_message_length_is_validated(user, agent, text) -> None:
    r = user.post(f"/api/agents/{agent['id']}/messages", json={"text": text})
    assert r.status_code in (400, 422)


def test_reconnect_with_last_event_id_replays_and_snapshots(user, agent) -> None:
    stream = open_synced_stream(user, agent["id"])
    turn_id = send_message(user, agent["id"], "SLOW story")
    seen = stream.read_until(
        lambda e: e["kind"] == "turn_started" and e["turn_id"] == turn_id, timeout=30
    )
    turn_started_seq = seen[-1]["seq"]
    deltas: list[dict] = []
    item_started: dict | None = None
    while len(deltas) < 3:
        event = stream.next_event()
        if event["kind"] == "item_started" and event["data"].get("item_type") == "agent_message":
            item_started = event
        if event["kind"] == "message_delta" and event["turn_id"] == turn_id:
            deltas.append(event)
    stream.close()  # the browser tab closes mid-turn
    assert item_started is not None, "the agent message never started"
    text_before = "".join(d["data"]["delta"] for d in deltas)

    time.sleep(1.5)  # the agent keeps working with nobody watching

    # `after=0` is overridden by the Last-Event-ID header.
    with EventStream(user, agent["id"], after=0, last_event_id=turn_started_seq) as again:
        replay = again.read_until(lambda e: e["kind"] == "snapshot", timeout=30)
        replayed = [e for e in replay if e["seq"] is not None]
        assert replayed, "nothing was replayed"
        assert all(e["seq"] > turn_started_seq for e in replayed)
        assert [e["seq"] for e in replayed] == sorted({e["seq"] for e in replayed})
        assert item_started["seq"] in [e["seq"] for e in replayed]

        snapshot = replay[-1]
        assert snapshot["data"]["turn_id"] == turn_id
        partial = snapshot["data"]["items"][item_started["data"]["item_id"]]
        assert partial.startswith(text_before) and len(partial) > len(text_before)

        # Live deltas keep flowing on the new connection.
        again.read_until(lambda e: e["kind"] == "message_delta" and e["turn_id"] == turn_id, timeout=10)

    assert history(user, agent["id"])["active"]["turn_id"] == turn_id
    user.post(f"/api/agents/{agent['id']}/stop")
    wait_turn_completed(user, agent["id"], turn_id, timeout=15)
    assert history(user, agent["id"])["active"] is None


# --------------------------------------------------------------------------
# Failures and conversations
# --------------------------------------------------------------------------


def test_rejected_credentials_end_the_turn_unauthorized(user, agent) -> None:
    # Codex retries a 401 five times before giving up (about 6 s).
    done = chat(user, agent["id"], "FAIL401", timeout=90)
    assert done["data"]["status"] == "failed"
    assert done["data"]["error_kind"] == "unauthorized"
    assert done["data"]["error_message"].startswith("The model provider rejected the platform's credentials")
    events = [e for e in history(user, agent["id"])["events"] if e["turn_id"] == done["turn_id"]]
    assert any(n["data"]["level"] == "error" for n in events_of(events, "notice"))
    assert get_agent(user, agent["id"])["chat_busy"] is False


def test_new_conversation_starts_a_fresh_thread(server, user, agent, fake_model) -> None:
    old = marker()
    chat(user, agent["id"], f"old topic {old}")
    old_thread = agent_row(server, agent["id"])["chat_thread_id"]

    r = user.post(f"/api/agents/{agent['id']}/conversation/new")
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["conversation_no"] == 2
    assert agent_row(server, agent["id"])["chat_thread_id"] is None
    notices = events_of(history(user, agent["id"])["events"], "notice")
    assert notices[-1]["data"]["message"] == "Started a new conversation."

    new = marker()
    assert chat(user, agent["id"], f"new topic {new}")["data"]["status"] == "completed"
    assert agent_row(server, agent["id"])["chat_thread_id"] not in (None, old_thread)
    requests = fake_model.requests_containing(new)
    assert requests and all(old not in r["raw"] for r in requests)


def test_workspace_is_the_agents_working_directory(server, user, agent) -> None:
    chat(user, agent["id"], "RUN:pwd > where.txt")
    workspace = agent_paths(server.settings, agent["id"]).workspace
    assert (workspace / "where.txt").read_text().strip() == str(workspace)
