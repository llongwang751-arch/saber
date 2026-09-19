from types import SimpleNamespace

import pytest

from config.config import APIConfig
from internal.llm.llm import Client
from internal.rag.hybrid import HybridStore


class _FailingRepo:
    local_available = False

    def __init__(self):
        self.calls = 0

    def search_milvus_dicts(self, _embedding, _top_k):
        self.calls += 1
        raise TimeoutError("milvus timeout")


def test_embedding_circuit_opens_and_skips_remote_calls(monkeypatch):
    cfg = APIConfig()
    cfg.embedding_api_key = "configured"
    cfg.embedding_api_url = "https://embedding.invalid/embeddings"
    cfg.embedding_model = "test"
    cfg.embedding_failure_threshold = 2
    client = Client(cfg)
    calls = []

    def fail(*_args, **_kwargs):
        calls.append("called")
        raise TimeoutError("embedding timeout")

    monkeypatch.setattr(client, "_call_embed", fail)
    with pytest.raises(TimeoutError):
        client.embed("one")
    with pytest.raises(TimeoutError):
        client.embed("two")
    with pytest.raises(RuntimeError, match="circuit open"):
        client.embed("three")

    assert len(calls) == 2
    assert client.embedding_circuit_snapshot()["state"] == "open"


def test_milvus_circuit_opens_and_trace_records_degradation():
    cfg = APIConfig()
    cfg.rag_milvus_dim = 3
    cfg.rag_retrieval_failure_threshold = 2
    repo = _FailingRepo()
    inf = SimpleNamespace(
        ready=SimpleNamespace(milvus="connected", elasticsearch="disconnected"),
        repo=SimpleNamespace(ragchunk=repo),
    )
    store = HybridStore(cfg, inf, embed_fn=lambda _query: [0.1, 0.2, 0.3])

    assert store.search("q1", 3) == []
    assert store.search("q2", 3) == []
    trace = {}
    assert store.search("q3", 3, trace) == []

    assert repo.calls == 2
    assert trace["returned_candidates"] == []
