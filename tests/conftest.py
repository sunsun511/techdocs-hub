from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from tools.api_client import bootstrap_database, authenticated
from tools.test_context import ensure_context
import os
import uuid


def pytest_configure(config):
    root = ensure_context()
    private = Path(os.environ["TECHDOCS_DATA_ROOT"]).resolve()
    if config.option.basetemp:
        target = Path(config.option.basetemp).resolve()
        if not target.is_relative_to(private) or target.exists():
            raise pytest.UsageError("Use a new basetemp inside the isolated data root")
    else:
        config.option.basetemp = str(private / ("cases-" + uuid.uuid4().hex))
    config._techdocs_evidence_root = root


@pytest.fixture
def client(tmp_path: Path):
    path = tmp_path / "test.sqlite3"
    owner, password = bootstrap_database(path)
    app = create_app(Settings(db_path=path, models_enabled=False, worker_enabled=False))
    with TestClient(app, base_url="http://127.0.0.1:8377", headers={"Origin": "http://127.0.0.1:8377"}) as c:
        yield authenticated(c, owner, password, registry=tmp_path / "security-secrets-engineering.json")


def import_doc(client, label="v1", content="Retention policy is seven days.", key="policy", **extra):
    payload = {
        "key": key,
        "title": "Retention policy",
        "version_label": label,
        "source_url": "https://example.org/policy",
        "format": "markdown",
        "content": content,
    }
    payload.update(extra)
    response = client.post("/api/documents", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["task"]


def search(client, query="retention", **extra):
    response = client.post("/api/search", json={"query": query, "method": "bm25", **extra})
    assert response.status_code == 200, response.text
    return response.json()["results"]


def process(client):
    assert client.app.state.runtime.worker.process_once()
