from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone, timedelta
import threading
from test_workspace import protected, denied, payload, evidence

from app import migration as m

pytest_plugins = ["test_workspace"]


def test_nonmember_creates_own_workspace_anonymous_cannot(env):
    denied(env, env["anonymous"], "POST", "/api/workspaces", {"name": "Anonymous"}, 401)
    eve = env["actors"]["eve"]
    r = eve.request("POST", "/api/workspaces", {"name": "Eve workspace"})
    assert r.status_code == 201 and r.json()["role"] == "owner"
    ws = r.json()["id"]
    assert eve.request("GET", f"/api/workspaces/{ws}/documents").json() == {"documents": []}
    denied(env, env["actors"]["alice"], "GET", f"/api/workspaces/{ws}/documents", None, 404)


def test_workspace_list_final_snapshot_after_member_removed(env):
    bob, alice = env["actors"]["bob"], env["actors"]["alice"]
    reached = threading.Event()
    release = threading.Event()

    def hook(stage):
        if stage == "read.before_final":
            reached.set()
            assert release.wait(5)

    env["rt"].hook = hook
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bob.request, "GET", "/api/workspaces")
        try:
            assert reached.wait(5)
            assert alice.request("DELETE", f"/api/workspaces/{env['A']}/members/{bob.uid}").status_code == 204
        finally:
            release.set()
        r = future.result(timeout=5)
    assert r.status_code == 200 and {w["id"] for w in r.json()["workspaces"]} == {env["B"]}


def test_private_search_session_expires_before_final_no_publication(env):
    bob = env["actors"]["bob"]
    reached = threading.Event()
    release = threading.Event()

    def hook(stage):
        if stage == "search.before_final":
            reached.set()
            assert release.wait(5)

    env["rt"].hook = hook
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            bob.request,
            "POST",
            f"/api/workspaces/{env['A']}/search",
            {"query": "sharedterm", "method": "bm25"},
        )
        try:
            assert reached.wait(5)
            with closing(m.connect(env["path"])) as db:
                db.execute(
                    "UPDATE sessions SET idle_expires_at=? WHERE user_id=?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), bob.uid),
                )
            before = protected(env)
        finally:
            release.set()
        r = future.result(timeout=5)
    assert r.status_code == 401 and protected(env) == before


def test_namespaced_import_buffer_and_utf8_size_preserved(env):
    alice = env["actors"]["alice"]
    path = f"/api/workspaces/{env['A']}/documents"
    small = {**payload("A", "large-valid"), "content": "sharedterm " + "x" * 20000}
    assert alice.request("POST", path, small).status_code == 202
    oversized = {**payload("A", "large-invalid"), "content": "字" * 400000}
    denied(env, alice, "POST", path, oversized, 413)
    evidence(
        "upload-boundary",
        {"accepted_bytes_above_16KiB": True, "UTF8_over_1MB_denied": True, "denied_state_unchanged": True},
    )
