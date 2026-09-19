"""Executable, deterministic RAG quality dataset and component contracts.

This suite owns the corpus, rewrite and Claim-to-Evidence oracle that sits
outside the generic Agent contract.  The unified benchmark runner reuses the
production evaluation metrics for ranking and abstention.  Component tests use
in-memory fakes and never call a network service or a real model.
"""

from __future__ import annotations

import inspect
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from config.config import APIConfig
from internal.application.local_repos import LocalRagChunkRepo
from internal.application.store import ApplicationStore
from internal.evaluation.schemas import Expected
from internal.rag.hybrid import HybridResult, HybridStore
from internal.rag.lab import run_rag_lab
from internal.rag.rag import Engine
from internal.rag.reranker import LLMReranker
from internal.rag.rewriter import HistoryMessage, LLMRewriter
from internal.rag.splitter import RecursiveSplitter
from internal.repo.ragchunk import Store as ProductionRagChunkStore


DATASET_PATH = Path(__file__).parent / "fixtures" / "rag_quality_eval_v1.jsonl"
REQUIRED_CATEGORIES = {
    "retrieval_hit",
    "graded_relevance",
    "no_answer",
    "query_rewrite",
    "rerank",
    "duplicate_chunks",
    "evidence_attribution",
    "tenant_isolation",
    "degradation",
}


def _load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _case(category: str) -> dict:
    return next(item for item in _load_cases() if item["category"] == category)


def _relevant(grades: dict[str, int]) -> set[str]:
    return {chunk_id for chunk_id, grade in grades.items() if int(grade) > 0}


def recall_at_k(ranking: list[str], grades: dict[str, int], k: int) -> float | None:
    relevant = _relevant(grades)
    if not relevant:
        return None
    return len(relevant & set(ranking[:k])) / len(relevant)


def reciprocal_rank_at_k(ranking: list[str], grades: dict[str, int], k: int) -> float | None:
    relevant = _relevant(grades)
    if not relevant:
        return None
    for rank, chunk_id in enumerate(ranking[:k], start=1):
        if chunk_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranking: list[str], grades: dict[str, int], k: int) -> float | None:
    if not _relevant(grades):
        return None

    def dcg(values: list[int]) -> float:
        return sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(values, start=1))

    actual = [int(grades.get(chunk_id, 0)) for chunk_id in ranking[:k]]
    ideal = sorted((int(value) for value in grades.values()), reverse=True)[:k]
    ideal_score = dcg(ideal)
    return dcg(actual) / ideal_score if ideal_score else 0.0


def claim_attribution(case: dict, run_name: str) -> tuple[float, float]:
    """Return claim coverage and evidence precision for deterministic labels."""

    expected = {claim["claim_id"]: set(claim["evidence_ids"]) for claim in case["oracle"]["claims"]}
    actual = {claim["claim_id"]: set(claim["evidence_ids"]) for claim in case["runs"][run_name]["answer_claims"]}
    if not expected:
        return (1.0, 1.0) if not actual else (1.0, 0.0)
    coverage = len(expected.keys() & actual.keys()) / len(expected)
    cited = sum(len(values) for values in actual.values())
    supported = sum(len(values & expected.get(claim_id, set())) for claim_id, values in actual.items())
    precision = supported / cited if cited else 0.0
    return coverage, precision


def test_dataset_has_complete_referentially_valid_rag_oracles():
    cases = _load_cases()
    assert {item["category"] for item in cases} == REQUIRED_CATEGORIES
    assert len({item["case_id"] for item in cases}) == len(cases)

    for item in cases:
        assert item["schema_version"] == "rag-eval-v1"
        corpus_ids = {chunk["chunk_id"] for chunk in item["corpus"]}
        assert len(corpus_ids) == len(item["corpus"])
        oracle = item["oracle"]
        grades = oracle["relevance_grades"]
        assert set(grades) <= corpus_ids
        assert set(oracle["relevant_chunk_ids"]) == _relevant(grades)
        assert all(isinstance(value, int) and 0 <= value <= 3 for value in grades.values())
        assert oracle["answerable"] is bool(oracle["relevant_chunk_ids"])
        for claim in oracle["claims"]:
            assert set(claim["evidence_ids"]) <= corpus_ids
        for run in item["runs"].values():
            assert len(run["ranking"]) == len(set(run["ranking"]))
            assert set(run["ranking"]) <= corpus_ids


def test_candidate_ranking_passes_offline_retrieval_gates_and_improves_baseline():
    answerable = [item for item in _load_cases() if item["oracle"]["answerable"]]
    candidate_recall = []
    candidate_mrr = []
    candidate_ndcg = []
    baseline_ndcg = []
    for item in answerable:
        grades = item["oracle"]["relevance_grades"]
        candidate = item["runs"]["candidate"]["ranking"]
        baseline = item["runs"]["baseline"]["ranking"]
        candidate_recall.append(recall_at_k(candidate, grades, 3))
        candidate_mrr.append(reciprocal_rank_at_k(candidate, grades, 3))
        candidate_ndcg.append(ndcg_at_k(candidate, grades, 3))
        baseline_ndcg.append(ndcg_at_k(baseline, grades, 3))

    assert sum(candidate_recall) / len(candidate_recall) == pytest.approx(1.0)
    assert sum(candidate_mrr) / len(candidate_mrr) == pytest.approx(1.0)
    assert sum(candidate_ndcg) / len(candidate_ndcg) >= 0.99
    assert sum(candidate_ndcg) / len(candidate_ndcg) > sum(baseline_ndcg) / len(baseline_ndcg)


def test_no_answer_is_scored_as_abstention_not_as_empty_retrieval_recall():
    item = _case("no_answer")
    grades = item["oracle"]["relevance_grades"]

    assert recall_at_k(item["runs"]["candidate"]["ranking"], grades, 3) is None
    assert reciprocal_rank_at_k(item["runs"]["candidate"]["ranking"], grades, 3) is None
    assert ndcg_at_k(item["runs"]["candidate"]["ranking"], grades, 3) is None
    assert item["runs"]["candidate"]["abstained"] is True
    assert item["runs"]["baseline"]["abstained"] is False


def test_claim_level_attribution_catches_wrong_evidence_even_when_recall_is_high():
    item = _case("evidence_attribution")

    baseline_coverage, baseline_precision = claim_attribution(item, "baseline")
    candidate_coverage, candidate_precision = claim_attribution(item, "candidate")

    assert baseline_coverage == 1.0
    assert baseline_precision == 0.5
    assert candidate_coverage == 1.0
    assert candidate_precision == 1.0


def test_generic_agent_eval_schema_cannot_natively_express_ranked_relevance():
    """Documents the current schema gap without changing shared contracts."""

    with pytest.raises(ValidationError):
        Expected(
            evidence_ids=["chunk-1"],
            relevance_grades={"chunk-1": 3},
            expected_ranking=["chunk-1"],
        )


def test_history_aware_query_rewrite_is_self_contained_and_fixture_driven():
    item = _case("query_rewrite")
    rewrites = item["oracle"]["acceptable_rewrites"]
    history = [HistoryMessage(**turn) for turn in item["history"]]
    rewriter = LLMRewriter(
        lambda _system, _user: json.dumps({"queries": rewrites}, ensure_ascii=False),
        num_queries=3,
    )

    actual = rewriter.rewrite(item["query"], history)

    assert actual == rewrites
    assert "星槎计划" in actual[0]
    assert "它" not in actual[0]


def test_reranker_moves_direct_answer_to_rank_one_and_improves_mrr():
    item = _case("rerank")
    by_id = {chunk["chunk_id"]: chunk for chunk in item["corpus"]}
    baseline_ids = item["runs"]["baseline"]["ranking"]
    candidates = [
        HybridResult(pg_id=index + 1, content=by_id[chunk_id]["text"], score=1 / (index + 1), source="hybrid")
        for index, chunk_id in enumerate(baseline_ids)
    ]
    grades = item["oracle"]["relevance_grades"]
    scores = [10 if chunk_id == "sla-005" else 2 if chunk_id == "noise-005" else 0 for chunk_id in baseline_ids]
    reranker = LLMReranker(
        lambda _system, _user: json.dumps(
            {"scores": [{"idx": index, "score": score} for index, score in enumerate(scores)]}
        )
    )

    results = reranker.rerank(item["query"], candidates, top_k=3)
    actual_ids = [baseline_ids[candidates.index(result)] for result in results]

    assert actual_ids == item["runs"]["candidate"]["ranking"]
    assert reciprocal_rank_at_k(actual_ids, grades, 3) > reciprocal_rank_at_k(baseline_ids, grades, 3)
    assert results[0].source.endswith("+rerank")


def test_engine_deduplicates_near_identical_parent_evidence():
    item = _case("duplicate_chunks")
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(rag_parent_dedup_threshold=0.85)
    candidates = [
        {"pg_id": index + 1, "chunk_id": chunk["chunk_id"], "content": chunk["text"], "score": 1.0 - index / 10}
        for index, chunk in enumerate(item["corpus"])
    ]

    deduped = engine._dedupe_results_by_content(candidates)

    assert [result["chunk_id"] for result in deduped] == item["runs"]["candidate"]["ranking"]


class _NoEmbeddingLLM:
    @staticmethod
    def embed(_text):
        return []


class _Events:
    @staticmethod
    def publish(*_args):
        return None


def test_local_rag_storage_and_retrieval_are_cross_user_isolated(tmp_path: Path):
    item = _case("tenant_isolation")
    store = ApplicationStore(database_url=f"sqlite+pysqlite:///{tmp_path / 'rag-tenants.db'}")
    try:
        alpha = store.create_user("rag-alpha", "not-a-real-password-hash")
        beta = store.create_user("rag-beta", "not-a-real-password-hash")
        repo = LocalRagChunkRepo(store)
        infra = SimpleNamespace(
            repo=SimpleNamespace(ragchunk=repo, events=_Events()),
            ready=SimpleNamespace(postgresql="disconnected", milvus="disconnected", elasticsearch="disconnected"),
        )
        cfg = APIConfig()
        alpha_engine = Engine(cfg, infra, _NoEmbeddingLLM(), user_id=alpha["user_id"])
        beta_engine = Engine(cfg, infra, _NoEmbeddingLLM(), user_id=beta["user_id"])
        corpus = {chunk["tenant_id"]: chunk for chunk in item["corpus"]}
        alpha_engine.ingest(corpus["tenant_alpha"]["text"], document_id="alpha-private")
        beta_engine.ingest(corpus["tenant_beta"]["text"], document_id="beta-private")

        alpha_answer, alpha_hits = alpha_engine.query("项目暗号 青鸟")
        beta_answer, beta_hits = beta_engine.query("项目暗号 白鲸")

        assert alpha_hits and "青鸟七号" in alpha_answer and "白鲸九号" not in alpha_answer
        assert beta_hits and "白鲸九号" in beta_answer and "青鸟七号" not in beta_answer
        assert repo.count(user_id=alpha["user_id"]) > 0
        assert repo.count(user_id=beta["user_id"]) > 0
    finally:
        store.close()


class _DegradationRepo:
    local_available = False

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def search_milvus_dicts(self, _embedding, _top_k, user_id="default_user"):
        self.calls.append(("semantic", user_id))
        raise RuntimeError("synthetic semantic outage")

    def search_es_dicts(self, _query, _top_k, user_id="default_user"):
        self.calls.append(("keyword", user_id))
        return [{"pg_id": 9, "score": 9.0}]

    def load_by_ids_with_parent(self, _ids, user_id="default_user"):
        self.calls.append(("load", user_id))
        return [{"id": 9, "content": "严重故障告警必须在30秒内送达值班人员。", "parent_content": ""}]


def _hybrid_cfg():
    return SimpleNamespace(
        rag_retrieval_failure_threshold=3,
        rag_retrieval_cooldown_seconds=30.0,
        rag_retrieval_half_open_max_calls=1,
        rag_milvus_dim=3,
        semantic_weight=0.5,
        kg_weight=0.0,
        rrf_constant_k=60,
    )


def test_semantic_outage_degrades_to_keyword_with_tenant_context_and_trace():
    item = _case("degradation")
    repo = _DegradationRepo()
    infra = SimpleNamespace(
        repo=SimpleNamespace(ragchunk=repo),
        ready=SimpleNamespace(milvus="connected", elasticsearch="connected"),
    )
    store = HybridStore(_hybrid_cfg(), infra, embed_fn=lambda _query: [1.0, 0.0, 0.0], user_id=item["tenant_id"])
    trace: dict = {}

    results = store.search(item["query"], top_k=3, trace=trace)

    assert results and results[0].source == item["failure_injection"]["expected_remaining_path"]
    assert "30秒" in results[0].content
    assert trace["retrieval_paths"]["semantic"]["reason"] == "backend_error"
    assert trace["retrieval_paths"]["keyword"]["ok"] is True


def test_engine_no_candidate_trace_has_explicit_no_answer_decision():
    item = _case("no_answer")
    engine = Engine.__new__(Engine)
    engine.loaded = True
    engine.cfg = SimpleNamespace(top_k=3, rag_no_answer_threshold=0.3)
    engine.inf = SimpleNamespace(
        ready=SimpleNamespace(postgresql="connected"),
        repo=SimpleNamespace(ragchunk=SimpleNamespace(local_available=False)),
    )
    engine._rewriter = None
    engine._hybrid = SimpleNamespace(search_multi=lambda *_args: [])
    engine._generate_fn = None

    answer, results, trace = engine.query_with_history_trace(item["query"], [])

    assert results == []
    assert "未找到相关内容" in answer
    assert trace["decision"] == "no_answer"
    assert trace["reason"] == "no_retrieval_candidates"
    assert trace["selected_evidence"] == []


class _KeywordEmbeddingClient:
    @staticmethod
    def embed_batch(texts):
        return [[1.0, 0.0] if ("预算" in text or "320" in text) else [0.0, 1.0] for text in texts]


class _BrokenEmbeddingClient:
    @staticmethod
    def embed_batch(_texts):
        raise RuntimeError("synthetic embedding outage")


def _lab_agent(llm):
    rag = SimpleNamespace(
        parent_splitter=RecursiveSplitter(160, 10),
        child_splitter=RecursiveSplitter(56, 8),
        _llm=llm,
        _generate_fn=lambda _system, user: "一期预算为320万元。" if "320" in user else "上下文不足。",
    )
    return SimpleNamespace(rag=rag)


def test_rag_lab_exposes_retrieval_hit_and_marks_embedding_fallback():
    item = _case("retrieval_hit")
    document = "\n\n".join(chunk["text"] for chunk in item["corpus"])

    healthy = run_rag_lab(_lab_agent(_KeywordEmbeddingClient()), document, item["query"], top_k=3)
    degraded = run_rag_lab(_lab_agent(_BrokenEmbeddingClient()), document, item["query"], top_k=3)

    assert [stage["id"] for stage in healthy["stages"]] == ["split", "query", "retrieve", "augment", "generate"]
    assert "320" in healthy["retrieval"]["results"][0]["content"]
    assert healthy["query"]["embedding_mode"] == "remote_embedding"
    assert degraded["query"]["embedding_mode"] == "local_hash_fallback"
    assert any("回落到本地哈希向量" in warning for warning in degraded["warnings"])


def test_production_rag_store_contract_is_tenant_scoped():
    """Production and local stores expose the same tenant-scoped contract."""

    for method_name in ("count", "save_pg_with_parent", "load_by_ids_with_parent", "search_es_dicts", "search_milvus_dicts"):
        parameters = inspect.signature(getattr(ProductionRagChunkStore, method_name)).parameters
        assert "user_id" in parameters, f"{method_name} must require tenant context"


def test_production_rag_store_applies_tenant_filters_to_all_read_models():
    class _PG:
        conn = object()

        def __init__(self):
            self.calls = []

        @staticmethod
        def is_real():
            return True

        def query(self, sql, params):
            self.calls.append((sql, params))
            return []

    class _ES:
        def __init__(self):
            self.body = None

        @staticmethod
        def is_real():
            return True

        def search(self, _index, body):
            self.body = body
            return {"hits": {"hits": []}}

    class _Milvus:
        def __init__(self):
            self.filter_expr = None

        @staticmethod
        def is_real():
            return True

        def search(self, _collection, _embedding, _top_k, *, output_fields, filter_expr):
            self.filter_expr = filter_expr
            return []

    pg, es, milvus = _PG(), _ES(), _Milvus()
    repo = ProductionRagChunkStore(pg, milvus, es)

    repo.load_by_ids_with_parent([1, 2], user_id="tenant_alpha")
    repo.search_es_dicts("预算", 3, user_id="tenant_alpha")
    repo.search_milvus_dicts([0.1, 0.2], 3, user_id="tenant_alpha")

    assert "user_id = %s" in pg.calls[0][0]
    assert pg.calls[0][1][-1] == "tenant_alpha"
    assert es.body["query"]["bool"]["filter"] == [{"term": {"user_id": "tenant_alpha"}}]
    assert milvus.filter_expr == 'user_id == "tenant_alpha"'


def test_hybrid_backend_fetch_propagates_tenant_context():
    item = _case("degradation")
    repo = _DegradationRepo()
    infra = SimpleNamespace(
        repo=SimpleNamespace(ragchunk=repo),
        ready=SimpleNamespace(milvus="connected", elasticsearch="connected"),
    )
    store = HybridStore(
        _hybrid_cfg(),
        infra,
        embed_fn=lambda _query: [1.0, 0.0, 0.0],
        user_id=item["tenant_id"],
    )

    store.search(item["query"], top_k=3)

    assert repo.calls
    assert all(user_id == item["tenant_id"] for _, user_id in repo.calls)
