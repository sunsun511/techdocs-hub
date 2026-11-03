from collections import Counter
import math
import re
import threading
import time
import numpy as np
from .storage import DomainError


def tokens(text):
    return re.findall(r"[a-z0-9_]+", text.casefold())


class LexicalIndex:
    def __init__(self, chunks):
        self.chunks = chunks
        self.bags = [Counter(tokens(c["heading"] + "\n" + c["text"])) for c in chunks]
        self.lengths = np.array([sum(b.values()) for b in self.bags])
        self.average = float(self.lengths.mean()) if len(chunks) else 1
        self.df = Counter(t for bag in self.bags for t in bag)

    def score(self, query):
        result = np.zeros(len(self.chunks))
        for term in sorted(set(tokens(query))):
            freq = np.array([b.get(term, 0) for b in self.bags])
            idf = math.log(1 + (len(self.chunks) - self.df[term] + .5) / (self.df[term] + .5))
            result += idf * freq * 2.2 / (freq + 1.2 * (.25 + .75 * self.lengths / max(self.average, 1)))
        return result


def ranked(scores, positive=False):
    return [int(i) for i in np.argsort(-scores, kind="stable") if not positive or scores[i] > 0]


class SearchEngine:
    def __init__(self, store, models):
        self.store, self.models = store, models
        self.lock = threading.Lock()
        self.cache = {}

    def index(self, document_id, version_id):
        generation = self.store.generation()
        key = (generation, document_id, version_id)
        with self.lock:
            if key in self.cache:
                return generation, self.cache[key]
            actual, chunks = self.store.snapshot(document_id, version_id)
            index = LexicalIndex(chunks)
            self.cache = {k: v for k, v in self.cache.items() if k[0] == actual}
            # Keep memory bounded when clients use many document/version filters.
            if len(self.cache) >= 32:
                self.cache.clear()
            self.cache[(actual, document_id, version_id)] = index
            return actual, index

    def search(self, request):
        started = time.perf_counter()
        language = request.language
        if language == "auto":
            language = "zh" if re.search(r"[\u3400-\u9fff]", request.query) else "en"
        method = request.method
        warnings = []
        translated = None
        lexical_query = request.query
        # Chinese translation is required for the lexical/hybrid path, not dense.
        uses_translation = language == "zh" and method != "dense"
        if request.translation is not None and not uses_translation:
            raise DomainError(422, "只有用关键词检索的中文问题才能提供修改后的英文译文")
        if uses_translation and request.translation is not None:
            # Same language, same method, same dense query; only the lexical text comes from the user.
            translated = lexical_query = request.translation
            warnings.append("已使用你修改后的英文译文检索，匹配方式与原问题相同")
        elif uses_translation:
            try:
                translation = self.models.translate(request.query)
            except RuntimeError as exc:
                raise DomainError(503, str(exc)) from exc
            except ValueError as exc:
                raise DomainError(422, str(exc)) from exc
            translated = lexical_query = translation["text"]
            warnings.append("中文检索使用机器译文；技术标识可能译错，请结合原文核对")
            if not translation["complete"]:
                warnings.append("译文达到长度上限；建议缩短问题或使用英文关键词")
        if method == "auto":
            method = "bm25" if language == "zh" else "hybrid" if self.models.dense_ready else "bm25"
            if language == "en" and not self.models.dense_ready:
                warnings.append("向量模型未加载，当前使用 BM25")
        if method in ["dense", "hybrid"] and not self.models.dense_ready:
            raise DomainError(503, "向量模型未加载；请安装模型或选择 BM25")
        qvec = self.models.embed([request.query], "query: ") if method in ["dense", "hybrid"] else None
        for _ in range(3):
            generation, index = self.index(request.document_id, request.version_id)
            if not index.chunks:
                return {"query": request.query, "translated_query": translated, "language": language,
                        "method": method, "generation": generation, "results": [], "warnings": warnings + ["所选范围暂无已就绪文档"], "elapsed_ms": (time.perf_counter() - started) * 1000}
            bm = index.score(lexical_query)
            dense = np.full(len(index.chunks), -np.inf)
            valid = set()  # Membership only; result order comes from stable ranked().
            if qvec is not None:
                for i, chunk in enumerate(index.chunks):
                    if chunk["vector"] is not None and chunk["model_tag"] == self.models.model_tag:
                        vector = np.frombuffer(chunk["vector"], dtype=np.float32)
                        if vector.shape == qvec[0].shape:
                            dense[i] = float(vector @ qvec[0])
                            valid.add(i)
                if not valid:
                    if request.method == "auto":
                        method = "bm25"
                        warnings.append("当前索引没有兼容向量，使用 BM25；新版本导入后可建立向量")
                    else:
                        raise DomainError(409, "该范围没有兼容向量索引；请导入新版本重新索引或用 BM25")
                elif len(valid) != len(index.chunks):
                    warnings.append("部分旧索引没有兼容向量，仅参与词法检索")
            if method == "bm25":
                scores, order = bm, ranked(bm, True)
            elif method == "dense":
                scores, order = dense, [i for i in ranked(dense) if i in valid]
            else:
                scores = np.zeros(len(bm))
                for rank, idx in enumerate(ranked(bm, True), 1):
                    scores[idx] += 1 / (60 + rank)
                for rank, idx in enumerate([i for i in ranked(dense) if i in valid], 1):
                    scores[idx] += 1 / (60 + rank)
                order = ranked(scores, True)
            results = []
            qterms = set(tokens(lexical_query))
            for idx in order[:request.top_k]:
                chunk = index.chunks[idx]
                matched = sorted(qterms & set(tokens(chunk["heading"] + " " + chunk["text"])))
                coverage = len(matched) / max(len(qterms), 1)
                results.append({
                    "chunk_id": chunk["id"], "document_id": chunk["document_id"], "document_key": chunk["key"],
                    "version_id": chunk["version_id"], "version_label": chunk["version_label"],
                    "title": chunk["title"], "heading": chunk["heading"], "text": chunk["text"],
                    "source_url": chunk["source_url"], "score": float(scores[idx]),
                    "bm25_score": float(bm[idx]), "cosine": float(dense[idx]) if np.isfinite(dense[idx]) else None,
                    "matched_terms": matched, "match_hint": "low" if coverage < .35 else "candidate",
                })
            # A changed document/version/delete invalidates the entire snapshot.
            if generation == self.store.generation():
                if not results:
                    warnings.append("未找到词法匹配，试试更短的关键词或向量对照")
                return {"query": request.query, "translated_query": translated, "language": language,
                        "method": method, "generation": generation, "results": results, "warnings": list(dict.fromkeys(warnings)), "elapsed_ms": (time.perf_counter() - started) * 1000}
        raise DomainError(409, "文档正在连续变更，请稍后重新搜索")
