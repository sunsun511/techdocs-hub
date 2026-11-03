"""A user-corrected translation must change only the lexical query text, never language or method."""
import numpy as np
import pytest
from app.schemas import SearchRequest
from app.search import SearchEngine
from app.storage import DomainError

CHUNKS = [
    ("c1", "Snapshotting", "By default Redis saves snapshots of the dataset on disk in a file called dump.rdb."),
    ("c2", "Eviction", "Reservation of memory and eviction policies are configured with maxmemory."),
]


class Store:
    def generation(self):
        return 1

    def snapshot(self, document_id, version_id):
        vec = np.array([1, 0, 0], dtype=np.float32).tobytes()
        return 1, [{"id": cid, "document_id": "d", "key": "redis", "version_id": "v", "version_label": "v1",
                    "title": "Redis", "heading": heading, "text": text, "source_url": "",
                    "vector": vec, "model_tag": "fake"} for cid, heading, text in CHUNKS]


class Models:
    dense_ready = True
    model_tag = "fake"

    def __init__(self):
        self.translated, self.embedded = [], []

    def translate(self, text):
        self.translated.append(text)
        return {"text": "Redis snapshot reservation", "complete": True}

    def embed(self, texts, prefix):
        self.embedded.extend(texts)
        return np.array([[1, 0, 0] for _ in texts], dtype=np.float32)


def run(**kw):
    models = Models()
    return SearchEngine(Store(), models).search(SearchRequest(**kw)), models


def test_auto_keeps_chinese_method_and_skips_machine_translation():
    machine, _ = run(query="Redis 默认快照文件叫什么", method="auto")
    fixed, models = run(query="Redis 默认快照文件叫什么", method="auto", translation="  Redis default snapshot dump.rdb ")
    assert machine["method"] == fixed["method"] == "bm25"
    assert fixed["language"] == "zh"
    assert models.translated == []
    assert fixed["translated_query"] == "Redis default snapshot dump.rdb"
    assert fixed["results"][0]["chunk_id"] == "c1"
    assert "dump" in fixed["results"][0]["matched_terms"]
    assert any("修改后的英文译文" in w for w in fixed["warnings"])


def test_hybrid_stays_hybrid_and_dense_side_still_uses_original_question():
    fixed, models = run(query="Redis 默认快照文件叫什么", method="hybrid", translation="Redis default snapshot")
    assert fixed["method"] == "hybrid"
    assert models.embedded == ["Redis 默认快照文件叫什么"]
    assert models.translated == []


@pytest.mark.parametrize("kw", [
    {"query": "What is dump.rdb", "language": "en"},
    {"query": "What is dump.rdb"},
    {"query": "Redis 默认快照文件叫什么", "method": "dense"},
])
def test_translation_rejected_where_no_translation_step_exists(kw):
    with pytest.raises(DomainError) as err:
        run(translation="Redis snapshot", **kw)
    assert err.value.status == 422


def test_blank_translation_means_use_machine_translation():
    result, models = run(query="Redis 默认快照文件叫什么", translation="   ")
    assert models.translated == ["Redis 默认快照文件叫什么"]
    assert result["translated_query"] == "Redis snapshot reservation"
