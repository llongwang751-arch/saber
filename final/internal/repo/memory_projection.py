"""Production long-term-memory outbox consumers and reconciliation.

PostgreSQL is the source of truth.  This module deliberately owns separate
database connections for its background threads; request repositories still
use the connection managed by :mod:`internal.infra.infra`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Iterator, Protocol

from internal.memory.consistency import (
    EventType,
    MemoryRecord,
    OutboxEvent,
    ProjectionConflict,
    ProjectionState,
    Target,
    compute_content_hash,
)

logger = logging.getLogger(__name__)

MEMORY_VECTOR_COLLECTION = "long_term_memory_vectors"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return float(value)


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("memory outbox payload must be a JSON object")


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        return list(decoded) if isinstance(decoded, list) else []
    return list(value) if isinstance(value, tuple) else []


class PostgresMemoryOutboxRepository:
    """Lease/retry/dead-letter repository using ``FOR UPDATE SKIP LOCKED``."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory
        self._local = threading.local()
        self._connections: set[Any] = set()
        self._connections_lock = threading.Lock()
        self._closed = False

    def _connection(self):
        if self._closed:
            raise RuntimeError("memory outbox repository is closed")
        conn = getattr(self._local, "connection", None)
        if conn is None or bool(getattr(conn, "closed", False)):
            conn = self._connection_factory()
            if conn is None:
                raise RuntimeError("memory outbox postgres unavailable")
            self._local.connection = conn
            with self._connections_lock:
                self._connections.add(conn)
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        conn = self._connection()
        prior_autocommit = bool(getattr(conn, "autocommit", False))
        try:
            conn.autocommit = False
            with conn.cursor() as cursor:
                yield cursor
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.autocommit = prior_autocommit
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except Exception:
                pass

    def claim(
        self,
        target: Target | str,
        worker_id: str,
        limit: int = 100,
        lease_seconds: float = 30.0,
    ) -> list[OutboxEvent]:
        target = Target(target)
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("memory outbox worker_id is required")
        limit = max(1, min(int(limit), 500))
        lease_seconds = max(1.0, float(lease_seconds))
        with self._transaction() as cursor:
            # A crashed worker's claims become eligible again after the lease.
            cursor.execute(
                "UPDATE memory_outbox SET status='pending',locked_at=NULL,locked_by=NULL "
                "WHERE status='processing' AND target=%s AND locked_at IS NOT NULL "
                "AND locked_at <= NOW() - (%s * INTERVAL '1 second')",
                (str(target), lease_seconds),
            )
            cursor.execute(
                "SELECT id,event_id,aggregate_id,user_id,aggregate_version,event_type,"
                "target,payload,attempts,created_at FROM memory_outbox "
                "WHERE status='pending' AND target=%s AND available_at<=NOW() "
                "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
                (str(target), limit),
            )
            rows = list(cursor.fetchall())
            ids = [int(row[0]) for row in rows]
            if ids:
                cursor.execute(
                    "UPDATE memory_outbox SET status='processing',locked_by=%s,locked_at=NOW() "
                    "WHERE id = ANY(%s) AND status='pending'",
                    (worker_id, ids),
                )
                if int(cursor.rowcount or 0) != len(ids):
                    raise RuntimeError("memory outbox claim lost rows")
        return [
            OutboxEvent(
                id=int(row[0]),
                event_id=str(row[1]),
                aggregate_id=int(row[2]),
                user_id=str(row[3]),
                aggregate_version=int(row[4]),
                event_type=EventType(row[5]),
                target=Target(row[6]),
                payload=_json_mapping(row[7]),
                attempts=int(row[8] or 0),
                created_at=row[9],
            )
            for row in rows
        ]

    def _finish(
        self,
        event_id: str,
        worker_id: str | None,
        sql: str,
        params: tuple[Any, ...],
    ) -> None:
        owner_clause = " AND locked_by=%s" if worker_id else ""
        values = (*params, worker_id) if worker_id else params
        with self._transaction() as cursor:
            cursor.execute(sql + owner_clause, values)
            if int(cursor.rowcount or 0) != 1:
                raise RuntimeError(f"memory outbox lease lost for event {event_id}")

    def mark_processed(self, event_id: str, worker_id: str | None = None) -> None:
        self._finish(
            event_id,
            worker_id,
            "UPDATE memory_outbox SET status='processed',processed_at=NOW(),"
            "locked_at=NULL,locked_by=NULL,last_error=NULL "
            "WHERE event_id=%s AND status='processing'",
            (event_id,),
        )

    def mark_retry(
        self,
        event_id: str,
        error: str,
        available_at: datetime,
        worker_id: str | None = None,
    ) -> None:
        self._finish(
            event_id,
            worker_id,
            "UPDATE memory_outbox SET status='pending',attempts=attempts+1,"
            "available_at=%s,last_error=%s,locked_at=NULL,locked_by=NULL "
            "WHERE event_id=%s AND status='processing'",
            (available_at, str(error)[:2000], event_id),
        )

    def mark_dead(self, event_id: str, error: str, worker_id: str | None = None) -> None:
        self._finish(
            event_id,
            worker_id,
            "UPDATE memory_outbox SET status='dead',attempts=attempts+1,"
            "last_error=%s,locked_at=NULL,locked_by=NULL "
            "WHERE event_id=%s AND status='processing'",
            (str(error)[:2000], event_id),
        )

    def enqueue_repair(self, event: OutboxEvent, dedupe_key: str) -> bool:
        if not dedupe_key:
            raise ValueError("memory repair dedupe key is required")
        record = event.memory_record()
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT INTO memory_outbox "
                "(event_id,aggregate_id,user_id,aggregate_version,event_type,target,payload,repair_dedupe_key) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT (repair_dedupe_key) DO NOTHING",
                (
                    event.event_id or str(uuid.uuid4()),
                    record.memory_id,
                    record.user_id,
                    record.version,
                    str(event.event_type),
                    str(event.target),
                    json.dumps(record.payload(), ensure_ascii=False),
                    dedupe_key,
                ),
            )
            return int(cursor.rowcount or 0) == 1

    def status(self) -> dict[str, int]:
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT status,COUNT(*) FROM memory_outbox "
                "WHERE status IN ('pending','processing','dead') GROUP BY status"
            )
            values = {str(status): int(count) for status, count in cursor.fetchall()}
        return {
            "pending": values.get("pending", 0),
            "processing": values.get("processing", 0),
            "dead": values.get("dead", 0),
        }

    def load_memory_page(self, after_id: int = 0, limit: int = 500) -> list[MemoryRecord]:
        """Read authoritative rows including tombstones for reconciliation."""

        limit = max(1, min(int(limit), 5000))
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT id,user_id,content,importance,embedding,"
                "COALESCE(embedding_model,''),COALESCE(embedding_revision,''),"
                "category,tags,slot_hint,version,content_hash,created_at,"
                "EXTRACT(EPOCH FROM updated_at),last_accessed,EXTRACT(EPOCH FROM deleted_at),"
                "COALESCE(status,'active'),COALESCE(quarantine_reason,''),"
                "COALESCE(supersedes,'[]'::jsonb),EXTRACT(EPOCH FROM superseded_at) "
                "FROM long_term_memory WHERE id>%s ORDER BY id LIMIT %s",
                (int(after_id), limit),
            )
            rows = list(cursor.fetchall())
        records: list[MemoryRecord] = []
        for row in rows:
            tags = [str(item) for item in _json_list(row[8])]
            supersedes = [int(item) for item in _json_list(row[18])]
            records.append(
                MemoryRecord(
                    memory_id=int(row[0]),
                    user_id=str(row[1]),
                    content=str(row[2] or ""),
                    importance=float(row[3] or 0.0),
                    embedding=[float(item) for item in _json_list(row[4])],
                    embedding_model=str(row[5] or ""),
                    embedding_revision=str(row[6] or ""),
                    category=str(row[7] or ""),
                    tags=tags,
                    slot_hint=str(row[9] or ""),
                    version=int(row[10] or 1),
                    content_hash=str(row[11] or ""),
                    created_at=_timestamp(row[12]),
                    updated_at=_timestamp(row[13]),
                    last_accessed=_timestamp(row[14]),
                    deleted_at=_timestamp(row[15]) if row[15] is not None else None,
                    quarantined=row[16] == "quarantined",
                    quarantine_reason=str(row[17] or ""),
                    superseded=row[16] == "superseded",
                    superseded_at=_timestamp(row[19]) if row[19] is not None else None,
                    supersedes=supersedes,
                )
            )
        return records


class ProjectionTargetStore(Protocol):
    def get(self, memory_id: int, user_id: str) -> ProjectionState | None: ...
    def upsert(self, record: MemoryRecord) -> None: ...
    def delete(self, record: MemoryRecord) -> None: ...
    def list_page(self, after_id: int, limit: int) -> list[ProjectionState]: ...


class MilvusMemoryProjectionStore:
    """Versioned memory vectors in a tenant-labelled Milvus collection."""

    def __init__(self, client: Any, dimension: int) -> None:
        self.client = client
        self.dimension = max(1, int(dimension))

    def initialize(self) -> None:
        if self.client is None:
            raise RuntimeError("milvus unavailable")
        if self.client.has_collection(MEMORY_VECTOR_COLLECTION):
            load = getattr(self.client, "load_collection", None)
            if callable(load):
                load(collection_name=MEMORY_VECTOR_COLLECTION)
            return
        from pymilvus import DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(field_name="memory_id", datatype=DataType.INT64, is_primary=True)
        schema.add_field(field_name="user_id", datatype=DataType.VARCHAR, max_length=256)
        schema.add_field(field_name="version", datatype=DataType.INT64)
        schema.add_field(field_name="content_hash", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=self.dimension)
        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="IVF_FLAT",
            metric_type="L2",
            params={"nlist": 128},
        )
        self.client.create_collection(
            collection_name=MEMORY_VECTOR_COLLECTION,
            schema=schema,
            index_params=index_params,
        )
        load = getattr(self.client, "load_collection", None)
        if callable(load):
            load(collection_name=MEMORY_VECTOR_COLLECTION)

    @staticmethod
    def _tenant_literal(user_id: str) -> str:
        return json.dumps(str(user_id), ensure_ascii=False)

    def get(self, memory_id: int, user_id: str) -> ProjectionState | None:
        rows = self.client.query(
            collection_name=MEMORY_VECTOR_COLLECTION,
            filter=(
                f"memory_id == {int(memory_id)} && "
                f"user_id == {self._tenant_literal(user_id)}"
            ),
            output_fields=["memory_id", "user_id", "version", "content_hash"],
            limit=1,
        )
        if not rows:
            return None
        row = rows[0]
        return ProjectionState(
            memory_id=int(row["memory_id"]),
            version=int(row["version"]),
            content_hash=str(row.get("content_hash") or ""),
            user_id=str(row.get("user_id") or ""),
        )

    def upsert(self, record: MemoryRecord) -> None:
        vector = [float(item) for item in record.embedding[: self.dimension]]
        if len(vector) < self.dimension:
            vector.extend([0.0] * (self.dimension - len(vector)))
        result = self.client.upsert(
            collection_name=MEMORY_VECTOR_COLLECTION,
            data=[
                {
                    "memory_id": record.memory_id,
                    "user_id": record.user_id,
                    "version": record.version,
                    "content_hash": record.content_hash,
                    "embedding": vector,
                }
            ],
        )
        if result is False:
            raise RuntimeError("milvus memory upsert failed")

    def delete(self, record: MemoryRecord) -> None:
        result = self.client.delete(
            collection_name=MEMORY_VECTOR_COLLECTION,
            filter=(
                f"memory_id == {record.memory_id} && "
                f"user_id == {self._tenant_literal(record.user_id)} && "
                f"version <= {record.version}"
            ),
        )
        if result is False:
            raise RuntimeError("milvus memory delete failed")

    def list_page(self, after_id: int, limit: int) -> list[ProjectionState]:
        rows = self.client.query(
            collection_name=MEMORY_VECTOR_COLLECTION,
            filter=f"memory_id > {int(after_id)}",
            output_fields=["memory_id", "user_id", "version", "content_hash"],
            limit=max(1, int(limit)),
        )
        return [
            ProjectionState(
                memory_id=int(row["memory_id"]),
                version=int(row["version"]),
                content_hash=str(row.get("content_hash") or ""),
                user_id=str(row.get("user_id") or ""),
            )
            for row in sorted(rows or [], key=lambda value: int(value["memory_id"]))
        ]


class Neo4jMemoryProjectionStore:
    """Strict Neo4j adapter; driver exceptions are allowed to trigger retry."""

    def __init__(self, client: Any) -> None:
        self.client = client

    @property
    def _driver(self):
        driver = getattr(self.client, "driver", None)
        if driver is None:
            raise RuntimeError("neo4j unavailable")
        return driver

    def _run(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._driver.session(default_access_mode="WRITE") as session:
            result = session.run(query, params)
            rows = [record.data() for record in result]
            consume = getattr(result, "consume", None)
            if callable(consume):
                consume()
            return rows

    def get(self, memory_id: int, user_id: str) -> ProjectionState | None:
        rows = self._run(
            "MATCH (m:Memory {user_id:$user_id,mem_id:$id}) "
            "RETURN m.mem_id AS memory_id,m.user_id AS user_id,"
            "coalesce(m.version,0) AS version,coalesce(m.content_hash,'') AS content_hash LIMIT 1",
            {"user_id": user_id, "id": int(memory_id)},
        )
        if not rows:
            return None
        row = rows[0]
        return ProjectionState(
            memory_id=int(row["memory_id"]),
            version=int(row["version"]),
            content_hash=str(row.get("content_hash") or ""),
            user_id=str(row.get("user_id") or ""),
        )

    def upsert(self, record: MemoryRecord) -> None:
        self._run(
            "MERGE (m:Memory {user_id:$user_id,mem_id:$id}) "
            "SET m.memory_id=$id,m.content=$content,m.importance=$importance,"
            "m.version=$version,m.content_hash=$content_hash,m.category=$category,"
            "m.tags=$tags,m.slot_hint=$slot_hint,m.updated_at=$updated_at",
            {
                "user_id": record.user_id,
                "id": record.memory_id,
                "content": record.content,
                "importance": record.importance,
                "version": record.version,
                "content_hash": record.content_hash,
                "category": record.category,
                "tags": list(record.tags),
                "slot_hint": record.slot_hint,
                "updated_at": record.updated_at,
            },
        )

    def upsert_edges(self, record: MemoryRecord) -> None:
        if not record.supersedes:
            return
        self._run(
            "MATCH (m:Memory {user_id:$user_id,mem_id:$id}) "
            "UNWIND $targets AS target_id "
            "MATCH (old:Memory {user_id:$user_id,mem_id:target_id}) "
            "MERGE (m)-[r:SUPERSEDES {user_id:$user_id}]->(old) "
            "SET r.memory_version=$version",
            {
                "user_id": record.user_id,
                "id": record.memory_id,
                "targets": list(record.supersedes),
                "version": record.version,
            },
        )

    def delete_edges(self, record: MemoryRecord) -> None:
        self._run(
            "MATCH (m:Memory {user_id:$user_id,mem_id:$id})"
            "-[r {user_id:$user_id}]-(:Memory {user_id:$user_id}) DELETE r",
            {"user_id": record.user_id, "id": record.memory_id},
        )

    def delete(self, record: MemoryRecord) -> None:
        self._run(
            "MATCH (m:Memory {user_id:$user_id,mem_id:$id}) "
            "WHERE coalesce(m.version,0) <= $version DETACH DELETE m",
            {"user_id": record.user_id, "id": record.memory_id, "version": record.version},
        )

    def list_page(self, after_id: int, limit: int) -> list[ProjectionState]:
        rows = self._run(
            "MATCH (m:Memory) WHERE m.mem_id>$after AND m.user_id IS NOT NULL "
            "RETURN m.mem_id AS memory_id,m.user_id AS user_id,"
            "coalesce(m.version,0) AS version,coalesce(m.content_hash,'') AS content_hash "
            "ORDER BY m.mem_id LIMIT $limit",
            {"after": int(after_id), "limit": max(1, int(limit))},
        )
        return [
            ProjectionState(
                memory_id=int(row["memory_id"]),
                version=int(row["version"]),
                content_hash=str(row.get("content_hash") or ""),
                user_id=str(row.get("user_id") or ""),
            )
            for row in rows
        ]


class MemoryTargetProjector:
    """Idempotent, version-aware application of one target's outbox events."""

    def __init__(self, target: Target, store: ProjectionTargetStore) -> None:
        self.target = Target(target)
        self.store = store

    def apply(self, event: OutboxEvent) -> None:
        if event.target != self.target:
            raise ValueError("memory projection target mismatch")
        record = event.memory_record()
        current = self.store.get(record.memory_id, record.user_id)
        if current is not None and current.user_id and current.user_id != record.user_id:
            raise ProjectionConflict("projection: tenant mismatch")
        if current is not None and record.version < current.version:
            return

        upserts = {
            Target.MILVUS: {EventType.UPSERT_MEMORY_VECTOR},
            Target.NEO4J: {
                EventType.UPSERT_MEMORY_GRAPH_NODE,
                EventType.UPSERT_MEMORY_GRAPH_EDGES,
            },
        }[self.target]
        deletes = {
            Target.MILVUS: {EventType.DELETE_MEMORY_VECTOR},
            Target.NEO4J: {
                EventType.DELETE_MEMORY_GRAPH_EDGES,
                EventType.DELETE_MEMORY_GRAPH_NODE,
            },
        }[self.target]
        if event.event_type in upserts:
            if current is not None and record.version == current.version:
                if current.content_hash != record.content_hash:
                    raise ProjectionConflict("projection: equal version hash conflict")
                if event.event_type != EventType.UPSERT_MEMORY_GRAPH_EDGES:
                    return
            if event.event_type == EventType.UPSERT_MEMORY_GRAPH_EDGES:
                edge_writer = getattr(self.store, "upsert_edges", None)
                if callable(edge_writer):
                    edge_writer(record)
            else:
                self.store.upsert(record)
            return
        if event.event_type in deletes:
            if current is None or record.version < current.version:
                return
            if event.event_type == EventType.DELETE_MEMORY_GRAPH_EDGES:
                edge_deleter = getattr(self.store, "delete_edges", None)
                if callable(edge_deleter):
                    edge_deleter(record)
            else:
                self.store.delete(record)
            return
        raise ValueError(f"unsupported memory projection event: {event.event_type}")


class MemoryProjectionWorker:
    def __init__(
        self,
        repository: PostgresMemoryOutboxRepository,
        projector: MemoryTargetProjector,
        *,
        worker_id: str,
        batch_size: int = 100,
        lease_seconds: float = 30.0,
        max_attempts: int = 10,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repository = repository
        self.projector = projector
        self.worker_id = worker_id
        self.batch_size = max(1, min(int(batch_size), 500))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.clock = clock

    def process_once(self) -> int:
        events = self.repository.claim(
            self.projector.target,
            self.worker_id,
            self.batch_size,
            self.lease_seconds,
        )
        processed = 0
        for event in events:
            try:
                self.projector.apply(event)
                self.repository.mark_processed(event.event_id, self.worker_id)
                processed += 1
            except Exception as exc:
                if event.attempts + 1 >= self.max_attempts:
                    self.repository.mark_dead(event.event_id, str(exc), self.worker_id)
                else:
                    delay = min(300, 2 ** min(event.attempts, 8))
                    self.repository.mark_retry(
                        event.event_id,
                        str(exc),
                        self.clock() + timedelta(seconds=delay),
                        self.worker_id,
                    )
        return processed


@dataclass(slots=True)
class ReconcileReport:
    checked: int = 0
    missing: int = 0
    stale: int = 0
    orphan: int = 0
    repair_enqueued: int = 0


class MemoryReconciler:
    def __init__(
        self,
        source: PostgresMemoryOutboxRepository,
        target_store: ProjectionTargetStore,
        target: Target,
        *,
        page_size: int = 500,
    ) -> None:
        self.source = source
        self.target_store = target_store
        self.target = Target(target)
        self.page_size = max(1, min(int(page_size), 5000))

    @staticmethod
    def _all_pages(loader: Callable[[int, int], list[Any]], page_size: int) -> list[Any]:
        output: list[Any] = []
        after = 0
        while True:
            page = loader(after, page_size)
            if not page:
                return output
            output.extend(page)
            next_after = max(int(getattr(value, "memory_id")) for value in page)
            if next_after <= after:
                raise RuntimeError("memory reconciliation cursor did not advance")
            after = next_after
            if len(page) < page_size:
                return output

    def _events_for(self, record: MemoryRecord) -> Iterable[tuple[EventType, str]]:
        deleted = record.deleted_at is not None
        if self.target == Target.MILVUS:
            event_type = EventType.DELETE_MEMORY_VECTOR if deleted else EventType.UPSERT_MEMORY_VECTOR
            yield event_type, "delete" if deleted else "upsert"
            return
        if deleted:
            yield EventType.DELETE_MEMORY_GRAPH_EDGES, "delete-edges"
            yield EventType.DELETE_MEMORY_GRAPH_NODE, "delete-node"
        else:
            yield EventType.UPSERT_MEMORY_GRAPH_NODE, "upsert-node"
            yield EventType.UPSERT_MEMORY_GRAPH_EDGES, "upsert-edges"

    def _enqueue(self, record: MemoryRecord) -> int:
        count = 0
        for event_type, operation in self._events_for(record):
            event = OutboxEvent(
                id=0,
                event_id=str(uuid.uuid4()),
                aggregate_id=record.memory_id,
                user_id=record.user_id,
                aggregate_version=record.version,
                event_type=event_type,
                target=self.target,
                payload=record.payload(),
            )
            key = (
                f"{self.target}:{record.user_id}:{record.memory_id}:"
                f"{record.version}:{operation}:repair"
            )
            if self.source.enqueue_repair(event, key):
                count += 1
        return count

    def run_once(self) -> ReconcileReport:
        report = ReconcileReport()
        authoritative = self._all_pages(self.source.load_memory_page, self.page_size)
        projected = self._all_pages(self.target_store.list_page, self.page_size)
        target_by_key = {
            (state.user_id, state.memory_id): state for state in projected
        }
        for record in authoritative:
            report.checked += 1
            key = (record.user_id, record.memory_id)
            state = target_by_key.pop(key, None)
            should_be_deleted = record.deleted_at is not None
            if state is None:
                if not should_be_deleted:
                    report.missing += 1
                    report.repair_enqueued += self._enqueue(record)
                continue
            if (
                state.version != record.version
                or state.content_hash != record.content_hash
                or bool(state.deleted) != should_be_deleted
            ):
                report.stale += 1
                report.repair_enqueued += self._enqueue(record)
        for state in target_by_key.values():
            report.orphan += 1
            tombstone = MemoryRecord(
                memory_id=state.memory_id,
                user_id=state.user_id,
                version=max(1, state.version),
                deleted_at=time.time(),
                superseded=True,
            )
            tombstone.content_hash = compute_content_hash(tombstone)
            report.repair_enqueued += self._enqueue(tombstone)
        return report


class MemoryProjectionSupervisor:
    """Lifecycle owner for projection workers and periodic repair scans."""

    def __init__(
        self,
        repository: PostgresMemoryOutboxRepository,
        workers: list[MemoryProjectionWorker],
        reconcilers: list[MemoryReconciler],
        *,
        poll_seconds: float = 0.5,
        reconcile_seconds: float = 6 * 60 * 60,
    ) -> None:
        self.repository = repository
        self.workers = list(workers)
        self.reconcilers = list(reconcilers)
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.reconcile_seconds = max(1.0, float(reconcile_seconds))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for worker in self.workers:
            thread = threading.Thread(
                target=self._run_worker,
                args=(worker,),
                name=f"memory-projection-{worker.projector.target}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        for reconciler in self.reconcilers:
            thread = threading.Thread(
                target=self._run_reconciler,
                args=(reconciler,),
                name=f"memory-reconcile-{reconciler.target}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def close(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads.clear()
        self.repository.close()

    def status(self) -> dict[str, int]:
        return self.repository.status()

    def _run_worker(self, worker: MemoryProjectionWorker) -> None:
        while not self._stop.is_set():
            try:
                worker.process_once()
            except Exception as exc:
                logger.warning("memory projection cycle failed target=%s: %s", worker.projector.target, exc)
            self._stop.wait(self.poll_seconds)

    def _run_reconciler(self, reconciler: MemoryReconciler) -> None:
        # Run once on startup so a long reconcile interval does not preserve an
        # outage gap until the first six-hour tick.
        while not self._stop.is_set():
            try:
                report = reconciler.run_once()
                if report.repair_enqueued:
                    logger.info(
                        "memory reconciliation target=%s repairs=%d",
                        reconciler.target,
                        report.repair_enqueued,
                    )
            except Exception as exc:
                logger.warning("memory reconciliation failed target=%s: %s", reconciler.target, exc)
            self._stop.wait(self.reconcile_seconds)


__all__ = [
    "MEMORY_VECTOR_COLLECTION",
    "MemoryProjectionSupervisor",
    "MemoryProjectionWorker",
    "MemoryReconciler",
    "MemoryTargetProjector",
    "MilvusMemoryProjectionStore",
    "Neo4jMemoryProjectionStore",
    "PostgresMemoryOutboxRepository",
    "ReconcileReport",
]
