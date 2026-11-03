"""Translation overrides through real login and scoped HTTP request handling.

Uses the existing isolated TestClient fixture and real Argon2 authentication.
Models are deliberately disabled to test missing-model and override contracts.
"""

import pytest
from pydantic import ValidationError

from conftest import import_doc, process
from app.schemas import SearchRequest


@pytest.mark.parametrize("method", ["auto", "bm25"])
def test_manual_translation_searches_when_translation_model_is_unavailable(client, method):
    task = import_doc(client, content="Retention policy is seven days.")
    process(client)
    models = client.app.state.runtime.models
    assert not models.translation_ready
    assert not models.dense_ready
    before = client.scope.snapshot(None, None)

    response = client.post("/api/search", json={
        "query": "备份保留策略", "method": method,
        "translation": "  retention policy  ", "document_id": task["document_id"],
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["query"] == "备份保留策略"
    assert result["language"] == "zh"
    assert result["method"] == "bm25"
    assert result["translated_query"] == "retention policy"
    assert result["results"][0]["version_id"] == task["version_id"]
    assert "seven days" in result["results"][0]["text"]
    assert client.scope.snapshot(None, None) == before


@pytest.mark.parametrize("optional", [{}, {"translation": None}, {"translation": " \t\n "}])
def test_missing_null_or_blank_translation_keeps_original_model_error(client, optional):
    response = client.post("/api/search", json={"query": "备份保留策略", **optional})
    assert response.status_code == 503
    assert "翻译" in response.json()["detail"]


def test_manual_translation_does_not_downgrade_explicit_hybrid_without_dense_model(client):
    response = client.post("/api/search", json={
        "query": "备份保留策略", "method": "hybrid", "translation": "retention policy",
    })
    assert response.status_code == 503
    assert "向量模型" in response.json()["detail"]


@pytest.mark.parametrize("query,language,method", [
    ("retention policy", "auto", "auto"),
    ("备份保留策略", "en", "bm25"),
    ("备份保留策略", "zh", "dense"),
])
def test_disallowed_override_is_rejected_before_missing_model_errors(client, query, language, method):
    response = client.post("/api/search", json={
        "query": query, "language": language, "method": method, "translation": "retention policy",
    })
    assert response.status_code == 422
    assert "只有用关键词检索的中文问题" in response.json()["detail"]


@pytest.mark.parametrize("translation", ["x" * 501, 123, ["retention"]],
                         ids=["over-500-characters", "number", "array"])
def test_invalid_translation_payload_returns_field_validation_error(client, translation):
    # The HTTP layer intentionally hides validation details (including submitted input).
    # Check the field boundary directly, then preserve that existing safe HTTP contract.
    with pytest.raises(ValidationError) as error:
        SearchRequest(query="备份保留策略", translation=translation)
    assert any(item["loc"] == ("translation",) for item in error.value.errors())
    response = client.post("/api/search", json={"query": "备份保留策略", "translation": translation})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request"}


@pytest.mark.parametrize("translation", ["x" * 500, "  retention policy  "],
                         ids=["exactly-500-characters", "trim-whitespace"])
def test_valid_translation_length_boundary_and_trimming(client, translation):
    response = client.post("/api/search", json={"query": "备份保留策略", "translation": translation})
    assert response.status_code == 200
    assert response.json()["translated_query"] == translation.strip()
    assert response.json()["method"] == "bm25"


@pytest.mark.parametrize("failure", ["session", "csrf"])
def test_translation_override_does_not_bypass_authentication_or_csrf(client, failure):
    payload = {"query": "备份保留策略", "translation": "retention policy"}
    if failure == "session":
        client.raw.cookies.clear()
        response = client.post("/api/search", json=payload)
        assert response.status_code == 401
    else:
        response = client.post("/api/search", json=payload, headers={"X-CSRF-Token": "invalid"})
        assert response.status_code == 403


def test_translation_override_cannot_select_a_document_from_another_workspace(client):
    task = import_doc(client, content="Retention policy is seven days.")
    process(client)
    created = client.raw.post("/api/workspaces", json={"name": "Other test workspace"})
    assert created.status_code == 201, created.text
    other = created.json()["id"]
    before = client.scope.snapshot(None, None)
    response = client.raw.post(f"/api/workspaces/{other}/search", json={
        "query": "备份保留策略", "translation": "retention policy",
        "document_id": task["document_id"],
    })
    assert response.status_code == 404
    assert client.scope.snapshot(None, None) == before
