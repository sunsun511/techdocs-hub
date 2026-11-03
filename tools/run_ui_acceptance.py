"""Run browser acceptance only on new disposable data, with owned process cleanup."""

import argparse
import json
import os
import subprocess
import sys
import time
import threading
import socket
from pathlib import Path
import psutil
import httpx

p = argparse.ArgumentParser()
p.add_argument("--private", type=Path, required=True)
p.add_argument("--evidence", type=Path, required=True)
p.add_argument("--script", choices=["e2e.cjs", "supplement.cjs", "ui-polish.cjs"], default="e2e.cjs")
args = p.parse_args()
C = Path(__file__).resolve().parents[1]
P = args.private.resolve()
E = args.evidence.resolve()
assert not P.is_relative_to(C) and not E.is_relative_to(C)
with socket.socket() as port_guard:
    port_guard.bind(("127.0.0.1", 8388))
P.mkdir(parents=True, exist_ok=False)
E.mkdir(parents=True, exist_ok=False)
env = {
    **os.environ,
    "PYTHONPATH": str(C),
    "PYTHON_EXECUTABLE": sys.executable,
    "PYTHONDONTWRITEBYTECODE": "1",
    "TECHDOCS_DATA_ROOT": str(P),
    "TECHDOCS_WORKER": "1",
    "UI_PRIVATE": str(P),
    "UI_EVIDENCE": str(E / "browser"),
}
subprocess.run(["node", "--check", str(C / "tests/browser" / args.script)], check=True)
subprocess.run(
    [
        sys.executable,
        "-B",
        "-m",
        "tools.ui_fixture",
        "--database",
        str(P / "ui.sqlite3"),
        "--credentials",
        str(P / "credentials.json"),
    ],
    cwd=C,
    env=env,
    check=True,
)
server = None
done = threading.Event()
samples = []


def monitor():
    while not done.wait(0.5):
        try:
            if server and server.poll() is None:
                procs = [psutil.Process(server.pid), *psutil.Process(server.pid).children(recursive=True)]
                samples.append({"time": time.time(), "rss": {str(x.pid): x.memory_info().rss for x in procs}})
        except psutil.NoSuchProcess:
            pass


try:
    with (P / "server.log").open("wb") as f:
        server = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(C / "tools/drill_server.py"),
                "--database",
                str(P / "ui.sqlite3"),
                "--models",
                str(Path(os.environ.get("TECHDOCS_MODEL_DIR", C / "models"))),
                "--port",
                "8388",
                "--boot",
                str(P / "boot.json"),
                "--worker",
            ],
            cwd=C,
            env=env,
            stdout=f,
            stderr=f,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=monitor, daemon=True).start()
        with httpx.Client(trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 60
            while True:
                assert server.poll() is None, "Service exited"
                try:
                    if client.get("http://127.0.0.1:8388/api/health").status_code == 200:
                        owned = {
                            server.pid,
                            *(x.pid for x in psutil.Process(server.pid).children(recursive=True)),
                        }
                        listeners = [
                            x
                            for x in psutil.net_connections("tcp")
                            if x.status == "LISTEN" and x.laddr.port == 8388
                        ]
                        assert listeners and all(x.pid in owned for x in listeners), (
                            "Port is not owned by test service"
                        )
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline
                time.sleep(0.2)
        r = subprocess.run(
            ["node", str(C / "tests/browser" / args.script)],
            cwd=C,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        password = json.loads((P / "credentials.json").read_text())["password"]
        (E / "console.log").write_text(
            (r.stdout + r.stderr).replace(password, "[REDACTED]"), encoding="utf-8"
        )
        print(r.stdout)
        print(r.stderr)
        code = r.returncode
finally:
    if server and server.poll() is None:
        procs = [*psutil.Process(server.pid).children(recursive=True), psutil.Process(server.pid)]
        for x in procs:
            try:
                x.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(procs, timeout=10)
        assert not alive, "Cleanup failed"
    done.set()
    (E / "resources.json").write_text(json.dumps(samples), encoding="utf-8")
    (E / "lifecycle.json").write_text(
        json.dumps(
            {
                "pid": server.pid if server else None,
                "stopped": True,
                "database": str(P / "ui.sqlite3"),
                "worker": True,
                "code": str(C),
            }
        ),
        encoding="utf-8",
    )
sys.exit(code)
