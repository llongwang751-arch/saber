import json
from types import SimpleNamespace

from internal.application.local_repos import LocalRagChunkRepo, RagProjectionOutboxWorker
from internal.application.store import ApplicationStore


class ProjectionRepo:
    def __init__(self):
        self.es = []
        self.milvus = []
        self.es_deleted = []
        self.milvus_deleted = []

    def index_es(self, pg_id, content, doc_hash, chunk_idx):
        self.es.append((pg_id, content, doc_hash, chunk_idx))

    def insert_milvus(self, ids, contents, embeddings):
        self.milvus.append((ids, contents, embeddings))

    def delete_es(self, ids):
        self.es_deleted.extend(ids)

    def delete_milvus(self, ids):
        self.milvus_deleted.extend(ids)


def test_rag_chunk_and_projection_jobs_commit_together_and_replay_idempotently(tmp_path):
    store = ApplicationStore(f"sqlite:///{tmp_path / 'rag-outbox.db'}")
    try:
        user = store.create_user("rag-outbox-user", "hash")
        projection = ProjectionRepo()
        repo = LocalRagChunkRepo(store, projection_repo=projection)
        worker = RagProjectionOutboxWorker(
            store,
            repo,
            SimpleNamespace(elasticsearch="connected", milvus="connected"),
        )

        first_id = repo.save_pg_with_parent(
            "doc-hash", 0, "预算八百万元", "完整父块", json.dumps([0.1, 0.2]), user_id=user["id"]
        )
        second_id = repo.save_pg_with_parent(
            "doc-hash", 0, "预算八百万元", "完整父块", json.dumps([0.1, 0.2]), user_id=user["id"]
        )

        assert first_id == second_id
        assert worker.status(user["id"])["pending"] == 2
        assert worker.process_once() == 2
        assert worker.status(user["id"]) == {"pending": 0, "retrying": 0, "dead": 0, "processed": 2}
        assert len(projection.es) == 1
        assert len(projection.milvus) == 1
        assert worker.process_once() == 0

        assert repo.delete_by_doc_hash("doc-hash", user_id=user["id"]) == [first_id]
        assert worker.status(user["id"])["pending"] == 2
        assert worker.process_once(user_id=user["id"]) == 2
        assert projection.es_deleted == [first_id]
        assert projection.milvus_deleted == [first_id]
    finally:
        store.close()


def test_rag_projection_job_waits_without_consuming_attempts_until_dependency_is_ready(tmp_path):
    store = ApplicationStore(f"sqlite:///{tmp_path / 'rag-outbox-wait.db'}")
    try:
        user = store.create_user("rag-outbox-wait", "hash")
        readiness = SimpleNamespace(elasticsearch="disconnected", milvus="disconnected")
        projection = ProjectionRepo()
        repo = LocalRagChunkRepo(store, projection_repo=projection)
        worker = RagProjectionOutboxWorker(store, repo, readiness)
        repo.save_pg_with_parent("doc", 0, "内容", "父块", "[]", user_id=user["id"])

        assert worker.process_once() == 0
        assert worker.status(user["id"])["retrying"] == 0
        readiness.elasticsearch = readiness.milvus = "connected"
        assert worker.process_once() == 2
    finally:
        store.close()
