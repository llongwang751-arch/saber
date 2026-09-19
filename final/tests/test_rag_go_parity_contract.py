"""Frozen Go 845e8f7 retrieval contracts exercised against Python.

These cases intentionally cover the non-obvious behavior in
``internal/domain/rag/hybrid.go`` instead of asserting Python-only choices.
"""

from math import isclose
from types import SimpleNamespace

from internal.rag.hybrid import HybridResult, HybridStore


class _Repo:
    local_available = False

    def __init__(self, *, fail_milvus=False, fail_es=False):
        self.fail_milvus = fail_milvus
        self.fail_es = fail_es
        self.rows = {
            1: {"id": 1, "content": "one", "parent_content": ""},
            2: {"id": 2, "content": "two", "parent_content": ""},
            3: {"id": 3, "content": "three", "parent_content": ""},
        }

    def search_milvus_dicts(self, _embedding, _top_k):
        if self.fail_milvus:
            raise RuntimeError("milvus unavailable")
        return [{"pg_id": 1}, {"pg_id": 2}]

    def search_es_dicts(self, _query, _top_k):
        if self.fail_es:
            raise RuntimeError("elasticsearch unavailable")
        return [{"pg_id": 2}, {"pg_id": 3}]

    def load_by_ids_with_parent(self, ids):
        return [self.rows[item_id] for item_id in ids if item_id in self.rows]


class _KG:
    def __init__(self):
        self.calls = 0

    @staticmethod
    def available():
        return True

    def search(self, _query, _top_k):
        self.calls += 1
        return [SimpleNamespace(pg_id=3, score=99.0)]


def _store(repo, kg=None):
    cfg = SimpleNamespace(
        rag_retrieval_failure_threshold=3,
        rag_retrieval_cooldown_seconds=30.0,
        rag_retrieval_half_open_max_calls=1,
        rag_milvus_dim=3,
        rrf_constant_k=60,
        # Deliberately extreme: Go exposes this field but does not consume it.
        semantic_weight=0.01,
        kg_weight=0.3,
    )
    infra = SimpleNamespace(
        ready=SimpleNamespace(milvus="connected", elasticsearch="connected"),
        repo=SimpleNamespace(ragchunk=repo),
    )
    return HybridStore(cfg, infra, embed_fn=lambda _query: [0.1, 0.2, 0.3], kg=kg)


def test_go_rrf_uses_unit_milvus_and_es_weights_and_configured_kg_weight():
    store = _store(_Repo(), _KG())

    results = store.search("query", top_k=3)

    assert [item.pg_id for item in results] == [2, 3, 1]
    scores = {item.pg_id: item.score for item in results}
    assert isclose(scores[1], 1.0 / 61.0)
    assert isclose(scores[2], 1.0 / 62.0 + 1.0 / 61.0)
    assert isclose(scores[3], 1.0 / 62.0 + 0.3 / 61.0)


def test_go_hybrid_does_not_use_kg_as_sole_fallback_when_both_primary_indexes_fail():
    kg = _KG()
    store = _store(_Repo(fail_milvus=True, fail_es=True), kg)

    assert store.search("query", top_k=3) == []
    assert kg.calls == 0


def test_go_multi_query_rrf_deduplicates_by_complete_chunk_content():
    store = _store(_Repo())

    def search(query, _top_k, trace=None):
        del trace
        return [
            HybridResult(
                pg_id=1 if query == "first" else 999,
                content="same complete chunk content",
                score=0.9,
                source="hybrid",
            )
        ]

    store.search = search
    results = store.search_multi(["first", "second"], top_k=10)

    assert len(results) == 1
    assert results[0].content == "same complete chunk content"
    assert isclose(results[0].score, 2.0 / 61.0)

