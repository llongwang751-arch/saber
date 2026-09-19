from internal.rag.local_reranker import LocalCrossEncoderReranker, LocalOverlapReranker
from internal.rag.reranker import LLMReranker


def test_english_word_boundaries_and_markdown_title_punctuation():
    from internal.rag.local_reranker import _features, _overlap_score
    features = _features("# tx-harness: Agent\nPython 3.10+ scripts/evaluate.py")
    assert {"tx-harness", "agent", "python", "3.10", "scripts/evaluate.py"} <= features
    assert "python3.10" not in features
    for query in ("tx-harness 是做什么的？", "Python 版本要求是什么？"):
        assert _overlap_score(query, _features(query), "# tx-harness: Agent\nPython 3.10+") > 0


class _Result:
    def __init__(self, content, score):
        self.content = content
        self.score = score
        self.source = "hybrid"


def test_local_overlap_reranker_prioritises_query_evidence():
    results = [
        _Result("今天阳光很好", 0.9),
        _Result("星槎47项目第二年预算为800万元", 0.1),
    ]

    reranked = LocalOverlapReranker().rerank("星槎47第二年预算", results, 2)

    assert reranked[0].content == "星槎47项目第二年预算为800万元"
    assert all(result.source.endswith("+local_rerank") for result in reranked)


def test_llm_reranker_uses_local_fallback_on_remote_failure():
    def fail(_system, _user):
        raise TimeoutError("remote unavailable")

    reranker = LLMReranker(fail, fallback_reranker=LocalOverlapReranker())
    results = [
        _Result("无关内容", 0.9),
        _Result("项目预算为800万元", 0.1),
    ]

    reranked = reranker.rerank("项目预算", results, 2)

    assert reranked[0].content == "项目预算为800万元"
    assert "+local_rerank" in reranked[0].source
    assert "+rerank" not in reranked[0].source


def test_cross_encoder_is_lazy_and_reranks_with_injected_model():
    loaded = []

    class Model:
        def predict(self, pairs):
            assert len(pairs) == 2
            return [-1.0, 3.0]

    reranker = LocalCrossEncoderReranker(
        "local/model",
        model_loader=lambda name: loaded.append(name) or Model(),
    )
    results = [_Result("无关", 0.9), _Result("预算八百万元", 0.1)]

    output = reranker.rerank("预算", results, 2)

    assert loaded == ["local/model"]
    assert output[0].content == "预算八百万元"
    assert output[0].source.endswith("+cross_encoder")
    assert 0.0 <= output[0].score <= 1.0


def test_cross_encoder_missing_dependency_falls_back_to_overlap():
    reranker = LocalCrossEncoderReranker(
        "missing/model",
        model_loader=lambda _name: (_ for _ in ()).throw(RuntimeError("missing")),
        fallback_reranker=LocalOverlapReranker(),
    )
    results = [_Result("无关", 0.9), _Result("预算八百万元", 0.1)]

    output = reranker.rerank("预算", results, 2)

    assert output[0].content == "预算八百万元"
    assert output[0].source.endswith("+local_rerank")
