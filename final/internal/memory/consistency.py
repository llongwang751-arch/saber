"""Versioned long-term-memory projection contracts shared with the Go build.

PostgreSQL/SQLite is authoritative.  Milvus and Neo4j are rebuildable
projections fed by a transactional outbox.  The small types in this module are
the Python equivalent of Go's ``domain/memory/consistency`` package.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol


class EventType(StrEnum):
    UPSERT_MEMORY_VECTOR = "upsert_memory_vector"
    DELETE_MEMORY_VECTOR = "delete_memory_vector"
    UPSERT_MEMORY_GRAPH_NODE = "upsert_memory_graph_node"
    DELETE_MEMORY_GRAPH_NODE = "delete_memory_graph_node"
    UPSERT_MEMORY_GRAPH_EDGES = "upsert_memory_graph_edges"
    DELETE_MEMORY_GRAPH_EDGES = "delete_memory_graph_edges"
    INVALIDATE_LTM_CACHE = "invalidate_ltm_cache"


class Target(StrEnum):
    MILVUS = "milvus"
    NEO4J = "neo4j"
    LTM_CACHE = "ltm_cache"


@dataclass(slots=True)
class MemoryRecord:
    memory_id: int
    user_id: str
    content: str = ""
    importance: float = 0.0
    embedding: list[float] = field(default_factory=list)
    embedding_model: str = ""
    embedding_revision: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    slot_hint: str = ""
    version: int = 1
    content_hash: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_accessed: float = 0.0
    deleted_at: float | None = None
    quarantined: bool = False
    quarantine_reason: str = ""
    superseded: bool = False
    superseded_at: float | None = None
    supersedes: list[int] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        # Go's JSON contract calls the identifier memory_id and omits empty
        # optional fields. Keeping payloads compact also makes hashes portable.
        return {key: item for key, item in value.items() if item not in (None, "", [], False)} | {
            "memory_id": self.memory_id,
            "user_id": self.user_id,
            "content": self.content,
            "importance": self.importance,
            "version": self.version,
            "content_hash": self.content_hash,
        }


@dataclass(slots=True)
class ProjectionState:
    memory_id: int
    version: int
    content_hash: str
    deleted: bool = False
    user_id: str = ""


class ProjectionConflict(RuntimeError):
    """An equal version carried different content, indicating corruption."""


class MemoryCommitError(RuntimeError):
    """An authoritative memory mutation did not commit."""


class MemoryUnavailable(MemoryCommitError):
    """The authoritative memory repository is unavailable."""


class MemoryVersionConflict(MemoryCommitError):
    """The caller attempted to replace a memory version it did not read."""


@dataclass(slots=True)
class CommittedChangeSet:
    """Rows and outbox event ids committed by one authoritative transaction."""

    upserts: list[MemoryRecord] = field(default_factory=list)
    deletes: list[MemoryRecord] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemoryUpdate:
    record: MemoryRecord
    expected_version: int


@dataclass(slots=True)
class MemoryDelete:
    memory_id: int
    user_id: str
    expected_version: int
    reason: str = ""


@dataclass(slots=True)
class ConsolidationPlan:
    updates: list[MemoryUpdate] = field(default_factory=list)
    deletes: list[MemoryDelete] = field(default_factory=list)


@dataclass(slots=True)
class OutboxEvent:
    """A leased PostgreSQL projection event.

    ``payload`` is kept as a mapping rather than an ORM object so a worker can
    validate the tenant/id/version envelope before touching an external
    projection.
    """

    id: int
    event_id: str
    aggregate_id: int
    user_id: str
    aggregate_version: int
    event_type: EventType
    target: Target
    payload: dict[str, Any]
    attempts: int = 0
    created_at: datetime | None = None

    def memory_record(self) -> MemoryRecord:
        value = dict(self.payload or {})
        record = MemoryRecord(
            memory_id=int(value.get("memory_id") or self.aggregate_id),
            user_id=str(value.get("user_id") or self.user_id),
            content=str(value.get("content") or ""),
            importance=float(value.get("importance") or 0.0),
            embedding=[float(item) for item in (value.get("embedding") or [])],
            embedding_model=str(value.get("embedding_model") or ""),
            embedding_revision=str(value.get("embedding_revision") or ""),
            category=str(value.get("category") or ""),
            tags=[str(item) for item in (value.get("tags") or [])],
            slot_hint=str(value.get("slot_hint") or ""),
            version=int(value.get("version") or self.aggregate_version),
            content_hash=str(value.get("content_hash") or ""),
            created_at=float(value.get("created_at") or 0.0),
            updated_at=float(value.get("updated_at") or 0.0),
            last_accessed=float(value.get("last_accessed") or 0.0),
            deleted_at=(
                float(value["deleted_at"])
                if value.get("deleted_at") is not None
                else None
            ),
            quarantined=bool(value.get("quarantined", False)),
            quarantine_reason=str(value.get("quarantine_reason") or ""),
            superseded=bool(value.get("superseded", False)),
            superseded_at=(
                float(value["superseded_at"])
                if value.get("superseded_at") is not None
                else None
            ),
            supersedes=[int(item) for item in (value.get("supersedes") or [])],
        )
        if record.memory_id != int(self.aggregate_id):
            raise MemoryCommitError("memory outbox aggregate id does not match payload")
        if record.user_id != self.user_id:
            raise MemoryCommitError("memory outbox tenant does not match payload")
        if record.version != int(self.aggregate_version):
            raise MemoryCommitError("memory outbox version does not match payload")
        if not record.user_id:
            raise MemoryCommitError("memory outbox tenant is required")
        if not record.content_hash:
            raise MemoryCommitError("memory outbox content hash is required")
        expected_hash = compute_content_hash(record)
        if record.content_hash != expected_hash:
            raise MemoryCommitError("memory outbox content hash verification failed")
        return record


def _rfc3339_nano(value: float | datetime | None) -> str:
    if value is None:
        return ""
    dt = value if isinstance(value, datetime) else datetime.fromtimestamp(float(value), timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def compute_content_hash(record: MemoryRecord) -> str:
    """Hash every field that can change a Milvus or Neo4j projection."""

    value: dict[str, Any] = {
        "memory_id": int(record.memory_id),
        "user_id": record.user_id,
        "content": record.content,
        "importance": float(record.importance),
    }
    optional = {
        "embedding": [float(item) for item in record.embedding],
        "embedding_model": record.embedding_model,
        "embedding_revision": record.embedding_revision,
        "category": record.category,
        "tags": sorted(str(item) for item in record.tags),
        "slot_hint": record.slot_hint,
        "deleted_at": _rfc3339_nano(record.deleted_at),
        "quarantined": bool(record.quarantined),
        "superseded": bool(record.superseded),
        "supersedes": sorted(int(item) for item in record.supersedes),
    }
    # Keep the same field order as Go's contentHashInput struct.
    for key in ("embedding", "embedding_model", "embedding_revision", "category", "tags", "slot_hint"):
        item = optional[key]
        if item not in ("", [], False):
            value[key] = item
    value["version"] = int(record.version)
    for key in ("deleted_at", "quarantined", "superseded", "supersedes"):
        item = optional[key]
        if item not in ("", [], False):
            value[key] = item
    def go_numbers(item):
        if isinstance(item, float) and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [go_numbers(value) for value in item]
        if isinstance(item, dict):
            return {key: go_numbers(value) for key, value in item.items()}
        return item

    # encoding/json uses compact separators, UTF-8 text, and HTML escaping.
    encoded_text = json.dumps(go_numbers(value), ensure_ascii=False, separators=(",", ":"))
    encoded_text = (
        encoded_text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    encoded = encoded_text.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProjectionStore(Protocol):
    def get(self, memory_id: int) -> ProjectionState | None: ...
    def upsert(self, record: MemoryRecord) -> None: ...
    def delete(self, memory_id: int, version: int) -> None: ...


class VersionedProjector:
    """Rejects stale/conflicting events and makes duplicate delivery harmless."""

    def __init__(
        self,
        store: ProjectionStore,
        target: Target,
        *,
        upserts: set[EventType],
        deletes: set[EventType],
    ) -> None:
        self.store = store
        self.target = target
        self.upserts = upserts
        self.deletes = deletes

    def apply(self, event_type: EventType | str, record: MemoryRecord) -> None:
        event_type = EventType(event_type)
        current = self.store.get(record.memory_id)
        if current is not None and record.version < current.version:
            return
        if event_type in self.upserts:
            if current is not None and record.version == current.version:
                if record.content_hash != current.content_hash:
                    raise ProjectionConflict("projection: equal version hash conflict")
                return
            self.store.upsert(record)
            return
        if event_type in self.deletes:
            if current is None or record.version < current.version:
                return
            self.store.delete(record.memory_id, record.version)


def milvus_projector(store: ProjectionStore) -> VersionedProjector:
    return VersionedProjector(
        store,
        Target.MILVUS,
        upserts={EventType.UPSERT_MEMORY_VECTOR},
        deletes={EventType.DELETE_MEMORY_VECTOR},
    )


def neo4j_projector(store: ProjectionStore) -> VersionedProjector:
    return VersionedProjector(
        store,
        Target.NEO4J,
        upserts={EventType.UPSERT_MEMORY_GRAPH_NODE, EventType.UPSERT_MEMORY_GRAPH_EDGES},
        deletes={EventType.DELETE_MEMORY_GRAPH_NODE, EventType.DELETE_MEMORY_GRAPH_EDGES},
    )
