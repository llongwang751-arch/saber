# longterm — 长期记忆条目仓储（Postgres 实现）。
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from internal.platform.postgres import PostgresClient
from internal.memory.consistency import (
    EventType,
    MemoryRecord,
    MemoryUnavailable,
    MemoryVersionConflict,
    Target,
    compute_content_hash,
)

logger = logging.getLogger(__name__)


@dataclass
class Row:
    """长期记忆条目的领域模型。"""
    id: int = 0
    content: str = ""
    importance: float = 0.0
    embedding: List[float] = field(default_factory=list)
    created_at: float = 0.0
    last_accessed: float = 0.0
    category: str = ""
    tags: List[str] = field(default_factory=list)
    slot_hint: str = ""
    score: float = 0.0
    status: str = "active"
    superseded_by: Optional[int] = None
    superseded_at: Optional[float] = None
    supersedes: List[int] = field(default_factory=list)
    quarantine_reason: str = ""
    version: int = 1
    content_hash: str = ""
    updated_at: float = 0.0
    deleted_at: Optional[float] = None


def _emb_to_str(embedding_json) -> str:
    if isinstance(embedding_json, (bytes, bytearray)):
        try:
            return bytes(embedding_json).decode("utf-8")
        except Exception:
            return "[]"
    if embedding_json is None:
        return "null"
    if isinstance(embedding_json, (list, tuple)):
        return json.dumps([float(item) for item in embedding_json])
    return str(embedding_json)


def _embedding_list(value) -> list[float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    return [float(item) for item in value] if isinstance(value, list) else []


def _insert_projection_events(cur, record: MemoryRecord, *, deleted: bool = False) -> None:
    specs = (
        (
            (EventType.DELETE_MEMORY_VECTOR, Target.MILVUS),
            (EventType.DELETE_MEMORY_GRAPH_EDGES, Target.NEO4J),
            (EventType.DELETE_MEMORY_GRAPH_NODE, Target.NEO4J),
        )
        if deleted
        else (
            (EventType.UPSERT_MEMORY_VECTOR, Target.MILVUS),
            (EventType.UPSERT_MEMORY_GRAPH_NODE, Target.NEO4J),
            (EventType.UPSERT_MEMORY_GRAPH_EDGES, Target.NEO4J),
        )
    )
    payload = json.dumps(record.payload(), ensure_ascii=False)
    for event_type, target in specs:
        cur.execute(
            "INSERT INTO memory_outbox "
            "(event_id,aggregate_id,user_id,aggregate_version,event_type,target,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (str(uuid.uuid4()), record.memory_id, record.user_id, record.version, str(event_type), str(target), payload),
        )


def _locked_record(cur, item_id: int, user_id: str) -> MemoryRecord | None:
    cur.execute(
        "SELECT id,user_id,content,importance,embedding,"
        "COALESCE(embedding_model,''),COALESCE(embedding_revision,''),"
        "category,tags,slot_hint,version,content_hash,created_at,"
        "EXTRACT(EPOCH FROM updated_at),last_accessed,EXTRACT(EPOCH FROM deleted_at),"
        "COALESCE(status,'active'),COALESCE(quarantine_reason,''),"
        "COALESCE(supersedes,'[]'::jsonb),EXTRACT(EPOCH FROM superseded_at) "
        "FROM long_term_memory "
        "WHERE id=%s AND user_id=%s FOR UPDATE",
        (item_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    tags = row[8]
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    supersedes = row[18]
    if isinstance(supersedes, str):
        try:
            supersedes = json.loads(supersedes)
        except Exception:
            supersedes = []
    return MemoryRecord(
        memory_id=int(row[0]),
        user_id=str(row[1]),
        content=row[2] or "",
        importance=float(row[3] or 0.0),
        embedding=_embedding_list(row[4]),
        embedding_model=row[5] or "",
        embedding_revision=row[6] or "",
        category=row[7] or "",
        tags=[str(item) for item in (tags or [])],
        slot_hint=row[9] or "",
        version=int(row[10] or 1),
        content_hash=row[11] or "",
        created_at=float(row[12] or 0.0),
        updated_at=float(row[13] or 0.0),
        last_accessed=float(row[14] or 0.0),
        deleted_at=float(row[15]) if row[15] is not None else None,
        quarantined=row[16] == "quarantined",
        quarantine_reason=row[17] or "",
        superseded=row[16] == "superseded",
        superseded_at=float(row[19]) if row[19] is not None else None,
        supersedes=[int(item) for item in (supersedes or [])],
    )


class PGRepo:
    """Postgres authoritative repository with transactional projection outbox."""

    def __init__(self, client: PostgresClient):
        self.client = client

    def create_committed(self, content: str, importance: float, embedding,
                         created_at: Optional[float] = None,
                         last_accessed: Optional[float] = None,
                         category: str = "",
                         tags: Optional[List[str]] = None,
                         slot_hint: str = "",
                         score: float = 0.0,
                         user_id: str = "default_user") -> MemoryRecord:
        """Commit one memory row and all target outbox events in one tx.

        Unlike the historical ``save`` implementation this method never turns
        an unavailable database or rollback into a successful-looking id.
        """
        if self.client is None or not self.client.is_real() or self.client.conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        if not str(user_id or "").strip():
            raise ValueError("memorytx: user_id is required")
        if created_at is None:
            created_at = time.time()
        if last_accessed is None:
            last_accessed = created_at
        if tags is None:
            tags = []
        category = category or "general"
        emb_param = _emb_to_str(embedding)
        conn = self.client.conn
        if conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO long_term_memory "
                    "(user_id, content, importance, embedding, created_at, last_accessed, "
                    " category, tags, slot_hint, score, version, updated_at) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, 1, NOW()) "
                    "RETURNING id,created_at,last_accessed,EXTRACT(EPOCH FROM updated_at)",
                    (user_id, content, importance, emb_param,
                     float(created_at), float(last_accessed),
                     category, json.dumps(list(tags)), slot_hint or "", float(score)),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("memorytx: insert returned no authoritative row")
                memory_id = int(row[0])
                record = MemoryRecord(
                    memory_id=memory_id,
                    user_id=user_id,
                    content=content,
                    importance=float(importance),
                    embedding=_embedding_list(embedding),
                    category=category,
                    tags=list(tags),
                    slot_hint=slot_hint or "",
                    version=1,
                    created_at=float(row[1] or created_at),
                    last_accessed=float(row[2] or last_accessed),
                    updated_at=float(row[3] or time.time()),
                )
                record.content_hash = compute_content_hash(record)
                cur.execute(
                    "UPDATE long_term_memory SET content_hash=%s WHERE id=%s AND version=1",
                    (record.content_hash, memory_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("memorytx: content hash update affected no row")
                _insert_projection_events(cur, record)
            conn.commit()
            return record
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("⚠️  长期记忆事务提交失败: %s", e)
            raise
        finally:
            conn.autocommit = prior_autocommit

    # 默认写入：保留旧 ``save -> id`` 协议，但失败必须抛出。
    def save(self, content: str, importance: float, embedding_json,
             created_at: Optional[float] = None,
             last_accessed: Optional[float] = None,
             category: str = "",
             tags: Optional[List[str]] = None,
             slot_hint: str = "",
             score: float = 0.0,
             user_id: str = "default_user") -> int:
        return self.create_committed(
            content,
            importance,
            embedding_json,
            created_at=created_at,
            last_accessed=last_accessed,
            category=category,
            tags=tags,
            slot_hint=slot_hint,
            score=score,
            user_id=user_id,
        ).memory_id

    # 加载全部长期记忆条目
    def load(self, user_id: str = "default_user") -> List[Row]:
        if self.client is None or not self.client.is_real():
            return []
        try:
            rows = self.client.query(
                "SELECT id, content, importance, embedding, "
                "created_at, last_accessed, "
                "COALESCE(category, ''), COALESCE(tags, '[]'::jsonb), "
                "COALESCE(slot_hint, ''), COALESCE(score, 0.0), "
                "COALESCE(status, 'active'), superseded_by, COALESCE(quarantine_reason, ''), "
                "COALESCE(version, 1), COALESCE(content_hash, ''), updated_at, deleted_at, "
                "EXTRACT(EPOCH FROM superseded_at), COALESCE(supersedes, '[]'::jsonb) "
                "FROM long_term_memory WHERE user_id = %s AND deleted_at IS NULL ORDER BY id",
                (user_id,),
            )
        except Exception as e:
            logger.warning("⚠️  加载长期记忆失败: %s", e)
            return []
        return self._decode_rows(rows)

    def load_committed(self, user_id: str = "default_user") -> List[Row]:
        """Strict authoritative reload used after an already-committed CAS race."""

        if self.client is None or not self.client.is_real() or self.client.conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        with self.client.conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, content, importance, embedding, created_at, last_accessed, "
                "COALESCE(category, ''), COALESCE(tags, '[]'::jsonb), "
                "COALESCE(slot_hint, ''), COALESCE(score, 0.0), "
                "COALESCE(status, 'active'), superseded_by, COALESCE(quarantine_reason, ''), "
                "COALESCE(version, 1), COALESCE(content_hash, ''), updated_at, deleted_at, "
                "EXTRACT(EPOCH FROM superseded_at), COALESCE(supersedes, '[]'::jsonb) "
                "FROM long_term_memory WHERE user_id=%s AND deleted_at IS NULL ORDER BY id",
                (user_id,),
            )
            rows = list(cursor.fetchall())
        return self._decode_rows(rows)

    @staticmethod
    def _decode_rows(rows) -> List[Row]:
        items: List[Row] = []
        for r in rows:
            try:
                rid, content, importance, emb_json, created_at, last_accessed, \
                    category, tags, slot_hint, score, status, superseded_by, quarantine_reason, \
                    version, content_hash, updated_at, deleted_at, superseded_at, supersedes = (
                        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12],
                        r[13], r[14], r[15], r[16], r[17], r[18]
                    )
                embedding: List[float] = []
                if emb_json:
                    try:
                        if isinstance(emb_json, (bytes, bytearray)):
                            embedding = json.loads(bytes(emb_json).decode("utf-8"))
                        elif isinstance(emb_json, str):
                            embedding = json.loads(emb_json)
                        elif isinstance(emb_json, list):
                            embedding = emb_json
                    except Exception:
                        embedding = []
                # tags: psycopg2 在 JSONB 列上通常直接返回 list；兼容 str 形式
                if isinstance(tags, (bytes, bytearray)):
                    try:
                        tags = json.loads(bytes(tags).decode("utf-8"))
                    except Exception:
                        tags = []
                elif isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except Exception:
                        tags = []
                if not isinstance(tags, list):
                    tags = []
                if isinstance(supersedes, (bytes, bytearray)):
                    try:
                        supersedes = json.loads(bytes(supersedes).decode("utf-8"))
                    except Exception:
                        supersedes = []
                elif isinstance(supersedes, str):
                    try:
                        supersedes = json.loads(supersedes)
                    except Exception:
                        supersedes = []
                if not isinstance(supersedes, list):
                    supersedes = []

                def _to_ts(v):
                    if v is None:
                        return 0.0
                    if hasattr(v, "timestamp"):
                        try:
                            return float(v.timestamp())
                        except Exception:
                            return 0.0
                    try:
                        return float(v)
                    except Exception:
                        return 0.0

                items.append(Row(
                    id=int(rid),
                    content=content or "",
                    importance=float(importance) if importance is not None else 0.0,
                    embedding=embedding,
                    created_at=_to_ts(created_at),
                    last_accessed=_to_ts(last_accessed),
                    category=category or "",
                    tags=[str(t) for t in tags],
                    slot_hint=slot_hint or "",
                    score=float(score) if score is not None else 0.0,
                    status=status or "active",
                    superseded_by=int(superseded_by) if superseded_by is not None else None,
                    superseded_at=_to_ts(superseded_at) if superseded_at is not None else None,
                    supersedes=[int(value) for value in supersedes],
                    quarantine_reason=quarantine_reason or "",
                    version=int(version or 1),
                    content_hash=content_hash or "",
                    updated_at=_to_ts(updated_at),
                    deleted_at=_to_ts(deleted_at) if deleted_at is not None else None,
                ))
            except Exception:
                continue
        return items

    def update_committed(self, item_id: int, content: str, importance: float, embedding,
                         *, expected_version: int | None = None,
                         user_id: str = "default_user") -> MemoryRecord:
        if self.client is None or not self.client.is_real():
            raise MemoryUnavailable("memorytx: postgres unavailable")
        conn = self.client.conn
        if conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                record = _locked_record(cur, item_id, user_id)
                if record is None or record.deleted_at is not None:
                    raise LookupError(f"memorytx: memory not found: {item_id}")
                if expected_version is not None and record.version != int(expected_version):
                    raise MemoryVersionConflict("memorytx: version conflict")
                record.content = content
                record.importance = float(importance)
                record.embedding = _embedding_list(embedding)
                record.version += 1
                record.updated_at = time.time()
                record.last_accessed = record.updated_at
                record.content_hash = compute_content_hash(record)
                cur.execute(
                    "UPDATE long_term_memory SET content=%s,importance=%s,embedding=%s::jsonb,"
                    "version=%s,updated_at=NOW(),last_accessed=%s,content_hash=%s "
                    "WHERE id=%s AND user_id=%s AND version=%s",
                    (content, importance, json.dumps(record.embedding), record.version, record.last_accessed,
                     record.content_hash, item_id, user_id, record.version - 1),
                )
                if cur.rowcount != 1:
                    raise MemoryVersionConflict("memorytx: version conflict")
                _insert_projection_events(cur, record)
            conn.commit()
            return record
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("⚠️  长期记忆更新失败 (id=%d): %s", item_id, e)
            raise
        finally:
            conn.autocommit = prior_autocommit

    # 修改一条长期记忆；旧调用者可忽略返回的权威记录。
    def update(self, item_id: int, content: str, importance: float, embedding_json,
               user_id: str = "default_user") -> MemoryRecord:
        return self.update_committed(
            item_id,
            content,
            importance,
            embedding_json,
            user_id=user_id,
        )

    def update_classified_committed(self, item_id: int, importance: float,
                                    tags: List[str], category: str,
                                    slot_hint: str, last_accessed: float,
                                    *, expected_version: int | None = None,
                                    user_id: str = "default_user") -> MemoryRecord:
        if self.client is None or not self.client.is_real():
            raise MemoryUnavailable("memorytx: postgres unavailable")
        conn = self.client.conn
        if conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                record = _locked_record(cur, item_id, user_id)
                if record is None or record.deleted_at is not None:
                    raise LookupError(f"memorytx: memory not found: {item_id}")
                if expected_version is not None and record.version != int(expected_version):
                    raise MemoryVersionConflict("memorytx: version conflict")
                record.importance = float(importance)
                record.tags = list(tags or [])
                record.category = category or ""
                record.slot_hint = slot_hint or ""
                record.last_accessed = float(last_accessed)
                record.version += 1
                record.updated_at = time.time()
                record.content_hash = compute_content_hash(record)
                cur.execute(
                    "UPDATE long_term_memory SET importance=%s,tags=%s::jsonb,category=%s,slot_hint=%s,"
                    "last_accessed=%s,version=%s,updated_at=NOW(),content_hash=%s "
                    "WHERE id=%s AND user_id=%s AND version=%s",
                    (record.importance, json.dumps(record.tags), record.category, record.slot_hint,
                     record.last_accessed, record.version, record.content_hash, item_id, user_id, record.version - 1),
                )
                if cur.rowcount != 1:
                    raise MemoryVersionConflict("memorytx: version conflict")
                _insert_projection_events(cur, record)
            conn.commit()
            return record
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("⚠️  长期记忆 update_classified 失败 (id=%d): %s", item_id, e)
            raise
        finally:
            conn.autocommit = prior_autocommit

    # dedup 命中后只更新 Schema-driven 字段（不动 content/embedding）。
    def update_classified(self, item_id: int, importance: float,
                          tags: List[str], category: str,
                          slot_hint: str, last_accessed: float,
                          user_id: str = "default_user") -> MemoryRecord:
        return self.update_classified_committed(
            item_id,
            importance,
            tags,
            category,
            slot_hint,
            last_accessed,
            user_id=user_id,
        )

    def apply_consolidation_committed(
        self,
        plan,
        *,
        user_id: str = "default_user",
    ):
        """Atomically apply a versioned consolidation plan and its outbox."""

        from internal.memory.consistency import CommittedChangeSet

        updates = list(getattr(plan, "updates", []) or [])
        deletes = list(getattr(plan, "deletes", []) or [])
        if not updates and not deletes:
            return CommittedChangeSet()
        if any(update.record.user_id != user_id for update in updates) or any(
            entry.user_id != user_id for entry in deletes
        ):
            raise ValueError("memorytx: consolidation crossed tenant boundary")
        if self.client is None or not self.client.is_real() or self.client.conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        conn = self.client.conn
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = False
            changes = CommittedChangeSet()
            with conn.cursor() as cur:
                for update in updates:
                    source = update.record
                    record = _locked_record(cur, int(source.memory_id), user_id)
                    if record is None or record.deleted_at is not None:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    if record.version != int(update.expected_version):
                        raise MemoryVersionConflict("memorytx: version conflict")
                    record.content = source.content
                    record.importance = float(source.importance)
                    record.embedding = list(source.embedding)
                    record.embedding_model = source.embedding_model
                    record.embedding_revision = source.embedding_revision
                    record.category = source.category or "general"
                    record.tags = list(source.tags)
                    record.slot_hint = source.slot_hint or ""
                    record.last_accessed = float(source.last_accessed or time.time())
                    record.quarantined = bool(source.quarantined)
                    record.quarantine_reason = source.quarantine_reason or ""
                    record.superseded = bool(source.superseded)
                    record.supersedes = list(source.supersedes)
                    record.version += 1
                    record.updated_at = time.time()
                    record.content_hash = compute_content_hash(record)
                    status = (
                        "quarantined" if record.quarantined
                        else "superseded" if record.superseded
                        else "active"
                    )
                    cur.execute(
                        "UPDATE long_term_memory SET content=%s,importance=%s,embedding=%s::jsonb,"
                        "embedding_model=%s,embedding_revision=%s,category=%s,tags=%s::jsonb,"
                        "slot_hint=%s,last_accessed=%s,status=%s,quarantine_reason=%s,"
                        "supersedes=%s::jsonb,version=%s,updated_at=NOW(),content_hash=%s "
                        "WHERE id=%s AND user_id=%s AND version=%s AND deleted_at IS NULL",
                        (
                            record.content, record.importance, json.dumps(record.embedding),
                            record.embedding_model, record.embedding_revision, record.category,
                            json.dumps(record.tags), record.slot_hint, record.last_accessed, status,
                            record.quarantine_reason, json.dumps(record.supersedes), record.version,
                            record.content_hash, record.memory_id, user_id, int(update.expected_version),
                        ),
                    )
                    if cur.rowcount != 1:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    _insert_projection_events(cur, record)
                    changes.upserts.append(record)
                for entry in deletes:
                    record = _locked_record(cur, int(entry.memory_id), user_id)
                    if record is None or record.deleted_at is not None:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    if record.version != int(entry.expected_version):
                        raise MemoryVersionConflict("memorytx: version conflict")
                    record.version += 1
                    record.updated_at = time.time()
                    record.deleted_at = record.updated_at
                    record.superseded = True
                    record.superseded_at = record.updated_at
                    record.content_hash = compute_content_hash(record)
                    cur.execute(
                        "UPDATE long_term_memory SET deleted_at=NOW(),status='superseded',"
                        "superseded_at=NOW(),version=%s,updated_at=NOW(),content_hash=%s "
                        "WHERE id=%s AND user_id=%s AND version=%s AND deleted_at IS NULL",
                        (
                            record.version, record.content_hash, record.memory_id,
                            user_id, int(entry.expected_version),
                        ),
                    )
                    if cur.rowcount != 1:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    _insert_projection_events(cur, record, deleted=True)
                    changes.deletes.append(record)
            conn.commit()
            return changes
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.autocommit = prior_autocommit

    def delete_committed(self, ids: List[int], *, user_id: str = "default_user",
                         expected_versions: dict[int, int] | None = None) -> list[MemoryRecord]:
        if not ids:
            return []
        if self.client is None or not self.client.is_real():
            raise MemoryUnavailable("memorytx: postgres unavailable")
        conn = self.client.conn
        if conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = False
            committed: list[MemoryRecord] = []
            with conn.cursor() as cur:
                for item_id in sorted({int(value) for value in ids}):
                    record = _locked_record(cur, item_id, user_id)
                    if record is None or record.deleted_at is not None:
                        if expected_versions and item_id in expected_versions:
                            raise MemoryVersionConflict("memorytx: version conflict")
                        continue
                    if expected_versions and record.version != int(expected_versions.get(item_id, record.version)):
                        raise MemoryVersionConflict("memorytx: version conflict")
                    record.version += 1
                    record.updated_at = time.time()
                    record.deleted_at = record.updated_at
                    record.superseded = True
                    record.superseded_at = record.updated_at
                    record.content_hash = compute_content_hash(record)
                    cur.execute(
                        "UPDATE long_term_memory SET deleted_at=NOW(),status='superseded',version=%s,"
                        "superseded_at=NOW(),updated_at=NOW(),content_hash=%s "
                        "WHERE id=%s AND user_id=%s AND version=%s",
                        (record.version, record.content_hash, item_id, user_id, record.version - 1),
                    )
                    if cur.rowcount != 1:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    _insert_projection_events(cur, record, deleted=True)
                    committed.append(record)
            conn.commit()
            return committed
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("⚠️  长期记忆批量删除失败: %s", e)
            raise
        finally:
            conn.autocommit = prior_autocommit

    # 批量删除；实际是带版本号的 tombstone，旧调用者可忽略返回值。
    def delete(self, ids: List[int], user_id: str = "default_user") -> list[MemoryRecord]:
        return self.delete_committed(ids, user_id=user_id)

    def set_status_committed(
        self,
        ids: List[int],
        status: str,
        *,
        reason: str = "",
        superseded_by: Optional[int] = None,
        expected_versions: dict[int, int] | None = None,
        user_id: str = "default_user",
    ) -> list[MemoryRecord]:
        if not ids:
            return []
        if self.client is None or not self.client.is_real():
            raise MemoryUnavailable("memorytx: postgres unavailable")
        conn = self.client.conn
        if conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = False
            committed: list[MemoryRecord] = []
            with conn.cursor() as cur:
                for item_id in sorted({int(value) for value in ids}):
                    record = _locked_record(cur, item_id, user_id)
                    if record is None or record.deleted_at is not None:
                        if expected_versions and item_id in expected_versions:
                            raise MemoryVersionConflict("memorytx: version conflict")
                        continue
                    if expected_versions and record.version != int(
                        expected_versions.get(item_id, record.version)
                    ):
                        raise MemoryVersionConflict("memorytx: version conflict")
                    record.quarantined = status == "quarantined"
                    record.superseded = status == "superseded"
                    record.quarantine_reason = reason or ""
                    record.superseded_at = time.time() if record.superseded else None
                    record.version += 1
                    record.updated_at = time.time()
                    record.content_hash = compute_content_hash(record)
                    cur.execute(
                        "UPDATE long_term_memory SET status=%s,quarantine_reason=%s,superseded_by=%s,"
                        "superseded_at=%s,supersedes=%s::jsonb,version=%s,updated_at=NOW(),content_hash=%s "
                        "WHERE id=%s AND user_id=%s AND version=%s",
                        (status, reason, superseded_by, record.superseded_at,
                         json.dumps(record.supersedes), record.version, record.content_hash,
                         item_id, user_id, record.version - 1),
                    )
                    if cur.rowcount != 1:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    _insert_projection_events(cur, record)
                    committed.append(record)
            conn.commit()
            return committed
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("⚠️  长期记忆状态更新失败: %s", e)
            raise
        finally:
            conn.autocommit = prior_autocommit

    def mark_superseded_committed(
        self,
        old_ids: List[int],
        new_id: int,
        *,
        expected_versions: dict[int, int],
        user_id: str = "default_user",
    ):
        """Commit reverse and forward supersession provenance in one tx."""

        from internal.memory.consistency import CommittedChangeSet

        old_set = {int(value) for value in old_ids if int(value) != int(new_id)}
        if not old_set:
            return CommittedChangeSet()
        if self.client is None or not self.client.is_real() or self.client.conn is None:
            raise MemoryUnavailable("memorytx: postgres unavailable")
        conn = self.client.conn
        prior_autocommit = conn.autocommit
        now = time.time()
        try:
            conn.autocommit = False
            changes = CommittedChangeSet()
            with conn.cursor() as cur:
                records: dict[int, MemoryRecord] = {}
                for item_id in sorted({*old_set, int(new_id)}):
                    record = _locked_record(cur, item_id, user_id)
                    if record is None or record.deleted_at is not None:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    if record.version != int(expected_versions.get(item_id, -1)):
                        raise MemoryVersionConflict("memorytx: version conflict")
                    records[item_id] = record

                marked: list[int] = []
                for item_id in sorted(old_set):
                    record = records[item_id]
                    if record.superseded:
                        continue
                    record.superseded = True
                    record.superseded_at = now
                    record.version += 1
                    record.updated_at = now
                    record.content_hash = compute_content_hash(record)
                    cur.execute(
                        "UPDATE long_term_memory SET status='superseded',superseded_by=%s,"
                        "superseded_at=TO_TIMESTAMP(%s),version=%s,updated_at=NOW(),content_hash=%s "
                        "WHERE id=%s AND user_id=%s AND version=%s AND deleted_at IS NULL",
                        (
                            int(new_id), now, record.version, record.content_hash,
                            item_id, user_id, int(expected_versions[item_id]),
                        ),
                    )
                    if cur.rowcount != 1:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    _insert_projection_events(cur, record)
                    changes.upserts.append(record)
                    marked.append(item_id)
                if marked:
                    replacement = records[int(new_id)]
                    replacement.supersedes = list(
                        dict.fromkeys([*replacement.supersedes, *marked])
                    )
                    replacement.version += 1
                    replacement.updated_at = now
                    replacement.content_hash = compute_content_hash(replacement)
                    cur.execute(
                        "UPDATE long_term_memory SET supersedes=%s::jsonb,version=%s,"
                        "updated_at=NOW(),content_hash=%s WHERE id=%s AND user_id=%s "
                        "AND version=%s AND deleted_at IS NULL",
                        (
                            json.dumps(replacement.supersedes), replacement.version,
                            replacement.content_hash, int(new_id), user_id,
                            int(expected_versions[int(new_id)]),
                        ),
                    )
                    if cur.rowcount != 1:
                        raise MemoryVersionConflict("memorytx: version conflict")
                    _insert_projection_events(cur, replacement)
                    changes.upserts.append(replacement)
            conn.commit()
            return changes
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.autocommit = prior_autocommit

    def set_status(
        self,
        ids: List[int],
        status: str,
        *,
        reason: str = "",
        superseded_by: Optional[int] = None,
        user_id: str = "default_user",
    ) -> list[MemoryRecord]:
        return self.set_status_committed(
            ids,
            status,
            reason=reason,
            superseded_by=superseded_by,
            user_id=user_id,
        )
