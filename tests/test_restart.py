"""Agents come back the same after the platform restarts."""

from __future__ import annotations

import uuid

from den.db import Database, iso, new_id, utcnow
from tests.conftest import (
    DEFAULT_PASSWORD,
    agent_row,
    chat,
    create_ready_agent,
    history,
    item_events,
    jobs_of,
    make_settings,
    new_client,
    register,
    running_server,
    unique_email,
)


def test_everything_survives_a_restart(fake_model, tmp_path) -> None:
    data_dir = tmp_path / "data"
    settings = make_settings(data_dir, fake_model, onboarding_probe=False)
    email = unique_email("restart")
    first_marker = uuid.uuid4().hex[:12]

    # ---- Life before the restart ----
    with running_server(settings) as server, new_client(server) as c:
        user = register(c, email, timezone="America/Detroit")
        agent = create_ready_agent(c, "Survivor", "gpt-6.1-sol")
        assert chat(c, agent["id"], f"remember {first_marker}")["data"]["status"] == "completed"
        thread_id = agent_row(server, agent["id"])["chat_thread_id"]
        assert thread_id
        r = c.post(
            f"/api/agents/{agent['id']}/jobs",
            json={"name": "Daily", "prompt": "Summarize", "schedule": {"kind": "cron", "expr": "0 9 * * *"}},
        )
        assert r.status_code == 201, r.text
        job = r.json()["job"]
        r = c.post(
            f"/api/agents/{agent['id']}/files/upload",
            params={"path": ""},
            files={"files": ("keep.txt", b"kept across restarts")},
        )
        assert r.status_code == 201, r.text
        session_cookie = c.cookies.get("den_session")
        events_before = history(c, agent["id"])["events"]

    # ---- While it is down: a turn that was running when the process died ----
    stuck_turn = new_id()
    db = Database(data_dir / "den.db")
    db.execute(
        "INSERT INTO turns (id, agent_id, source, conversation_no, prompt, status, started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (stuck_turn, agent["id"], "chat", 1, "cut off", "running", iso(utcnow())),
    )
    db.close()

    # ---- A new process on the same data directory (and a new port) ----
    second_marker = uuid.uuid4().hex[:12]
    with running_server(settings) as server, new_client(server) as c:
        # The session cookie still works: sessions live in the database.
        c.cookies.set("den_session", session_cookie)
        me = c.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["user"] == user
        # And so does logging in again.
        with new_client(server) as fresh:
            r = fresh.post("/api/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})
            assert r.status_code == 200

        [back] = c.get("/api/agents").json()["agents"]
        assert (back["id"], back["name"], back["model"]) == (agent["id"], "Survivor", "gpt-6.1-sol")
        assert back["setup_state"] == "ready"

        events_after = history(c, agent["id"])["events"]
        assert [e["seq"] for e in events_after[: len(events_before)]] == [e["seq"] for e in events_before]
        replies = [e["data"]["text"] for e in item_events(events_after, "agent_message")]
        assert f"Echo: remember {first_marker}" in replies

        [job_after] = jobs_of(c, agent["id"])
        assert job_after["id"] == job["id"]
        assert job_after["status"] == "active"
        assert job_after["next_run_at"] == job["next_run_at"]
        assert job_after["schedule"]["timezone"] == "America/Detroit"

        r = c.get(f"/api/agents/{agent['id']}/files/download", params={"path": "keep.txt"})
        assert r.status_code == 200 and r.content == b"kept across restarts"

        stuck = server.ctx.db.one("SELECT * FROM turns WHERE id=?", (stuck_turn,))
        assert stuck["status"] == "interrupted"
        assert stuck["error_kind"] == "platform_restart"
        assert stuck["error_message"]

        # The conversation continues in the same Codex thread, which lives on disk.
        assert chat(c, agent["id"], f"and also {second_marker}")["data"]["status"] == "completed"
        assert agent_row(server, agent["id"])["chat_thread_id"] == thread_id
        requests = fake_model.requests_containing(second_marker)
        assert requests and all(first_marker in r["raw"] for r in requests)


def test_setup_cut_off_by_a_restart_is_marked_error(fake_model, tmp_path) -> None:
    data_dir = tmp_path / "data"
    settings = make_settings(data_dir, fake_model, onboarding_probe=False)
    with running_server(settings) as server, new_client(server) as c:
        register(c)
        agent = create_ready_agent(c, "Interrupted")
        session_cookie = c.cookies.get("den_session")

    # Pretend the process died during setup.
    db = Database(data_dir / "den.db")
    db.execute("UPDATE agents SET setup_state='creating' WHERE id=?", (agent["id"],))
    db.close()

    with running_server(settings) as server, new_client(server) as c:
        c.cookies.set("den_session", session_cookie)
        back = c.get(f"/api/agents/{agent['id']}").json()["agent"]
        assert back["setup_state"] == "error"
        assert back["setup_detail"] == "setup: the platform restarted during setup; press Retry"
        assert c.post(f"/api/agents/{agent['id']}/setup/retry").status_code == 202
