"""Execute the image's actual health command against the real HTTP boundary.

Only urllib's network transport is adapted to TestClient; credentials, database,
app factory, Host/Origin middleware, and health route are real and isolated.
"""

from contextlib import closing
import json
from pathlib import Path
import re
import runpy
import shlex
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient
from app import migration
from app.config import Settings
from app.main import create_app
from tools.api_client import bootstrap_database

ROOT = Path(__file__).resolve().parents[1]


def health_command():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instruction = dockerfile.split("HEALTHCHECK ", 1)[1].replace("\\\n", " ")
    command = instruction.split("CMD ", 1)[1].splitlines()[0].strip()
    return json.loads(command) if command.startswith("[") else shlex.split(command)


def run_image_health():
    command = health_command()
    assert command[0] == "python"
    if command[1] == "-c":
        exec(compile(command[2], "<Dockerfile HEALTHCHECK>", "exec"), {"__name__": "__main__"})
    else:
        assert command[1] == "-m" and len(command) == 3
        runpy.run_module(command[2], run_name="__main__")


def configured_origin(case):
    if case == "image-default":
        return re.search(r"TECHDOCS_ORIGIN=(\S+)", (ROOT / "Dockerfile").read_text(encoding="utf-8")).group(1)
    template = re.search(r'TECHDOCS_ORIGIN: "([^"]+)"', (ROOT / "compose.yaml").read_text(encoding="utf-8")).group(1)
    if case == "compose-default":
        return re.sub(r"\$\{TECHDOCS_PORT:-(\d+)\}", r"\1", template)
    port = "18477"
    if case == "acceptance-default":
        port = re.search(r'"--port", type=int, default=(\d+)', (ROOT / "tools/container_check.py").read_text(encoding="utf-8")).group(1)
    return re.sub(r"\$\{TECHDOCS_PORT:-\d+\}", port, template)


def records(path):
    with closing(migration.connect(path, readonly=True)) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return migration.fingerprint({t: db.execute('SELECT * FROM "' + t + '"').fetchall() for t in tables})


@pytest.mark.parametrize("case", ["image-default", "compose-default", "acceptance-default", "custom-port"])
def test_image_health_and_host_origin_contract(tmp_path, monkeypatch, case):
    origin = configured_origin(case)
    monkeypatch.setenv("TECHDOCS_ORIGIN", origin)
    path = tmp_path / "health.sqlite3"
    _, password = bootstrap_database(path)
    (tmp_path / "security-secrets-health.json").write_text(json.dumps([password]))
    app = create_app(Settings(db_path=path, origin=origin, models_enabled=False, worker_enabled=False))
    with TestClient(app, base_url="http://127.0.0.1:8377") as client:
        before = records(path)
        seen = []

        def transport(request, timeout):
            request = Request(request) if isinstance(request, str) else request
            assert request.full_url == "http://127.0.0.1:8377/api/health"
            assert timeout == 3
            headers = dict(request.header_items())
            headers.setdefault("Host", request.host)
            result = client.get(request.full_url, headers=headers)
            seen.append(result.status_code)
            if result.status_code >= 400:
                raise HTTPError(request.full_url, result.status_code, result.text, {}, None)
            # urllib HTTPResponse is a context manager with a numeric status.
            class Response:
                status = result.status_code

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return Response()

        monkeypatch.setattr("urllib.request.urlopen", transport)
        run_image_health()
        assert seen == [200]
        assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
        expected_host = origin.split("://", 1)[1]
        login = {"username": "test-owner", "password": password}
        for bad_origin in (None, "http://evil.example", origin + "/"):
            headers = {"Host": expected_host}
            if bad_origin is not None:
                headers["Origin"] = bad_origin
            response = client.post("/api/auth/login", json=login, headers=headers)
            assert response.status_code == 403
        assert client.post("/api/auth/login", json=login, headers={"Host": "evil.example", "Origin": origin}).status_code == 400
        assert records(path) == before


@pytest.mark.parametrize("error", [HTTPError("http://127.0.0.1:8377/api/health", 503, "Unavailable", {}, None), URLError("connection refused"), TimeoutError("timed out")])
def test_image_health_propagates_failure(monkeypatch, error):
    monkeypatch.setenv("TECHDOCS_ORIGIN", configured_origin("acceptance-default"))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(type(error)):
        run_image_health()


def test_image_health_requires_http_200(monkeypatch):
    monkeypatch.setenv("TECHDOCS_ORIGIN", configured_origin("acceptance-default"))

    class EmptyResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: EmptyResponse())
    with pytest.raises(RuntimeError, match="HTTP 200"):
        run_image_health()


def test_image_health_missing_origin_fails_before_network(monkeypatch):
    monkeypatch.delenv("TECHDOCS_ORIGIN", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("No request should be sent without explicit Origin configuration")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(KeyError, match="TECHDOCS_ORIGIN"):
        run_image_health()
