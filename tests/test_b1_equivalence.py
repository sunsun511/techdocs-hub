"""Ranking/output contract against frozen pre-B1 code, including vector exclusions."""
import numpy as np
import pytest

from app.schemas import SearchRequest
from app.search import SearchEngine
from app.storage import DomainError
from tools.b1_reference import candidate_module, reference_module


class Models:
    dense_ready = True
    model_tag = "same-model"

    def embed(self, texts, prefix):
        return np.array([[1, 0, 0]], dtype=np.float32)


class Store:
    def __init__(self, case):
        self.chunks = []
        for i in range(6):
            vector = np.array([1, 0, 0], dtype=np.float32).tobytes()
            tag = "same-model"
            if case == "mixed_noncontiguous":
                if i == 0:
                    vector = None
                elif i == 2:
                    tag = "old-model"
                elif i == 4:
                    vector = np.array([1, 0], dtype=np.float32).tobytes()
            elif case == "all_missing":
                vector = None
            self.chunks.append({"id": str(i), "document_id": "doc", "key": "policy", "version_id": "v1",
                                "version_label": "v1", "title": "Policy", "heading": "Policy",
                                "text": "Retention policy is seven days.", "source_url": "https://example.org",
                                "vector": vector, "model_tag": tag})

    def generation(self):
        return 7

    def snapshot(self, document_id, version_id):
        return 7, self.chunks


def outcome(engine, request):
    try:
        result = engine.search(request)
        return {k: v for k, v in result.items() if k != "elapsed_ms"}
    except DomainError as exc:
        return {"status": exc.status, "message": exc.message}


@pytest.mark.parametrize("case", ["all_valid_tied_scores", "mixed_noncontiguous", "all_missing"])
@pytest.mark.parametrize("method", ["auto", "bm25", "dense", "hybrid"])
def test_pre_b1_ranking_scores_warnings_and_errors_are_identical(case, method):
    store, models = Store(case), Models()
    request = SearchRequest(query="retention policy", language="en", method=method, top_k=20)
    old = reference_module().SearchEngine(store, models)
    new = SearchEngine(store, models)
    assert outcome(new, request) == outcome(old, request)
    candidate = candidate_module().SearchEngine(store, models)
    assert outcome(candidate, request) == outcome(old, request)
