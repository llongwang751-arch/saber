import math
from types import SimpleNamespace

from internal.rag.lab import run_rag_lab
from internal.rag.splitter import RecursiveSplitter


class _EmbeddingClient:
    def embed_batch(self, texts):
        vectors = []
        for text in texts:
            if "预算" in text or "320" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "启动" in text or "2026" in text:
                vectors.append([0.7, 0.7, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
        return vectors


class _BrokenEmbeddingClient:
    def embed_batch(self, _texts):
        raise RuntimeError("offline")


def _agent(llm):
    rag = SimpleNamespace(
        parent_splitter=RecursiveSplitter(120, 10),
        child_splitter=RecursiveSplitter(48, 8),
        _llm=llm,
        _generate_fn=lambda _system, user: "一期预算为 320 万元。" if "320" in user else "未找到答案。",
    )
    return SimpleNamespace(rag=rag)


def test_rag_lab_exposes_all_five_real_pipeline_stages():
    result = run_rag_lab(
        _agent(_EmbeddingClient()),
        "星槎-47 一期预算为 320 万元，计划在 2026 年 9 月启动。\n\n二期预算暂未确定。",
        "一期预算是多少？",
        top_k=3,
    )

    assert [stage["id"] for stage in result["stages"]] == [
        "split", "query", "retrieve", "augment", "generate"
    ]
    assert result["query"]["embedding_mode"] == "remote_embedding"
    assert result["document"]["child_count"] >= 1
    assert result["retrieval"]["results"][0]["score"] == 1.0
    assert "320" in result["prompt"]["context"]
    assert "一期预算" in result["prompt"]["user"]
    assert result["answer"] == "一期预算为 320 万元。"
    assert all(math.isfinite(item["score"]) for item in result["retrieval"]["results"])


def test_rag_lab_marks_local_vector_fallback_without_nan():
    result = run_rag_lab(
        _agent(_BrokenEmbeddingClient()),
        "设备在线率不低于 99.5%，异常告警到达时间小于 30 秒。",
        "异常告警多久到达？",
        top_k=5,
    )

    assert result["query"]["embedding_mode"] == "local_hash_fallback"
    assert result["warnings"]
    assert result["query"]["vector_dimension"] == 256
    assert all(math.isfinite(item["score"]) for item in result["retrieval"]["results"])
