from types import SimpleNamespace

from config.config import APIConfig
from internal.rag.hybrid import HybridStore


class _Repo:
    local_available = False

    def __init__(self):
        self.es = []
        self.milvus = []

    def load_all_for_reindex(self):
        return [
            {"pg_id": 1, "content": "one", "doc_hash": "d", "chunk_idx": 0, "embedding": [0.1, 0.2]},
            {"pg_id": 2, "content": "two", "doc_hash": "d", "chunk_idx": 1, "embedding": [0.3, 0.4]},
        ]

    def index_es(self, pg_id, content, doc_hash, chunk_idx):
        self.es.append((pg_id, content, doc_hash, chunk_idx))
        return None

    def insert_milvus(self, pg_ids, contents, embeddings):
        self.milvus.append((pg_ids, contents, embeddings))
        return None


def test_rebuild_indexes_repairs_both_projections_from_truth_store():
    cfg = APIConfig()
    cfg.rag_milvus_dim = 2
    cfg.retry_delay_ms = 0
    repo = _Repo()
    inf = SimpleNamespace(
        ready=SimpleNamespace(milvus="connected", elasticsearch="connected"),
        repo=SimpleNamespace(ragchunk=repo, events=SimpleNamespace(publish=lambda *_args: None)),
    )
    store = HybridStore(cfg, inf)

    report = store.rebuild_indexes()

    assert report == {
        "source_rows": 2,
        "elasticsearch_indexed": 2,
        "milvus_indexed": 2,
        "failures": [],
        "ok": True,
    }
    assert [item[0] for item in repo.es] == [1, 2]
    assert repo.milvus[0][0] == [1, 2]
