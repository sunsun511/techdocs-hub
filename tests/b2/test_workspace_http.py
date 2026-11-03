import b2_support as support
from contextlib import closing
import json
import os
import socket
import subprocess
import sys
import time
import httpx
import psutil
from test_workspace import Actor, evidence, payload, protected

from app import migration as m

pytest_plugins = ["test_workspace"]


def test_real_loopback_roles_revocations_owner_handoff_persist_across_restart(env):
    path = env["path"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = "http://127.0.0.1:" + str(port)
    children = []
    servers = []
    clients = []
    script = support.CODE_ROOT / "tools/serve_verified.py"

    def start():
        started = time.time()
        child = subprocess.Popen(
            [sys.executable, "-B", str(script), "--database", str(path), "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        children.append(child)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and child.poll() is None:
            for file in path.parent.glob(f"service-ready-{port}-*.json"):
                info = json.loads(file.read_text())
                if info["started_at"] < started or not psutil.pid_exists(info["pid"]):
                    continue
                proc = psutil.Process(info["pid"])
                cmd = " ".join(proc.cmdline())
                if str(script) not in cmd or str(path) not in cmd:
                    continue
                try:
                    if httpx.get(origin + "/api/health", timeout=0.3, trust_env=False).status_code == 200:
                        assert all(s["pid"] != info["pid"] for s in servers), (
                            "Must start a new actual server process"
                        )
                        servers.append(info)
                        return child, info
                except httpx.HTTPError:
                    pass
            time.sleep(0.05)
        raise AssertionError("Verified loopback server did not become ready")

    def stop(child, info):
        # Verify ownership immediately before terminating this actual server,
        # including Windows virtualenv redirector's descendant process.
        if psutil.pid_exists(info["pid"]):
            proc = psutil.Process(info["pid"])
            cmd = " ".join(proc.cmdline())
            assert str(script) in cmd and str(path) in cmd
            proc.terminate()
        out, err = child.communicate(timeout=10)
        assert not out and not err, "No HTTP credentials/access bodies may be logged"
        assert not psutil.pid_exists(info["pid"])

    try:
        child, server = start()
        actors = {}
        for name, seed in env["actors"].items():
            client = httpx.Client(base_url=origin, headers={"Origin": origin}, trust_env=False, timeout=5)
            clients.append(client)
            actors[name] = Actor(None, name, seed.pw, client=client)
        A, B = env["A"], env["B"]
        alice, bob, carol, dave = map(actors.get, ["alice", "bob", "carol", "dave"])
        assert alice.request("GET", f"/api/workspaces/{A}/documents").status_code == 200
        assert alice.request("GET", f"/api/workspaces/{B}/documents").status_code == 404
        r = bob.request("POST", f"/api/workspaces/{A}/search", {"query": "sharedterm", "method": "bm25"})
        assert r.status_code == 200 and "TENANT_B" not in r.text
        assert (
            bob.request(
                "POST", f"/api/workspaces/{B}/documents", payload("B", "http-viewer-denied")
            ).status_code
            == 403
        )
        r = carol.request("POST", f"/api/workspaces/{B}/documents", payload("B", "http-editor-allowed"))
        assert r.status_code == 202
        tid = r.json()["task"]["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if carol.request("GET", f"/api/workspaces/{B}/tasks/{tid}").json()["state"] == "succeeded":
                break
            time.sleep(0.05)
        assert carol.request("GET", f"/api/workspaces/{B}/tasks/{tid}").json()["state"] == "succeeded"
        assert alice.request("DELETE", f"/api/workspaces/{A}/members/{bob.uid}").status_code == 204
        assert bob.request("GET", f"/api/workspaces/{A}/documents").status_code == 404
        assert bob.request("GET", f"/api/workspaces/{B}/documents").status_code == 200
        assert (
            dave.request("PUT", f"/api/workspaces/{B}/members/{carol.uid}", {"role": "viewer"}).status_code
            == 200
        )
        assert (
            carol.request(
                "POST", f"/api/workspaces/{B}/documents", payload("B", "http-demoted-denied")
            ).status_code
            == 403
        )
        before = protected(env)
        assert dave.request("DELETE", f"/api/workspaces/{B}/members/{dave.uid}").status_code == 409
        assert protected(env) == before
        assert (
            dave.request("PUT", f"/api/workspaces/{B}/members/{bob.uid}", {"role": "owner"}).status_code
            == 200
        )
        assert dave.request("DELETE", f"/api/workspaces/{B}/membership").status_code == 204
        assert dave.request("GET", f"/api/workspaces/{B}/documents").status_code == 404
        stop(child, server)
        child, server = start()
        # Same real Cookie jars: no re-login, proving DB-bound live roles after restart.
        assert bob.request("GET", f"/api/workspaces/{A}/documents").status_code == 404
        assert bob.request("GET", f"/api/workspaces/{B}/members").status_code == 200
        assert carol.request("GET", f"/api/workspaces/{B}/documents").status_code == 200
        assert (
            carol.request(
                "POST", f"/api/workspaces/{B}/documents", payload("B", "http-still-demoted")
            ).status_code
            == 403
        )
        assert dave.request("GET", f"/api/workspaces/{B}/documents").status_code == 404
        before = protected(env)
        assert (
            bob.request("PUT", f"/api/workspaces/{B}/members/{bob.uid}", {"role": "viewer"}).status_code
            == 409
        )
        assert protected(env) == before
        stop(child, server)
    finally:
        for client in clients:
            client.close()
        for child, info in zip(children, servers):
            if child.poll() is None:
                stop(child, info)
        for child in children:
            if child.poll() is None:
                owned = psutil.Process(child.pid).children(recursive=True)
                for proc in owned:
                    if str(script) in " ".join(proc.cmdline()) and str(path) in " ".join(proc.cmdline()):
                        proc.terminate()
                child.terminate()
                child.communicate(timeout=10)
    assert len(children) == len(servers) == 2
    with closing(m.connect(path, readonly=True)) as db:
        m.integrity(db)
    evidence(
        "real-workspace-http",
        {
            "actual_server_boots": servers,
            "HTTP_starts": 2,
            "all_stopped": True,
            "same_cookies_after_restart": True,
            "member_removal_persisted": True,
            "role_downgrade_persisted": True,
            "owner_handoff_persisted": True,
            "last_owner_409_and_no_db_change": True,
            "real_worker_publication": True,
            "model_inference": "BM25; ONNX disabled",
        },
    )
