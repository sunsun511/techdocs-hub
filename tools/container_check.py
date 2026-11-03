"""Isolated Compose lifecycle smoke; engine is never installed by this tool."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
import secrets

import httpx
from tools.api_client import authenticated

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/docker.json")
    parser.add_argument("--port", type=int, default=8378)
    parser.add_argument("--models", action="store_true", help="Use existing fixed models, never download")
    parser.add_argument("--require-engine", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "not_run",
        "docker_restart_verified": False,
        "models_enabled": args.models,
        "checks": [],
    }

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not shutil.which("docker"):
        report["reason"] = "docker executable unavailable; no installation attempted"
        save()
        print(json.dumps(report))
        if args.require_engine:
            raise SystemExit(2)
        return
    project = "techdocs-check-" + uuid.uuid4().hex[:10]
    report["isolated_project"] = project
    env = {**os.environ, "TECHDOCS_PORT": str(args.port), "TECHDOCS_MODELS": "1" if args.models else "0"}
    command = ["docker", "compose", "-p", project]
    logs = args.output.with_suffix(".log")

    def compose(*arguments, checked=True):
        cmd = command + list(arguments)
        with logs.open("ab") as stream:
            stream.write(("\nCOMMAND " + json.dumps(cmd) + "\n").encode())
            return subprocess.run(
                cmd, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, check=checked, timeout=600
            )

    def wait(client):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                response = client.get("/api/health")
                if response.status_code == 200:
                    return response.json()
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise TimeoutError("Container health timeout")

    started = False
    try:
        with logs.open("wb") as stream:
            subprocess.run(
                ["docker", "info"], stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=30
            )
        (ROOT / "models").mkdir(exist_ok=True)
        if args.models:
            from app.models import Models

            verifier = Models(ROOT / "models")
            assert verifier.dense_ready and verifier.translation_ready, verifier.errors
            del verifier
        started = True
        compose("build", "web")
        password = secrets.token_urlsafe(24)
        initialized = subprocess.run(
            command
            + [
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "web",
                "python",
                "-m",
                "tools.container_fixture",
                "--database",
                "/data/techdocs.sqlite3",
                "--disposable",
            ],
            cwd=ROOT,
            env=env,
            input=json.dumps({"password": password}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=60,
        )
        owner = json.loads(initialized.stdout)
        compose("up", "-d", "--wait", "--wait-timeout", "90")
        origin = f"http://127.0.0.1:{args.port}"
        with httpx.Client(base_url=origin, timeout=120, headers={"Origin": origin}, trust_env=False) as raw:
            wait(raw)
            client = authenticated(raw, owner, password)
            report["health_initial"] = client.get("/api/health").json()
            assert report["health_initial"]["dense_ready"] == args.models
            assert report["health_initial"]["translation_ready"] == args.models
            payload = {"key": "docker-test", "title": "Docker retention policy", "format": "text"}

            def import_version(label, content):
                response = client.post(
                    "/api/documents", json={**payload, "version_label": label, "content": content}
                )
                response.raise_for_status()
                task = response.json()["task"]
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    response = client.get("/api/tasks/" + task["id"])
                    response.raise_for_status()
                    state = response.json()
                    if state["state"] == "succeeded":
                        return task
                    assert state["state"] not in ["failed", "cancelled", "superseded"], state
                    time.sleep(0.1)
                raise TimeoutError("Index task timeout")

            def search(query="retention policy", lang="en"):
                begin = time.perf_counter()
                response = client.post("/api/search", json={"query": query, "language": lang})
                response.raise_for_status()
                data = response.json()
                report.setdefault("search_responses", []).append(
                    {
                        "query": query,
                        "language": lang,
                        "http_ms": (time.perf_counter() - begin) * 1000,
                        "response": data,
                    }
                )
                return data["results"]

            old = import_version("v1", "Retention policy is seven days.")
            assert search()[0]["version_id"] == old["version_id"]
            new = import_version("v2", "Retention policy is fourteen days.")
            assert search()[0]["version_id"] == new["version_id"]
            client.post(
                "/api/documents/" + old["document_id"] + "/active-version",
                json={"version_id": old["version_id"]},
            ).raise_for_status()
            assert search()[0]["version_id"] == old["version_id"]
            report["checks"].append("import_update_switch_version")
            compose("restart", "web")
            report["health_restarted"] = wait(client)
            assert search()[0]["version_id"] == old["version_id"]
            report["checks"].append("index_and_active_version_survive_container_restart")
            if args.models:
                assert search("备份保留策略是多少天？", "zh")
                report["checks"].append("fixed_onnx_english_and_chinese_http_inference")
            client.delete("/api/documents/" + old["document_id"]).raise_for_status()
            assert search() == []
            compose("restart", "web")
            report["health_deleted_restart"] = wait(client)
            assert search() == []
            report["checks"].append("delete_survives_container_restart")
        report.update(status="passed", docker_restart_verified=True)
    except Exception as exc:
        report.update(status="failed", error=repr(exc))
        raise
    finally:
        if started:
            try:
                compose("logs", "--no-color", checked=False)
            except Exception as exc:
                report["log_collection_error"] = repr(exc)
            try:
                compose("down", "--volumes")
                report["owned_stack_removed"] = True
            except Exception as exc:
                report.update(status="failed", cleanup_error=repr(exc), owned_stack_removed=False)
        save()
    print(json.dumps(report, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
