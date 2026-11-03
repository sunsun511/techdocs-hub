from concurrent.futures import ThreadPoolExecutor
import threading
from fastapi.testclient import TestClient
from app.main import create_app
from app.config import Settings
from conftest import import_doc, search, process
from tools.api_client import WorkspaceClient
from app.workspace_api import ScopedSearch


def test_import_search_and_source_context(client):
    task = import_doc(
        client, content="# Retention\n\nRetention policy is seven days.\n\nKeep an offline copy."
    )
    assert search(client) == []  # queued versions are not partially visible
    process(client)
    result = search(client)[0]
    assert result["version_id"] == task["version_id"]
    assert result["source_url"] == "https://example.org/policy"
    assert result["heading"] == "Retention"
    assert "seven" in result["text"]
    passage = client.get("/api/passages/" + result["chunk_id"]).json()
    assert passage["is_active"] is True
    assert any("seven days" in p["text"] for p in passage["neighbors"])


def test_repeated_import_and_concurrent_idempotency(client):
    payload = dict(key="policy", title="Policy", version_label="v1", content="Retention is seven days.")

    def submit(_):
        return client.post("/api/documents", json=payload, headers={"Idempotency-Key": "same-request"})

    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(submit, range(12)))
    assert sum(r.status_code == 202 for r in replies) == 1
    assert all(r.status_code in [200, 202] for r in replies)
    assert len({r.json()["task"]["id"] for r in replies}) == 1
    process(client)
    count = len(search(client))
    duplicate = client.post("/api/documents", json=payload)
    assert duplicate.json()["duplicate"]
    assert len(search(client)) == count
    changed = {**payload, "content": "Retention is fourteen days."}
    assert (
        client.post("/api/documents", json=changed, headers={"Idempotency-Key": "same-request"}).status_code
        == 409
    )
    assert client.post("/api/documents", json=changed).status_code == 409


def test_atomic_update_and_historical_filter(client):
    old = import_doc(client)
    process(client)
    new = import_doc(client, "v2", "Retention policy is fourteen days.")
    assert search(client)[0]["version_label"] == "v1"
    process(client)
    assert {r["version_label"] for r in search(client)} == {"v2"}
    assert all("seven" not in r["text"] for r in search(client))
    history = search(client, document_id=old["document_id"], version_id=old["version_id"])
    assert history[0]["version_label"] == "v1"
    assert (
        client.post("/api/search", json={"query": "retention", "version_id": old["version_id"]}).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/documents/{old['document_id']}/active-version", json={"version_id": old["version_id"]}
        ).status_code
        == 200
    )
    assert {r["version_label"] for r in search(client)} == {"v1"}
    assert client.get(f"/api/tasks/{new['id']}").json()["state"] == "succeeded"


def test_publication_transaction_rolls_back_and_retry_works(client, monkeypatch):
    old = import_doc(client)
    process(client)
    store = client.scope
    generation = store.generation()
    new = import_doc(client, "v2", "Retention policy is fourteen days.")
    with monkeypatch.context() as patch:

        def crash(stage):
            if stage == "publish.after_chunks":
                raise RuntimeError("injected failure before commit")

        patch.setattr(client.app.state.runtime, "hook", crash)
        process(client)
    assert store.generation() == generation
    assert client.get(f"/api/tasks/{new['id']}").json()["state"] == "failed"
    assert search(client)[0]["version_id"] == old["version_id"]
    with client.app.state.auth.transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM chunks WHERE version_id=?", (new["version_id"],)).fetchone()[0]
            == 0
        )
    assert client.post(f"/api/tasks/{new['id']}/retry").status_code == 200
    process(client)
    assert search(client)[0]["version_id"] == new["version_id"]
    assert client.get(f"/api/tasks/{new['id']}").json()["attempts"] == 2


def delayed_build(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.runtime.models.embed

    def blocked(texts, prefix):
        entered.set()
        assert release.wait(10)
        return original(texts, prefix)

    monkeypatch.setattr(client.app.state.runtime.models, "embed", blocked)
    thread = threading.Thread(target=client.app.state.runtime.worker.process_once)
    thread.start()
    assert entered.wait(5)
    return thread, release


def test_late_old_worker_cannot_overwrite_new_request(client, monkeypatch):
    old = import_doc(client)
    thread, release = delayed_build(client, monkeypatch)
    new = import_doc(client, "v2", "Retention policy is fourteen days.")
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert client.get(f"/api/tasks/{old['id']}").json()["state"] == "superseded"
    process(client)
    assert search(client)[0]["version_id"] == new["version_id"]


def test_delete_during_build_prevents_late_publication(client, monkeypatch):
    task = import_doc(client)
    thread, release = delayed_build(client, monkeypatch)
    assert client.delete(f"/api/documents/{task['document_id']}").status_code == 200
    release.set()
    thread.join(10)
    assert search(client) == []
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "cancelled"
    assert client.post(f"/api/tasks/{task['id']}/retry").status_code == 409


def test_delete_removes_cached_candidates_and_passages(client):
    task = import_doc(client)
    process(client)
    chunk = search(client)[0]
    assert client.delete(f"/api/documents/{task['document_id']}").json()["deleted"]
    assert search(client) == []
    assert (
        client.post(
            "/api/search",
            json={"query": "retention", "document_id": task["document_id"], "version_id": task["version_id"]},
        ).status_code
        == 404
    )
    assert client.get("/api/passages/" + chunk["chunk_id"]).status_code == 404
    assert client.delete(f"/api/documents/{task['document_id']}").status_code == 200


def test_delete_during_search_invalidates_inflight_snapshot(client, monkeypatch):
    task = import_doc(client)
    process(client)
    rt = client.app.state.runtime
    engine = ScopedSearch(rt, client.scope)
    generation, index = engine.index(None, None)
    rt.cache.publish(client.workspace_id, generation, engine.staged)
    score = index.score

    def delete_before_ranking(query):
        client.scope.delete_document(task["document_id"])
        return score(query)

    monkeypatch.setattr(index, "score", delete_before_ranking)
    assert search(client) == []


def test_version_switch_cancels_pending_updates(client):
    old = import_doc(client)
    process(client)
    ready = import_doc(client, "v2", "Retention policy is fourteen days.")
    process(client)
    pending = import_doc(client, "v3", "Retention policy is thirty days.")
    assert (
        client.post(
            f"/api/documents/{old['document_id']}/active-version", json={"version_id": old["version_id"]}
        ).status_code
        == 200
    )
    assert client.get(f"/api/tasks/{pending['id']}").json()["state"] == "superseded"
    assert not client.app.state.runtime.worker.process_once()
    assert search(client)[0]["version_id"] == old["version_id"]
    assert ready["version_id"] != old["version_id"]


def test_reopen_persists_index_and_recovers_claimed_task(client):
    old = import_doc(client)
    process(client)
    task = import_doc(client, "v2", "Retention policy is fourteen days.")
    claimed = client.app.state.runtime.internal.claim_task()
    assert claimed["id"] == task["id"]
    with TestClient(
        create_app(Settings(db_path=client.app.state.auth.path, models_enabled=False, worker_enabled=False)),
        base_url="http://127.0.0.1:8377",
        headers={"Origin": "http://127.0.0.1:8377"},
        cookies=client.raw.cookies,
    ) as raw:
        reopened = WorkspaceClient(raw, client.workspace_id, client.csrf)
        assert reopened.get("/api/health").json()["recovered_running_tasks"] == 1
        assert search(reopened)[0]["version_id"] == old["version_id"]
        process(reopened)
        assert search(reopened)[0]["version_id"] == task["version_id"]


def test_unindexable_update_preserves_previous_version(client):
    old = import_doc(client)
    process(client)
    task = import_doc(client, "v2", "<script>no searchable content</script>", format="html")
    process(client)
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "failed"
    assert search(client)[0]["version_id"] == old["version_id"]


def test_invalid_version_not_cross_document_access(client):
    a = import_doc(client)
    b = import_doc(client, key="another")
    process(client)
    process(client)
    assert (
        client.post(
            f"/api/documents/{a['document_id']}/active-version", json={"version_id": b["version_id"]}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/search",
            json={"query": "retention", "document_id": a["document_id"], "version_id": b["version_id"]},
        ).status_code
        == 404
    )


def test_low_matching_candidates_not_hidden_by_refusal_gate(client):
    import_doc(client, content="Retention policy describes storage and archived files.")
    process(client)
    rows = search(client, "retention nonexistenttopic weather nonexistentpricing billing ghost")
    assert rows and rows[0]["match_hint"] == "low"


def test_models_missing_are_explicit_and_do_not_fake_translation(client):
    response = client.post("/api/search", json={"query": "默认快照文件名是什么？"})
    assert response.status_code == 503
    assert "中文翻译模型" in response.json()["detail"]
    assert client.post("/api/search", json={"query": "retention", "method": "dense"}).status_code == 503


def test_validation_and_size_boundary(client):
    bad = dict(
        key="policy", title="Policy", version_label="v1", content="text", source_url="javascript:alert(1)"
    )
    assert client.post("/api/documents", json=bad).status_code == 422
    assert client.post("/api/search", json={"query": "   "}).status_code == 422
    assert client.post("/api/search", json={"query": "retention", "top_k": 1000}).status_code == 422
    bad["source_url"] = ""
    bad["content"] = "字" * 400_000
    assert client.post("/api/documents", json=bad).status_code == 413


def test_health_and_ui_available(client):
    assert client.get("/").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/openapi.json").json()["info"]["version"] == "0.1.0"
