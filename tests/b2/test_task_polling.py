"""Deterministic real-loopback task publication and authorization races."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
import httpx
import pytest
import uvicorn
from test_workspace import Actor, ORIGIN, payload, protected

pytest_plugins = ["test_workspace"]


@contextmanager
def loopback(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", lifespan="off", proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
                          headers={"Origin": ORIGIN, "Host": urlsplit(ORIGIN).netloc},
                          trust_env=False, timeout=10) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("change,expected", [
    ("publish", 200), ("revoke", 404), ("role", 409), ("session", 401),
    ("aba", 409), ("foreign", 404), ("list_generation", 409),
])
def test_task_polling_real_http_races(env, change, expected):
    rt, ws = env["rt"], env["A"]
    alice, seed = env["actors"]["alice"], env["actors"]["bob"]
    created = alice.request("POST", f"/api/workspaces/{ws}/documents", payload("A", "poll-race"))
    assert created.status_code == 202
    task = created.json()["task"]
    events = []

    def record(stage, **extra):
        events.append({"stage": stage, "monotonic_ns": time.monotonic_ns(), **extra})

    reached, release = threading.Event(), threading.Event()

    def hook(stage):
        if stage == "read.before_final":
            record("read_snapshot_finished")
            reached.set()
            if not release.wait(5):
                raise RuntimeError("poll race barrier timeout")
        elif stage == "publish.after_chunks":
            record("publish_transaction_uncommitted")

    with loopback(env["app"]) as client:
        actor = Actor(None, "bob", seed.pw, client=client)
        rt.hook = hook
        path = f"/api/workspaces/{ws}/tasks/{task['id']}"
        if change == "foreign":
            path = f"/api/workspaces/{env['B']}/tasks/{task['id']}"
            before = protected(env)
            response = actor.request("GET", path)
            assert protected(env) == before
        else:
            if change == "list_generation":
                path = f"/api/workspaces/{ws}/documents"
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(actor.request, "GET", path)
                try:
                    assert reached.wait(5)
                    if change in ("publish", "list_generation"):
                        assert rt.worker.process_once()
                        record("publish_committed")
                    elif change in ("revoke", "aba"):
                        assert alice.request("DELETE", f"/api/workspaces/{ws}/members/{actor.uid}").status_code == 204
                        if change == "aba":
                            assert alice.request("POST", f"/api/workspaces/{ws}/members", {"username": "bob", "role": "editor"}).status_code == 201
                        record("membership_change_committed")
                    elif change == "role":
                        assert alice.request("PUT", f"/api/workspaces/{ws}/members/{actor.uid}", {"role": "viewer"}).status_code == 200
                        record("role_change_committed")
                    else:
                        rt.auth.offline_revoke(actor.uid)
                        record("session_revoked")
                    before = protected(env)
                finally:
                    release.set()
                response = pending.result(timeout=5)
            assert protected(env) == before
        rt.hook = lambda _: None
        record("http_response", status=response.status_code, body=response.json())
        evidence = {"scenario": change, "events": events, "transport": "real_loopback_http",
                    "credentials_recorded": False, "protected_unchanged_after_concurrent_operation": True}
        output = os.environ.get("TECHDOCS_POLL_EVIDENCE")
        if output:
            target = Path(output)
            target.mkdir(parents=True, exist_ok=True)
            file = target / (change + ".json")
            with file.open("x", encoding="utf-8") as stream:
                json.dump(evidence, stream, ensure_ascii=False, indent=2)
        assert response.status_code == expected, response.text
        if change == "publish":
            assert response.json()["id"] == task["id"]
            assert response.json()["state"] == "succeeded"
        elif change == "list_generation":
            assert response.json()["detail"] == "Workspace data changed; retry request"
        else:
            assert "state" not in response.json()
