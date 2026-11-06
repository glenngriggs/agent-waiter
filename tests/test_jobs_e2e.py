"""Scheduled jobs through the real harness: run now, timed runs, cancel, overlap."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from den.db import iso, parse_iso, utcnow
from tests.conftest import (
    chat,
    create_ready_agent,
    events_of,
    get_run,
    history,
    jobs_of,
    make_settings,
    register,
    running_server,
    runs_of,
    wait_for,
    wait_run_status,
)

RUN_KEYS = {
    "id", "job_id", "status", "late", "trigger", "scheduled_for", "started_at",
    "finished_at", "summary", "error_message", "turn_id",
}


@pytest.fixture(scope="module")
def server(fake_model, tmp_path_factory):
    settings = make_settings(tmp_path_factory.mktemp("jobs"), fake_model, onboarding_probe=False)
    with running_server(settings) as s:
        yield s


@pytest.fixture
def user(server, client_factory):
    c = client_factory(server)
    register(c, timezone="America/Detroit")
    return c


@pytest.fixture
def agent(user):
    return create_ready_agent(user, "Worker")


def create_job(user, agent_id: str, prompt: str, schedule: dict, name: str = "Job", **extra) -> dict:
    r = user.post(
        f"/api/agents/{agent_id}/jobs", json={"name": name, "prompt": prompt, "schedule": schedule, **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()["job"]


def run_now(user, agent_id: str, job_id: str) -> dict:
    r = user.post(f"/api/agents/{agent_id}/jobs/{job_id}/run")
    assert r.status_code == 202, r.text
    return r.json()["run"]


HOURLY = {"kind": "interval", "minutes": 60}


def test_run_now_succeeds_with_the_reply_as_summary(user, agent) -> None:
    prompt = f"Say hello {uuid.uuid4().hex[:8]}"
    before = utcnow()
    job = create_job(user, agent["id"], prompt, HOURLY, name="Greeter")
    assert job["created_by"] == "user"
    assert job["status"] == "active"
    assert job["schedule"] == {
        "kind": "interval", "value": "60", "description": "Every 60 minutes", "timezone": "America/Detroit",
    }
    assert parse_iso(job["next_run_at"]) >= before + timedelta(minutes=59)
    assert job["last_run"] is None

    run = run_now(user, agent["id"], job["id"])
    assert set(run) >= RUN_KEYS
    assert run["trigger"] == "manual" and run["status"] in ("queued", "running")
    run = wait_run_status(user, agent["id"], run["id"], {"succeeded", "failed", "interrupted"})
    assert run["status"] == "succeeded", run["error_message"]
    assert run["summary"] == f"Echo: {prompt}"
    assert run["late"] is False
    assert run["started_at"] and run["finished_at"] and run["turn_id"]

    # The transcript of the run.
    detail = get_run(user, agent["id"], run["id"])
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds[0] == "turn_started" and kinds[-1] == "turn_completed"
    assert all(e["channel"] == "job" and e["turn_id"] == run["turn_id"] for e in detail["events"])
    started = detail["events"][0]["data"]
    assert started["source"] == "job"
    assert started["job_id"] == job["id"] and started["run_id"] == run["id"]
    assert started["job_name"] == "Greeter"
    assert any(
        e["kind"] == "item_completed" and e["data"].get("text") == f"Echo: {prompt}" for e in detail["events"]
    )

    # The result card in the chat timeline; the run's own items stay out of it.
    timeline = history(user, agent["id"])["events"]
    finished = [e for e in events_of(timeline, "job_run_finished") if e["data"]["run_id"] == run["id"]]
    assert len(finished) == 1
    assert finished[0]["channel"] == "agent"
    assert finished[0]["data"]["status"] == "succeeded"
    assert finished[0]["data"]["summary"] == f"Echo: {prompt}"
    assert [e for e in events_of(timeline, "job_run_started") if e["data"]["run_id"] == run["id"]]
    assert not [e for e in timeline if e["channel"] == "job"]

    # The job shows its last result, and a manual run does not move the schedule.
    [listed] = jobs_of(user, agent["id"])
    assert listed["last_run"]["id"] == run["id"]
    assert listed["next_run_at"] == job["next_run_at"]
    assert [r["id"] for r in runs_of(user, agent["id"], job["id"])] == [run["id"]]


def test_once_job_fires_on_its_own_and_finishes(user, agent) -> None:
    at = iso(utcnow() + timedelta(seconds=3))
    job = create_job(user, agent["id"], "Ping the timer", {"kind": "once", "at": at}, name="Timer")
    assert job["schedule"]["kind"] == "once"

    def finished_job():
        [j] = jobs_of(user, agent["id"])
        return j if j["status"] == "finished" else None

    job = wait_for(finished_job, timeout=60, interval=0.5, message="the once job to finish")
    assert job["next_run_at"] is None
    last = job["last_run"]
    assert last["status"] == "succeeded", last["error_message"]
    assert last["trigger"] == "schedule"
    assert last["late"] is False
    assert parse_iso(last["scheduled_for"]) == parse_iso(at)
    assert len(runs_of(user, agent["id"], job["id"])) == 1


def test_cancelling_a_job_interrupts_its_running_run(user, agent) -> None:
    job = create_job(user, agent["id"], "SLOW report", HOURLY, name="Slowpoke")
    run = run_now(user, agent["id"], job["id"])
    wait_run_status(user, agent["id"], run["id"], {"running"}, timeout=30)

    # A second run while one is going is refused.
    assert user.post(f"/api/agents/{agent['id']}/jobs/{job['id']}/run").status_code == 409

    r = user.delete(f"/api/agents/{agent['id']}/jobs/{job['id']}")
    assert r.status_code == 200, r.text
    cancelled = r.json()["job"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["next_run_at"] is None
    assert cancelled["cancelled_at"]

    run = wait_run_status(user, agent["id"], run["id"], {"interrupted", "failed", "succeeded"}, timeout=20)
    assert run["status"] == "interrupted"
    # A cancelled job cannot be run again.
    assert user.post(f"/api/agents/{agent['id']}/jobs/{job['id']}/run").status_code == 409


def test_a_job_run_and_a_chat_turn_run_at_the_same_time(user, agent) -> None:
    job = create_job(user, agent["id"], "SLOW background work", HOURLY, name="Background")
    run = run_now(user, agent["id"], job["id"])
    wait_run_status(user, agent["id"], run["id"], {"running"}, timeout=30)
    # Wait until the run's app-server is up and the model is streaming, so the
    # chat turn really overlaps an active run. (Two app-servers cold-starting
    # on a brand-new codex-home at the same instant is a separate Codex race.)
    wait_for(
        lambda: any(
            e["kind"] == "item_started" and e["data"].get("item_type") == "agent_message"
            for e in get_run(user, agent["id"], run["id"])["events"]
        ),
        timeout=30,
        message="the job run to start streaming",
    )

    done = chat(user, agent["id"], "hello while you work")
    assert done["data"]["status"] == "completed"
    assert get_run(user, agent["id"], run["id"])["run"]["status"] == "running"

    # Stopping the chat never touches the job run.
    assert user.post(f"/api/agents/{agent['id']}/stop").json() == {"stopped": False}
    assert get_run(user, agent["id"], run["id"])["run"]["status"] == "running"

    user.delete(f"/api/agents/{agent['id']}/jobs/{job['id']}")
    wait_run_status(user, agent["id"], run["id"], {"interrupted"}, timeout=20)


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "prompt": "p", "schedule": {"kind": "interval", "minutes": 0}},
        {"name": "x", "prompt": "p", "schedule": {"kind": "cron", "expr": "not cron"}},
        {"name": "x", "prompt": "p", "schedule": {"kind": "once", "at": "2001-01-01T00:00:00Z"}},
        {"name": "x", "prompt": "p", "schedule": {"kind": "weekly"}},
        {"name": "", "prompt": "p", "schedule": {"kind": "interval", "minutes": 60}},
        {"name": "x", "prompt": "", "schedule": {"kind": "interval", "minutes": 60}},
        {"name": "x", "prompt": "p", "schedule": {"kind": "interval", "minutes": 60}, "timezone": "Mars/Base"},
    ],
)
def test_invalid_jobs_are_400_with_a_message(user, agent, body) -> None:
    r = user.post(f"/api/agents/{agent['id']}/jobs", json=body)
    assert r.status_code in (400, 422), r.text
    if r.status_code == 400:
        assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert jobs_of(user, agent["id"]) == []


def test_explicit_timezone_wins_over_the_users(user, agent) -> None:
    job = create_job(user, agent["id"], "p", {"kind": "cron", "expr": "0 9 * * *"}, timezone="Asia/Tokyo")
    assert job["schedule"]["timezone"] == "Asia/Tokyo"
    assert job["schedule"]["value"] == "0 9 * * *"
    assert job["schedule"]["description"] == "Cron `0 9 * * *` (Asia/Tokyo)"


def test_too_many_active_jobs_is_409(server, user, agent) -> None:
    limit = server.settings.max_jobs_per_agent
    for i in range(limit):
        create_job(user, agent["id"], "p", HOURLY, name=f"J{i}")
    r = user.post(f"/api/agents/{agent['id']}/jobs", json={"name": "one more", "prompt": "p", "schedule": HOURLY})
    assert r.status_code == 409
    # Cancelled jobs do not count.
    first = jobs_of(user, agent["id"])[-1]
    user.delete(f"/api/agents/{agent['id']}/jobs/{first['id']}")
    create_job(user, agent["id"], "p", HOURLY, name="replacement")


def test_jobs_are_listed_active_first_then_newest(user, agent) -> None:
    oldest = create_job(user, agent["id"], "p", HOURLY, name="oldest")
    middle = create_job(user, agent["id"], "p", HOURLY, name="middle")
    user.delete(f"/api/agents/{agent['id']}/jobs/{oldest['id']}")
    newest = create_job(user, agent["id"], "p", HOURLY, name="newest")
    names = [j["name"] for j in jobs_of(user, agent["id"])]
    assert names == ["newest", "middle", "oldest"]
    assert [newest["id"], middle["id"]] == [j["id"] for j in jobs_of(user, agent["id"])[:2]]


def test_unknown_job_and_run_ids_are_404(user, agent) -> None:
    missing = uuid.uuid4().hex
    base = f"/api/agents/{agent['id']}"
    assert user.delete(f"{base}/jobs/{missing}").status_code == 404
    assert user.post(f"{base}/jobs/{missing}/run").status_code == 404
    assert user.get(f"{base}/jobs/{missing}/runs").status_code == 404
    assert user.get(f"{base}/runs/{missing}").status_code == 404
