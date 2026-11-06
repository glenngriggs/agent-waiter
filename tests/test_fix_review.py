"""Regression tests for the follow-ups applied in review: the custom key and
its URL, container stop/restart against new messages, stopping after a
failed setup, a model the server dropped, and unencodable request text."""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.conftest import (
    create_ready_agent,
    get_agent,
    register,
    send_message,
    wait_for,
    wait_turn_completed,
)


# --------------------------------------------------------------------------
# #19: a saved custom key only goes back to the URL it was saved for
# --------------------------------------------------------------------------


def _capture_server() -> tuple[HTTPServer, list[str | None]]:
    captured: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
            captured.append(self.headers.get("authorization"))
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "missing model"}}')

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, captured


def test_saved_custom_key_is_not_sent_to_a_new_url(live_server, client_factory, fake_model) -> None:
    capture, captured = _capture_server()
    try:
        c = client_factory(live_server)
        register(c)
        saved_url = fake_model.url + "/v1"
        r = c.put("/api/providers/custom/key", json={
            "api_key": "sk-victim-secret-1234567890", "base_url": saved_url, "models": ["m1"]})
        assert r.status_code == 200, r.text
        r = c.put("/api/providers/custom/key",
                  json={"base_url": f"http://127.0.0.1:{capture.server_port}/v1"})
        assert r.status_code == 400
        assert "Paste the key again" in r.json()["detail"]
        assert captured == []
        # The same URL (trailing slash aside) may still reuse the saved key.
        r = c.put("/api/providers/custom/key",
                  json={"base_url": saved_url + "/", "models": ["m1", "m2"]})
        assert r.status_code == 200, r.text
    finally:
        capture.shutdown()


# --------------------------------------------------------------------------
# #11: no message is accepted while the container is being stopped
# --------------------------------------------------------------------------


def test_message_sent_during_container_stop_is_refused(live_server, client_factory) -> None:
    c = client_factory(live_server)
    register(c)
    agent = create_ready_agent(c)
    runtime = live_server.ctx.runtime
    real_stop = runtime.stop
    in_stop = threading.Event()

    async def slow_stop(agent_id: str) -> None:
        in_stop.set()
        await asyncio.sleep(1.5)
        await real_stop(agent_id)

    runtime.stop = slow_stop
    try:
        busy = send_message(c, agent["id"], "SLOW busy")
        result: dict = {}
        stopper = threading.Thread(target=lambda: result.update(
            r=c.post(f"/api/agents/{agent['id']}/container/stop")))
        stopper.start()
        assert in_stop.wait(20)
        r = c.post(f"/api/agents/{agent['id']}/messages", json={"text": "sneak in"})
        assert r.status_code == 409, r.text
        stopper.join(30)
        assert result["r"].status_code == 200
        wait_turn_completed(c, agent["id"], busy, timeout=20)
    finally:
        runtime.stop = real_stop
    # Once the stop is done, messages are accepted again.
    assert c.post(f"/api/agents/{agent['id']}/messages", json={"text": "hi"}).status_code == 202


# --------------------------------------------------------------------------
# #24: a container left by a failed setup can be stopped by its owner
# --------------------------------------------------------------------------


def test_stop_is_allowed_after_a_failed_setup(live_server, client_factory) -> None:
    c = client_factory(live_server)
    register(c)
    agent = create_ready_agent(c)
    live_server.ctx.db.execute(
        "UPDATE agents SET setup_state = 'error' WHERE id = ?", (agent["id"],))
    assert c.post(f"/api/agents/{agent['id']}/container/stop").status_code == 200
    assert c.post(f"/api/agents/{agent['id']}/container/start").status_code == 409


# --------------------------------------------------------------------------
# #9: a model the operator no longer offers fails fast with a clear message
# --------------------------------------------------------------------------


def test_turn_on_a_model_no_longer_offered_fails_fast(live_server, client_factory) -> None:
    c = client_factory(live_server)
    register(c)
    agent = create_ready_agent(c)
    live_server.ctx.db.execute(
        "UPDATE agents SET model = 'gpt-retired' WHERE id = ?", (agent["id"],))
    done = wait_turn_completed(c, agent["id"], send_message(c, agent["id"], "hello"), timeout=20)
    assert done["data"]["status"] == "failed"
    assert done["data"]["error_kind"] == "model_unavailable"
    assert "no longer offered" in done["data"]["error_message"]
    wait_for(lambda: not get_agent(c, agent["id"])["chat_busy"], timeout=10, message="idle")


# --------------------------------------------------------------------------
# #21: unencodable text outside the auth bodies is a 400, not a 500
# --------------------------------------------------------------------------


def test_lone_surrogate_in_an_agent_name_is_400(live_server, client_factory) -> None:
    c = client_factory(live_server)
    register(c)
    r = c.post("/api/agents", content=b'{"name": "bad \\ud800", "model": "gpt-6-luna"}',
               headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "The request contains invalid text."
