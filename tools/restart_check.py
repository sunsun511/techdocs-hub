"""Real HTTP/process restart test. Uses only disposable temporary test databases."""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import httpx
import psutil

from tools.api_client import bootstrap_database, authenticated
ROOT = Path(__file__).resolve().parents[1]


def check_restart(output=None):
    events = []
    with tempfile.TemporaryDirectory(prefix="techdocs-restart-", dir=os.environ["TECHDOCS_DATA_ROOT"]) as tmp:
        db = Path(tmp) / "app.sqlite3"
        owner, password = bootstrap_database(db)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = {**os.environ, "TECHDOCS_DB_PATH": str(db), "TECHDOCS_MODELS": "0", "TECHDOCS_ORIGIN": f"http://127.0.0.1:{port}"}
        proc = None
        log_path = Path(tmp) / "server.log"
        log_stream = log_path.open("wb")
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False, headers={"Origin":f"http://127.0.0.1:{port}"})

        def start(worker, slow=False):
            nonlocal proc
            if slow:
                # Inject long index construction, not a pre-baked DB status.
                code = f"""import time,uvicorn
from app.config import Settings
from app.main import create_app
from app.models import Models
class SlowModel(Models):
 def embed(self,texts,prefix):
  time.sleep(60)
  return None
uvicorn.run(create_app(Settings.from_env(),SlowModel('.',False)),host='127.0.0.1',port={port})
"""
                cmd = [sys.executable, "-u", "-c", code]
            else:
                cmd = [sys.executable, "-u", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port)]
            proc = subprocess.Popen(cmd, cwd=ROOT, env={**env, "TECHDOCS_WORKER": "1" if worker else "0"}, stdout=log_stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert proc.poll() is None, log_path.read_text(errors="replace")
                try:
                    response = client.raw.get("/api/health") if hasattr(client,"workspace_id") else client.get("/api/health")
                    if response.status_code == 200:
                        return client.get("/api/health").json() if hasattr(client,"workspace_id") else response.json()
                except httpx.TransportError:
                    pass
                time.sleep(.1)
            raise TimeoutError("Server startup timed out")

        def stop():
            nonlocal proc
            if proc and proc.poll() is None:
                # Windows venv python.exe can be a launcher with a real Python
                # child. Stop ONLY descendants of this test's Popen, then owner.
                children = psutil.Process(proc.pid).children(recursive=True)
                for child in reversed(children):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
                _, alive = psutil.wait_procs(children, timeout=5)
                for child in alive:
                    child.kill()
                psutil.wait_procs(alive, timeout=5)
            proc = None

        def wait_task(task, expected):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                response = client.get("/api/tasks/" + task["id"])
                response.raise_for_status()
                data = response.json()
                if data["state"] == expected:
                    return data
                assert data["state"] not in ["failed", "superseded", "cancelled"], data
                time.sleep(.05)
            raise TimeoutError((task, expected))

        try:
            start(False)
            client = authenticated(client, owner, password)
            payload = {"key": "restart-policy", "title": "Restart policy", "version_label": "v1", "format": "text", "content": "Retention policy is seven days."}
            response = client.post("/api/documents", json=payload)
            response.raise_for_status()
            task = response.json()["task"]
            assert task["state"] == "queued"
            stop()
            start(True)
            wait_task(task, "succeeded")
            assert client.post("/api/search", json={"query": "retention"}).json()["results"][0]["version_label"] == "v1"
            events.append({"case": "queued_task_resumes_after_real_process_restart", "passed": True})
            stop()
            start(True, slow=True)
            response = client.post("/api/documents", json={**payload, "version_label": "v2", "content": "Retention policy is fourteen days."})
            response.raise_for_status()
            new_task = response.json()["task"]
            wait_task(new_task, "running")
            assert client.post("/api/search", json={"query": "retention", "method": "bm25"}).json()["results"][0]["version_label"] == "v1"
            stop()  # actual process termination interrupts construction
            health = start(True)
            assert health["recovered_running_tasks"] == 1
            result = wait_task(new_task, "succeeded")
            assert result["attempts"] == 2
            hits = client.post("/api/search", json={"query": "retention"}).json()["results"]
            assert {r["version_label"] for r in hits} == {"v2"}
            assert all("seven" not in r["text"] for r in hits)
            events.append({"case": "crashed_running_worker_recovers_without_partial_publication", "passed": True, "fault": "60s injected index-build delay, terminated owning child process"})
            client.delete("/api/documents/" + task["document_id"]).raise_for_status()
            stop()
            start(True)
            assert client.post("/api/search", json={"query": "retention"}).json()["results"] == []
            events.append({"case": "deleted_document_stays_absent_after_real_process_restart", "passed": True})
        finally:
            stop()
            client.close()
            log_stream.close()
    report = {"checks": events, "passed": len(events), "real_os_processes": True, "docker_tested": False, "models_disabled": True, "production_data_modified": False}
    if output:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(check_restart(ROOT / "artifacts/restart.json"), indent=2))
