from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from internal.application.local_repos import LocalLongTermRepo, MemoryOutboxWorker
from internal.application.models import AgentLongTermMemoryRecord, MemoryOutboxRecord, MemoryProjectionRecord
from internal.application.store import ApplicationStore
from internal.memory.consistency import (
    EventType,
    MemoryRecord,
    ProjectionConflict,
    ProjectionState,
    compute_content_hash,
    milvus_projector,
)


class MemoryProjectionStore:
    def __init__(self):
        self.state: ProjectionState | None = None

    def get(self, memory_id: int):
        return self.state if self.state and self.state.memory_id == memory_id else None

    def upsert(self, record: MemoryRecord):
        self.state = ProjectionState(record.memory_id, record.version, record.content_hash)

    def delete(self, memory_id: int, version: int):
        self.state = None


def test_content_hash_is_stable_across_set_order_and_changes_with_version():
    record = MemoryRecord(memory_id=7, user_id="u1", content="事实", tags=["b", "a"], supersedes=[9, 2])
    reordered = replace(record, tags=["a", "b"], supersedes=[2, 9])
    assert compute_content_hash(record) == compute_content_hash(reordered)
    assert compute_content_hash(record) != compute_content_hash(replace(record, version=2))
    assert record.tags == ["b", "a"]


def test_content_hash_matches_go_encoding_json_golden_value():
    record = MemoryRecord(
        memory_id=7,
        user_id="u1",
        content="事实<&",
        importance=0.5,
        embedding=[1, 0.25],
        category="general",
        tags=["b", "a"],
        version=2,
    )
    assert compute_content_hash(record) == "350562103f39512c1873e6bf63ac5a0054b2d3aeb891d415978eade8e6eb8d5a"


def test_versioned_projector_ignores_stale_is_idempotent_and_rejects_conflict():
    store = MemoryProjectionStore()
    projector = milvus_projector(store)
    v2 = MemoryRecord(memory_id=1, user_id="u1", content="v2", version=2)
    v2.content_hash = compute_content_hash(v2)
    projector.apply(EventType.UPSERT_MEMORY_VECTOR, v2)
    projector.apply(EventType.UPSERT_MEMORY_VECTOR, v2)
    stale = replace(v2, content="v1", version=1)
    stale.content_hash = compute_content_hash(stale)
    projector.apply(EventType.UPSERT_MEMORY_VECTOR, stale)
    assert store.state and store.state.version == 2
    with pytest.raises(ProjectionConflict):
        projector.apply(EventType.UPSERT_MEMORY_VECTOR, replace(v2, content_hash="different"))


def test_local_memory_mutation_and_targeted_outbox_commit_atomically(tmp_path):
    store = ApplicationStore(f"sqlite:///{tmp_path / 'memory.db'}")
    user = store.create_user("memory-user", "unused")
    repo = LocalLongTermRepo(store)
    memory_id = repo.save("事实", 0.8, json.dumps([0.1, 0.2]), user_id=user["id"])
    with store.transaction() as session:
        memory = session.get(AgentLongTermMemoryRecord, memory_id)
        events = session.scalars(select(MemoryOutboxRecord).order_by(MemoryOutboxRecord.id)).all()
        assert memory.version == 1 and len(memory.content_hash) == 64
        assert {(event.event_type, event.target) for event in events} == {
            ("upsert_memory_vector", "milvus"),
            ("upsert_memory_graph_node", "neo4j"),
            ("upsert_memory_graph_edges", "neo4j"),
        }
        assert all(event.aggregate_version == 1 and event.payload["content_hash"] == memory.content_hash for event in events)

    worker = MemoryOutboxWorker(store)
    assert worker.process_once() == 3
    with store.transaction() as session:
        states = session.scalars(select(MemoryProjectionRecord)).all()
        assert {(state.target, state.version, state.deleted) for state in states} == {
            ("milvus", 1, False),
            ("neo4j", 1, False),
        }

    repo.delete([memory_id], user_id=user["id"])
    assert worker.process_once() == 3
    with store.transaction() as session:
        memory = session.get(AgentLongTermMemoryRecord, memory_id)
        states = session.scalars(select(MemoryProjectionRecord)).all()
        assert memory.deleted_at is not None and memory.version == 2
        assert all(state.version == 2 and state.deleted for state in states)
    store.close()
