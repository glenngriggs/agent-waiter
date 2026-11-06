"""Chat message queue and interrupt, through the real app-server and fake model.

While a chat turn runs, new messages wait in a per-agent queue (mode
"queue") or jump to the front and stop the running turn (mode "interrupt").
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    DEFAULT_PASSWORD,
    create_ready_agent,
    get_agent,
    history,
    make_settings,
    register,
    running_server,
    send_message,
    wait_for,
    wait_turn_completed,
)

MAX_QUEUED = 10


@pytest.fixture(scope="module")
def server(fake_model, tmp_path_factory):
    settings = make_settings(tmp_path_factory.mktemp("queue"), fake_model, onboarding_probe=False)
    with running_server(settings) as s:
        yield s


@pytest.fixture
def user(server, client_factory):
    c = client_factory(server)
    register(c)
    return c


@pytest.fixture
def agent(user):
    return create_ready_agent(user, "Queuey")


def post(user, agent_id: str, text: str, mode: str | None = None):
    body = {"text": text} if mode is None else {"text": text, "mode": mode}
    return user.post(f"/api/agents/{agent_id}/messages", json=body)


def started_order(user, agent_id: str) -> list[str]:
    return [e["data"]["prompt"] for e in history(user, agent_id)["events"] if e["kind"] == "turn_started"]


def stop_and_drain(user, agent_id: str) -> None:
    user.delete(f"/api/agents/{agent_id}/queue")
    user.post(f"/api/agents/{agent_id}/stop")
    wait_for(lambda: not get_agent(user, agent_id)["chat_busy"], timeout=20, message="idle")


def test_idle_agent_runs_the_message_at_once(user, agent) -> None:
    r = post(user, agent["id"], "hello")
    assert r.status_code == 202
    body = r.json()
    assert body["queued"] is False and body["position"] == 0
    assert wait_turn_completed(user, agent["id"], body["turn_id"], timeout=60)["data"]["status"] == "completed"


def test_messages_sent_while_busy_queue_and_run_in_order(user, agent) -> None:
    slow = send_message(user, agent["id"], "SLOW first")
    a = post(user, agent["id"], "second").json()
    b = post(user, agent["id"], "third").json()
    assert (a["queued"], a["position"]) == (True, 0)
    assert (b["queued"], b["position"]) == (True, 1)

    h = history(user, agent["id"])
    assert [q["prompt"] for q in h["queue"]] == ["second", "third"]
    assert get_agent(user, agent["id"])["queued_count"] == 2
    # Queued messages are announced but not started yet.
    kinds = {(e["kind"], e["turn_id"]) for e in h["events"]}
    assert ("turn_queued", a["turn_id"]) in kinds and ("turn_started", a["turn_id"]) not in kinds

    user.post(f"/api/agents/{agent['id']}/stop")  # Stop ends only the running turn ...
    assert wait_turn_completed(user, agent["id"], slow, timeout=20)["data"]["status"] == "interrupted"
    # ... and the queue carries on by itself, in order.
    assert wait_turn_completed(user, agent["id"], a["turn_id"], timeout=60)["data"]["status"] == "completed"
    assert wait_turn_completed(user, agent["id"], b["turn_id"], timeout=60)["data"]["status"] == "completed"
    assert started_order(user, agent["id"])[-3:] == ["SLOW first", "second", "third"]
    assert history(user, agent["id"])["queue"] == []


def test_interrupt_stops_the_running_turn_and_jumps_the_queue(user, agent) -> None:
    slow = send_message(user, agent["id"], "SLOW long task")
    waiting = post(user, agent["id"], "can wait").json()
    urgent = post(user, agent["id"], "urgent", mode="interrupt").json()
    assert urgent["queued"] is True and urgent["position"] == 0

    done = wait_turn_completed(user, agent["id"], slow, timeout=20)["data"]
    assert done["status"] == "interrupted"
    assert done["error_message"] == "Interrupted by a newer message."
    assert wait_turn_completed(user, agent["id"], urgent["turn_id"], timeout=60)["data"]["status"] == "completed"
    assert wait_turn_completed(user, agent["id"], waiting["turn_id"], timeout=60)["data"]["status"] == "completed"
    assert started_order(user, agent["id"])[-3:] == ["SLOW long task", "urgent", "can wait"]


def test_interrupt_on_an_idle_agent_just_runs(user, agent) -> None:
    r = post(user, agent["id"], "now please", mode="interrupt").json()
    assert r["queued"] is False
    assert wait_turn_completed(user, agent["id"], r["turn_id"], timeout=60)["data"]["status"] == "completed"


def test_cancel_one_and_clear_the_queue(user, agent) -> None:
    send_message(user, agent["id"], "SLOW busy")
    ids = [post(user, agent["id"], f"q{i}").json()["turn_id"] for i in range(3)]

    assert user.delete(f"/api/agents/{agent['id']}/queue/{ids[1]}").status_code == 204
    assert user.delete(f"/api/agents/{agent['id']}/queue/{ids[1]}").status_code == 404  # already gone
    h = history(user, agent["id"])
    assert [q["turn_id"] for q in h["queue"]] == [ids[0], ids[2]]
    assert any(e["kind"] == "turn_cancelled" and e["turn_id"] == ids[1] for e in h["events"])

    assert user.delete(f"/api/agents/{agent['id']}/queue").json() == {"cancelled": 2}
    assert history(user, agent["id"])["queue"] == []
    stop_and_drain(user, agent["id"])
    # Nothing that was cancelled ever started.
    assert not set(ids) & {e["turn_id"] for e in history(user, agent["id"])["events"] if e["kind"] == "turn_started"}


def test_queue_is_limited(user, agent) -> None:
    send_message(user, agent["id"], "SLOW busy")
    for i in range(MAX_QUEUED):
        assert post(user, agent["id"], f"m{i}").status_code == 202
    r = post(user, agent["id"], "one too many")
    assert r.status_code == 409 and "queue is full" in r.json()["detail"]
    stop_and_drain(user, agent["id"])


def test_bad_mode_is_rejected(user, agent) -> None:
    assert post(user, agent["id"], "x", mode="sideways").status_code == 400


def test_container_stop_clears_the_queue(user, agent) -> None:
    slow = send_message(user, agent["id"], "SLOW busy")
    queued = post(user, agent["id"], "later").json()["turn_id"]
    assert user.post(f"/api/agents/{agent['id']}/container/stop").status_code == 200
    wait_turn_completed(user, agent["id"], slow, timeout=20)
    wait_for(lambda: not get_agent(user, agent["id"])["chat_busy"], timeout=10, message="idle")
    h = history(user, agent["id"])
    assert h["queue"] == []
    assert not any(e["kind"] == "turn_started" and e["turn_id"] == queued for e in h["events"])


def test_another_user_cannot_touch_my_queue(user, agent, server, client_factory) -> None:
    send_message(user, agent["id"], "SLOW busy")
    mine = post(user, agent["id"], "private").json()["turn_id"]
    other = client_factory(server)
    register(other)
    assert other.delete(f"/api/agents/{agent['id']}/queue/{mine}").status_code == 404
    assert other.delete(f"/api/agents/{agent['id']}/queue").status_code == 404
    assert other.post(f"/api/agents/{agent['id']}/messages", json={"text": "x"}).status_code == 404
    # Their own agent cannot cancel my turn id either.
    theirs = create_ready_agent(other, "Theirs")
    assert other.delete(f"/api/agents/{theirs['id']}/queue/{mine}").status_code == 404
    assert [q["turn_id"] for q in history(user, agent["id"])["queue"]] == [mine]
    stop_and_drain(user, agent["id"])


def test_queue_survives_a_platform_restart(fake_model, tmp_path, client_factory) -> None:
    settings = make_settings(tmp_path / "data", fake_model, onboarding_probe=False)
    with running_server(settings) as s1:
        c = client_factory(s1)
        email, password = register(c)["email"], DEFAULT_PASSWORD
        agent = create_ready_agent(c, "Durable")
        send_message(c, agent["id"], "SLOW interrupted by restart")
        queued = post(c, agent["id"], "run me after the restart").json()["turn_id"]
    with running_server(settings) as s2:
        c2 = client_factory(s2)
        assert c2.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
        done = wait_turn_completed(c2, agent["id"], queued, timeout=60)
        assert done["data"]["status"] == "completed"
