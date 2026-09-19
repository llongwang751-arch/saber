"""SQLite-backed product repositories used when optional PostgreSQL is absent.

These repositories deliberately implement the same small interfaces as the
legacy PostgreSQL repositories.  A user id is required on every read/write so
the local development mode has the same tenant boundary as production.
"""

from __future__ import annotations

import json
import hashlib
import heapq
import inspect
import math
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text


def _ensure_fts5_table(session) -> bool:
    try:
        session.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS agent_rag_chunks_fts USING fts5(
                pg_id UNINDEXED,
                user_id UNINDEXED,
                doc_hash UNINDEXED,
                content UNINDEXED,
                parent_content UNINDEXED,
                search_text,
                tokenize = 'unicode61'
            );
        """))
        return True
    except Exception:
        return False

from internal.document.library import (
    DOCUMENT_STATUS_ACTIVE,
    Document,
    DocumentVersion,
    WriteRequest,
    WriteResult,
    new_id,
    normalize_write_request,
)
from internal.repo.chathistory import Entry
from internal.repo.longterm import Row as LongTermRow
from internal.repo.ragchunk import Row as RagRow
from internal.memory.consistency import (
    CommittedChangeSet,
    EventType,
    MemoryRecord,
    MemoryVersionConflict,
    ProjectionConflict,
    Target,
    compute_content_hash,
)

from .models import (
    AgentChatHistoryRecord,
    AgentDocumentRecord,
    AgentDocumentVersionRecord,
    AgentLongTermMemoryRecord,
    AgentPreferenceRecord,
    AgentActionRecord,
    AgentRagChunkRecord,
    AgentTaskSnapshotRecord,
    AgentTraceRecord,
    MemoryOutboxRecord,
    MemoryProjectionRecord,
    RagProjectionJobRecord,
    utcnow,
)


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="ignore")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except Exception:
            return []
    return []


def _state_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="ignore")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def _outbox(
    session,
    *,
    user_id: str,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    aggregate_version: int = 1,
    target: Target | str = Target.LTM_CACHE,
) -> None:
    session.add(
        MemoryOutboxRecord(
            event_id=str(uuid.uuid4()),
            user_id=user_id,
            event_type=event_type,
            aggregate_id=str(aggregate_id),
            aggregate_version=int(aggregate_version),
            target=str(target),
            payload=payload,
            next_attempt_at=utcnow(),
        )
    )


def _memory_record(record: AgentLongTermMemoryRecord) -> MemoryRecord:
    return MemoryRecord(
        memory_id=int(record.id),
        user_id=record.user_id,
        content=record.content or "",
        importance=float(record.importance or 0.0),
        embedding=[float(value) for value in (record.embedding or [])],
        embedding_model=record.embedding_model or "",
        embedding_revision=record.embedding_revision or "",
        category=record.category or "",
        tags=[str(value) for value in (record.tags or [])],
        slot_hint=record.slot_hint or "",
        version=int(record.version or 1),
        content_hash=record.content_hash or "",
        created_at=float(record.created_at or 0.0),
        updated_at=float(record.updated_at or 0.0),
        last_accessed=float(record.last_accessed or 0.0),
        deleted_at=float(record.deleted_at) if record.deleted_at is not None else None,
        quarantined=record.status == "quarantined",
        quarantine_reason=record.quarantine_reason or "",
        superseded=record.status == "superseded",
        superseded_at=(
            float(record.superseded_at) if record.superseded_at is not None else None
        ),
        supersedes=[int(value) for value in (record.supersedes or [])],
    )


def _stamp_memory(record: AgentLongTermMemoryRecord, *, increment: bool = False) -> MemoryRecord:
    now = time.time()
    if increment:
        record.version = int(record.version or 1) + 1
    record.updated_at = now
    value = _memory_record(record)
    value.content_hash = compute_content_hash(value)
    record.content_hash = value.content_hash
    return value


def _projection_events(session, record: AgentLongTermMemoryRecord, *, deleted: bool = False) -> None:
    value = _memory_record(record)
    payload = value.payload()
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
    for event_type, target in specs:
        _outbox(
            session,
            user_id=record.user_id,
            event_type=str(event_type),
            aggregate_id=str(record.id),
            aggregate_version=record.version,
            target=target,
            payload=payload,
        )


class LocalPreferenceRepo:
    def __init__(self, store):
        self.store = store

    def save(self, user_id: str, key: str, value: str) -> None:
        with self.store.transaction() as session:
            record = session.get(AgentPreferenceRecord, (user_id, key))
            if record is None:
                record = AgentPreferenceRecord(user_id=user_id, key=key, value=value)
                session.add(record)
            else:
                record.value = value
                record.updated_at = utcnow()

    def load(self, user_id: str) -> dict[str, str]:
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentPreferenceRecord).where(AgentPreferenceRecord.user_id == user_id)
            ).all()
            return {record.key: record.value for record in records}


class LocalChatHistoryRepo:
    def __init__(self, store):
        self.store = store

    def save(self, role: str, content: str, user_id: str = "default_user", conversation_id: str = "") -> None:
        with self.store.transaction() as session:
            session.add(AgentChatHistoryRecord(user_id=user_id, role=role, content=content, conversation_id=conversation_id))

    def load(self, limit: int, user_id: str = "default_user", conversation_id: str = "") -> list[Entry]:
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentChatHistoryRecord)
                .where(AgentChatHistoryRecord.user_id == user_id)
                .where(AgentChatHistoryRecord.conversation_id == conversation_id)
                .order_by(AgentChatHistoryRecord.id.desc())
                .limit(max(1, min(int(limit), 1000)))
            ).all()
            records.reverse()
            return [
                Entry(
                    role=record.role,
                    content=record.content,
                    created_at=record.created_at.strftime("%H:%M:%S") if record.created_at else "",
                )
                for record in records
            ]


class LocalSnapshotRepo:
    def __init__(self, store):
        self.store = store

    def get(self, task_id: str, user_id: str = 'default_user'):
        with self.store.transaction() as session:
            record = session.get(AgentTaskSnapshotRecord, (task_id, user_id))
            return dict(record.state) if record is not None else None

    def finish_recovery(self, task, user_id, conversation_id):
        """Commit the recovered reply and checkpoint together, once per task."""
        with self.store.transaction() as session:
            session.execute(text('UPDATE users SET username=username WHERE id=:id'), {'id': user_id})
            record = session.get(AgentTaskSnapshotRecord, (task['task_id'], user_id))
            if record is None:
                raise KeyError('Task checkpoint does not exist')
            if (record.state or {}).get('recovery_reply_saved'):
                return False
            state = dict(task, recovery_reply_saved=True)
            session.add(AgentChatHistoryRecord(user_id=user_id, role='assistant',
                content=str(task.get('result') or ''), conversation_id=conversation_id))
            record.state = state
        task['recovery_reply_saved'] = True
        return True

    def save(self, task_id: str, state_json: Any, user_id: str = "default_user") -> None:
        state = _state_dict(state_json)
        with self.store.transaction() as session:
            record = session.get(AgentTaskSnapshotRecord, (task_id, user_id))
            if record is None:
                record = AgentTaskSnapshotRecord(task_id=task_id, user_id=user_id, state=state)
                session.add(record)
            else:
                record.state = state
                record.created_at = utcnow()

    def list(self, limit: int = 50, user_id: str = "default_user") -> list[dict[str, Any]]:
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentTaskSnapshotRecord)
                .where(AgentTaskSnapshotRecord.user_id == user_id)
                .order_by(AgentTaskSnapshotRecord.created_at.desc())
                .limit(max(1, min(int(limit), 500)))
            ).all()
            return [
                {
                    "task_id": record.task_id,
                    "state": record.state or {},
                    "created_at": record.created_at.isoformat() if record.created_at else None,
                }
                for record in records
            ]


class LocalLongTermRepo:
    def __init__(self, store):
        self.store = store

    def begin_user_message(self, user_id: str) -> int:
        from internal.harness.journal import fingerprint
        with self.store.transaction() as session:
            # Obtain the writer lock before reading the sequence. PostgreSQL
            # SQLAlchemy stores lock the same user row; SQLite serializes writers.
            changed = session.execute(text('UPDATE users SET username=username WHERE id=:id'), {'id': user_id})
            if changed.rowcount != 1:
                raise ValueError('Unknown memory owner')
            row = session.get(AgentActionRecord, (user_id, 'memory-order'))
            order = int(row.payload.get('order', 0)) + 1 if row else 1
            if row is None:
                row = AgentActionRecord(user_id=user_id, action_id='memory-order', fingerprint=fingerprint('memory-order'), status='active')
                session.add(row)
            row.payload = {'order': order}
            return order

    def commit_user_fact(self, user_id, key, value, *, order, embedding=None,
                         importance=0.7, category='profile', tags=None, slot_hint='', priority=0):
        """One transaction for preference, fact correction, ordering and outbox."""
        from internal.memory.facts import ALIASES, fact_key
        from internal.harness.journal import fingerprint
        key = ALIASES.get(key, key)
        tag = fact_key(key, value)
        content = f'用户{key}: {value}'
        slot_id = 'memory-slot:' + tag
        now = time.time()
        with self.store.transaction() as session:
            session.execute(text('UPDATE users SET username=username WHERE id=:id'), {'id': user_id})
            slot = session.get(AgentActionRecord, (user_id, slot_id))
            if slot is not None and (int(slot.payload.get('order', 0)), int(slot.payload.get('priority', 0))) > (int(order), int(priority)):
                # The same message may enrich a deterministic fact with a vector,
                # but may never weaken its precedence or change its value.
                if (int(slot.payload.get('order', 0)) == int(order)
                        and slot.payload.get('key') == key and slot.payload.get('value') == value
                        and embedding):
                    priority = int(slot.payload.get('priority', 0))
                else:
                    return None  # A delayed extraction must not undo a newer correction.
            record = None
            if slot:
                record = session.get(AgentLongTermMemoryRecord, slot.payload.get('memory_id'))
                if record is not None and record.user_id != user_id:
                    raise ValueError('Fact owner mismatch')
                if record is not None and record.status != 'active':
                    raise ValueError('Inactive fact requires explicit review, not automatic resurrection')
                old_key, old_value = slot.payload.get('key'), slot.payload.get('value')
                if old_key and old_key != key:
                    old_pref = session.get(AgentPreferenceRecord, (user_id, old_key))
                    if old_pref and old_pref.value == old_value:
                        session.delete(old_pref)
            if record is None:
                legacy = session.scalars(select(AgentLongTermMemoryRecord).where(
                    AgentLongTermMemoryRecord.user_id == user_id,
                    AgentLongTermMemoryRecord.status == 'active')).all()
                record = next((r for r in reversed(legacy) if tag in (r.tags or [])), None)
            if record is None:
                record = AgentLongTermMemoryRecord(user_id=user_id, content=content,
                    importance=importance, embedding=list(embedding or []), created_at=now,
                    last_accessed=now, category=category, tags=[], slot_hint=slot_hint,
                    score=0.0, version=1, updated_at=now)
                session.add(record)
            else:
                record.version += 1
                if record.content != content:
                    record.embedding = list(embedding or [])
                elif embedding:
                    record.embedding = list(embedding)
                record.content, record.updated_at = content, now
                record.status = 'active'
                record.deleted_at = None
            record.tags = list(dict.fromkeys([*(tags or []), tag, 'source:user', 'trust:user_asserted']))
            record.importance, record.category, record.slot_hint = float(importance), category, slot_hint
            session.flush()
            _stamp_memory(record)
            pref = session.get(AgentPreferenceRecord, (user_id, key))
            if pref is None:
                pref = AgentPreferenceRecord(user_id=user_id, key=key, value=value)
                session.add(pref)
            else:
                pref.value, pref.updated_at = value, utcnow()
            for alias, canonical in ALIASES.items():
                if canonical == key and alias != key:
                    obsolete = session.get(AgentPreferenceRecord, (user_id, alias))
                    if obsolete is not None:
                        session.delete(obsolete)
                    # Older extractors may have persisted an unnormalised alias.
                    # Keep a tombstone rather than leaving a contradictory active fact.
                    old_tag = 'factkey:' + hashlib.sha256(alias.encode()).hexdigest()
                    alias_rows = session.scalars(select(AgentLongTermMemoryRecord).where(
                        AgentLongTermMemoryRecord.user_id == user_id,
                        AgentLongTermMemoryRecord.status == 'active',
                        AgentLongTermMemoryRecord.id != record.id,
                        AgentLongTermMemoryRecord.content.startswith('用户' + alias + ':'))).all()
                    for alias_row in alias_rows:
                        if old_tag in (alias_row.tags or []):
                            alias_row.status, alias_row.superseded_at = 'superseded', now
                            _stamp_memory(alias_row, increment=True)
                            _projection_events(session, alias_row, deleted=True)
            if slot is None:
                slot = AgentActionRecord(user_id=user_id, action_id=slot_id, fingerprint=fingerprint(tag), status='active')
                session.add(slot)
            slot.payload = {'order': int(order), 'priority': int(priority), 'memory_id': record.id, 'key': key, 'value': value}
            _projection_events(session, record)
            return _memory_record(record)

    def create_committed(
        self,
        content: str,
        importance: float,
        embedding,
        created_at: float | None = None,
        last_accessed: float | None = None,
        category: str = "",
        tags: list[str] | None = None,
        slot_hint: str = "",
        score: float = 0.0,
        user_id: str = "default_user",
    ) -> MemoryRecord:
        """Commit the authoritative row and projection events atomically."""

        if not str(user_id or "").strip():
            raise ValueError("memorytx: user_id is required")
        now = time.time()
        record = AgentLongTermMemoryRecord(
            user_id=user_id,
            content=content,
            importance=float(importance),
            embedding=[float(value) for value in _json_list(embedding)],
            created_at=float(created_at or now),
            last_accessed=float(last_accessed or created_at or now),
            category=category or "",
            tags=[str(value) for value in (tags or [])],
            slot_hint=slot_hint or "",
            score=float(score),
            version=1,
            updated_at=now,
        )
        with self.store.transaction() as session:
            session.add(record)
            session.flush()
            _stamp_memory(record)
            _projection_events(session, record)
            return _memory_record(record)

    def save(
        self,
        content: str,
        importance: float,
        embedding_json,
        created_at: float | None = None,
        last_accessed: float | None = None,
        category: str = "",
        tags: list[str] | None = None,
        slot_hint: str = "",
        score: float = 0.0,
        user_id: str = "default_user",
    ) -> int:
        """Compatibility wrapper returning the committed database id."""

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

    def load(self, user_id: str = "default_user") -> list[LongTermRow]:
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentLongTermMemoryRecord)
                .where(
                    AgentLongTermMemoryRecord.user_id == user_id,
                    AgentLongTermMemoryRecord.deleted_at.is_(None),
                )
                .order_by(AgentLongTermMemoryRecord.id)
            ).all()
            return [self._row(record) for record in records]

    def load_committed(self, user_id: str = "default_user") -> list[LongTermRow]:
        """Strict reload used after a cache CAS conflict."""

        return self.load(user_id=user_id)

    def update_committed(
        self,
        item_id: int,
        content: str,
        importance: float,
        embedding,
        *,
        expected_version: int | None = None,
        user_id: str = "default_user",
    ) -> MemoryRecord:
        with self.store.transaction() as session:
            record = session.scalar(
                select(AgentLongTermMemoryRecord).where(
                    AgentLongTermMemoryRecord.id == item_id,
                    AgentLongTermMemoryRecord.user_id == user_id,
                ).with_for_update()
            )
            if record is None or record.deleted_at is not None:
                raise LookupError(f"memorytx: memory not found: {item_id}")
            if expected_version is not None and int(record.version) != int(expected_version):
                raise MemoryVersionConflict("memorytx: version conflict")
            record.content = content
            record.importance = float(importance)
            record.embedding = [float(value) for value in _json_list(embedding)]
            record.last_accessed = time.time()
            _stamp_memory(record, increment=True)
            _projection_events(session, record)
            return _memory_record(record)

    def update(
        self,
        item_id: int,
        content: str,
        importance: float,
        embedding_json,
        user_id: str = "default_user",
    ) -> MemoryRecord:
        return self.update_committed(
            item_id,
            content,
            importance,
            embedding_json,
            user_id=user_id,
        )

    def update_classified_committed(
        self,
        item_id: int,
        importance: float,
        tags: list[str],
        category: str,
        slot_hint: str,
        last_accessed: float,
        *,
        expected_version: int | None = None,
        user_id: str = "default_user",
    ) -> MemoryRecord:
        with self.store.transaction() as session:
            record = session.scalar(
                select(AgentLongTermMemoryRecord).where(
                    AgentLongTermMemoryRecord.id == item_id,
                    AgentLongTermMemoryRecord.user_id == user_id,
                ).with_for_update()
            )
            if record is None or record.deleted_at is not None:
                raise LookupError(f"memorytx: memory not found: {item_id}")
            if expected_version is not None and int(record.version) != int(expected_version):
                raise MemoryVersionConflict("memorytx: version conflict")
            record.importance = float(importance)
            record.tags = [str(value) for value in (tags or [])]
            record.category = category or ""
            record.slot_hint = slot_hint or ""
            record.last_accessed = float(last_accessed)
            _stamp_memory(record, increment=True)
            _projection_events(session, record)
            return _memory_record(record)

    def update_classified(
        self,
        item_id: int,
        importance: float,
        tags: list[str],
        category: str,
        slot_hint: str,
        last_accessed: float,
        user_id: str = "default_user",
    ) -> MemoryRecord:
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
    ) -> CommittedChangeSet:
        """CAS every consolidation mutation in one row+outbox transaction."""

        updates = list(getattr(plan, "updates", []) or [])
        deletes = list(getattr(plan, "deletes", []) or [])
        ids = {
            int(update.record.memory_id) for update in updates
        } | {int(entry.memory_id) for entry in deletes}
        if not ids:
            return CommittedChangeSet()
        if any(update.record.user_id != user_id for update in updates) or any(
            entry.user_id != user_id for entry in deletes
        ):
            raise ValueError("memorytx: consolidation crossed tenant boundary")
        with self.store.transaction() as session:
            rows = session.scalars(
                select(AgentLongTermMemoryRecord).where(
                    AgentLongTermMemoryRecord.user_id == user_id,
                    AgentLongTermMemoryRecord.id.in_(sorted(ids)),
                    AgentLongTermMemoryRecord.deleted_at.is_(None),
                ).with_for_update()
            ).all()
            by_id = {int(row.id): row for row in rows}
            if set(by_id) != ids:
                raise MemoryVersionConflict("memorytx: version conflict")
            expected = {
                int(update.record.memory_id): int(update.expected_version)
                for update in updates
            }
            expected.update(
                {int(entry.memory_id): int(entry.expected_version) for entry in deletes}
            )
            if any(int(by_id[item_id].version) != version for item_id, version in expected.items()):
                raise MemoryVersionConflict("memorytx: version conflict")

            changes = CommittedChangeSet()
            for update in updates:
                source = update.record
                row = by_id[int(source.memory_id)]
                row.content = source.content
                row.importance = float(source.importance)
                row.embedding = list(source.embedding)
                row.embedding_model = source.embedding_model
                row.embedding_revision = source.embedding_revision
                row.category = source.category or "general"
                row.tags = list(source.tags)
                row.slot_hint = source.slot_hint or ""
                row.last_accessed = float(source.last_accessed or time.time())
                row.status = (
                    "quarantined" if source.quarantined
                    else "superseded" if source.superseded
                    else "active"
                )
                row.quarantine_reason = source.quarantine_reason or ""
                row.superseded_at = source.superseded_at
                row.supersedes = list(source.supersedes)
                _stamp_memory(row, increment=True)
                _projection_events(session, row)
                changes.upserts.append(_memory_record(row))
            for entry in deletes:
                row = by_id[int(entry.memory_id)]
                row.deleted_at = time.time()
                row.status = "superseded"
                row.superseded_at = row.deleted_at
                _stamp_memory(row, increment=True)
                _projection_events(session, row, deleted=True)
                changes.deletes.append(_memory_record(row))
            return changes

    def delete(self, ids: list[int], user_id: str = "default_user") -> None:
        clean_ids = sorted({int(value) for value in ids})
        if not clean_ids:
            return
        with self.store.transaction() as session:
            owned = session.scalars(
                select(AgentLongTermMemoryRecord).where(
                    AgentLongTermMemoryRecord.user_id == user_id,
                    AgentLongTermMemoryRecord.id.in_(clean_ids),
                    AgentLongTermMemoryRecord.deleted_at.is_(None),
                )
            ).all()
            for record in owned:
                record.deleted_at = time.time()
                record.status = "superseded"
                _stamp_memory(record, increment=True)
                _projection_events(session, record, deleted=True)

    def set_status_committed(
        self,
        ids: list[int],
        status: str,
        *,
        reason: str = "",
        superseded_by: int | None = None,
        expected_versions: dict[int, int] | None = None,
        user_id: str = "default_user",
    ) -> list[MemoryRecord]:
        clean_ids = sorted({int(value) for value in ids})
        if not clean_ids:
            return []
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentLongTermMemoryRecord).where(
                    AgentLongTermMemoryRecord.user_id == user_id,
                    AgentLongTermMemoryRecord.id.in_(clean_ids),
                ).with_for_update()
            ).all()
            by_id = {int(record.id): record for record in records}
            if expected_versions and any(item_id not in by_id for item_id in expected_versions):
                raise MemoryVersionConflict("memorytx: version conflict")
            committed: list[MemoryRecord] = []
            for record in records:
                if expected_versions and int(record.version) != int(
                    expected_versions.get(int(record.id), record.version)
                ):
                    raise MemoryVersionConflict("memorytx: version conflict")
                record.status = status
                record.quarantine_reason = reason or ""
                record.superseded_by = superseded_by
                record.superseded_at = time.time() if status == "superseded" else None
                _stamp_memory(record, increment=True)
                _projection_events(session, record)
                committed.append(_memory_record(record))
            return committed

    def mark_superseded_committed(
        self,
        old_ids: list[int],
        new_id: int,
        *,
        expected_versions: dict[int, int],
        user_id: str = "default_user",
    ) -> CommittedChangeSet:
        """Atomically link both sides of a supersession relationship."""

        old_set = {int(value) for value in old_ids if int(value) != int(new_id)}
        if not old_set:
            return CommittedChangeSet()
        all_ids = sorted({*old_set, int(new_id)})
        now = time.time()
        with self.store.transaction() as session:
            rows = session.scalars(
                select(AgentLongTermMemoryRecord).where(
                    AgentLongTermMemoryRecord.user_id == user_id,
                    AgentLongTermMemoryRecord.id.in_(all_ids),
                    AgentLongTermMemoryRecord.deleted_at.is_(None),
                ).with_for_update()
            ).all()
            by_id = {int(row.id): row for row in rows}
            if set(by_id) != set(all_ids):
                raise MemoryVersionConflict("memorytx: version conflict")
            if any(
                int(by_id[item_id].version) != int(expected_versions.get(item_id, -1))
                for item_id in all_ids
            ):
                raise MemoryVersionConflict("memorytx: version conflict")

            changes = CommittedChangeSet()
            marked: list[int] = []
            for item_id in sorted(old_set):
                row = by_id[item_id]
                if row.status == "superseded":
                    continue
                row.status = "superseded"
                row.superseded_by = int(new_id)
                row.superseded_at = now
                _stamp_memory(row, increment=True)
                _projection_events(session, row)
                changes.upserts.append(_memory_record(row))
                marked.append(item_id)
            if marked:
                replacement = by_id[int(new_id)]
                replacement.supersedes = list(
                    dict.fromkeys([*(replacement.supersedes or []), *marked])
                )
                _stamp_memory(replacement, increment=True)
                _projection_events(session, replacement)
                changes.upserts.append(_memory_record(replacement))
            return changes

    def set_status(
        self,
        ids: list[int],
        status: str,
        *,
        reason: str = "",
        superseded_by: int | None = None,
        user_id: str = "default_user",
    ) -> list[MemoryRecord]:
        return self.set_status_committed(
            ids,
            status,
            reason=reason,
            superseded_by=superseded_by,
            user_id=user_id,
        )

    @staticmethod
    def _row(record: AgentLongTermMemoryRecord) -> LongTermRow:
        return LongTermRow(
            id=record.id,
            content=record.content,
            importance=record.importance,
            embedding=[float(value) for value in (record.embedding or [])],
            created_at=record.created_at,
            last_accessed=record.last_accessed,
            category=record.category,
            tags=[str(value) for value in (record.tags or [])],
            slot_hint=record.slot_hint,
            score=record.score,
            status=record.status,
            superseded_by=record.superseded_by,
            superseded_at=record.superseded_at,
            supersedes=[int(value) for value in (record.supersedes or [])],
            quarantine_reason=record.quarantine_reason,
            version=record.version,
            content_hash=record.content_hash,
            updated_at=record.updated_at,
            deleted_at=record.deleted_at,
        )


class LocalDocumentRepo:
    def __init__(self, store):
        self.store = store

    def write(self, req: WriteRequest, user_id: str = "default_user") -> WriteResult:
        req = normalize_write_request(req)
        if not req.title:
            raise ValueError("title is required")
        if not req.content_md:
            raise ValueError("content_md is required")
        created = not bool(req.document_id)
        document_id = req.document_id or new_id("doc")
        with self.store.transaction() as session:
            document = session.get(AgentDocumentRecord, document_id)
            if created:
                document = AgentDocumentRecord(
                    id=document_id,
                    user_id=user_id,
                    title=req.title,
                    doc_type=req.doc_type,
                    source=req.source,
                    status=DOCUMENT_STATUS_ACTIVE,
                    created_by=user_id,
                )
                session.add(document)
                version = 1
            else:
                if document is None or document.user_id != user_id:
                    raise LookupError(f"document not found: {document_id}")
                latest = session.scalar(
                    select(func.max(AgentDocumentVersionRecord.version)).where(
                        AgentDocumentVersionRecord.document_id == document_id
                    )
                )
                version = int(latest or 0) + 1
                document.title = req.title
                document.doc_type = req.doc_type
                document.source = req.source
                document.status = DOCUMENT_STATUS_ACTIVE
                document.updated_at = utcnow()
            version_record = AgentDocumentVersionRecord(
                id=new_id("ver"),
                document_id=document_id,
                version=version,
                content_md=req.content_md,
                summary=req.summary,
                metadata_json=req.metadata or {},
            )
            session.add(version_record)
            session.flush()
            return WriteResult(
                document=self._document(document, version_record),
                version=self._version(version_record),
                created=created,
            )

    def list(self, user_id: str = "default_user") -> list[Document]:
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentDocumentRecord)
                .where(
                    AgentDocumentRecord.user_id == user_id,
                    AgentDocumentRecord.status != "deleted",
                )
                .order_by(AgentDocumentRecord.updated_at.desc())
            ).all()
            return [self._document(record, self._latest(session, record.id)) for record in records]

    def get(self, document_id: str, user_id: str = "default_user") -> tuple[Document, DocumentVersion]:
        if not (document_id or "").strip():
            raise ValueError("document_id is required")
        with self.store.transaction() as session:
            document = session.scalar(
                select(AgentDocumentRecord).where(
                    AgentDocumentRecord.id == document_id,
                    AgentDocumentRecord.user_id == user_id,
                    AgentDocumentRecord.status != "deleted",
                )
            )
            if document is None:
                raise LookupError(f"document not found: {document_id}")
            version = self._latest(session, document_id)
            if version is None:
                raise LookupError(f"document has no version: {document_id}")
            return self._document(document, version), self._version(version)

    def get_version(self, version_id: str, user_id: str = "default_user") -> DocumentVersion:
        if not (version_id or "").strip():
            raise ValueError("version_id is required")
        with self.store.transaction() as session:
            record = session.scalar(
                select(AgentDocumentVersionRecord)
                .join(AgentDocumentRecord, AgentDocumentRecord.id == AgentDocumentVersionRecord.document_id)
                .where(
                    AgentDocumentVersionRecord.id == version_id,
                    AgentDocumentRecord.user_id == user_id,
                    AgentDocumentRecord.status != "deleted",
                )
            )
            if record is None:
                raise LookupError(f"version not found: {version_id}")
            return self._version(record)

    def delete(self, document_id: str, user_id: str = "default_user") -> None:
        with self.store.transaction() as session:
            record = session.scalar(
                select(AgentDocumentRecord).where(
                    AgentDocumentRecord.id == document_id,
                    AgentDocumentRecord.user_id == user_id,
                    AgentDocumentRecord.status != "deleted",
                )
            )
            if record is None:
                raise LookupError(f"document not found: {document_id}")
            record.status = "deleted"
            record.updated_at = utcnow()

    @staticmethod
    def _latest(session, document_id: str):
        return session.scalar(
            select(AgentDocumentVersionRecord)
            .where(AgentDocumentVersionRecord.document_id == document_id)
            .order_by(AgentDocumentVersionRecord.version.desc())
            .limit(1)
        )

    @staticmethod
    def _document(record: AgentDocumentRecord, version: AgentDocumentVersionRecord | None) -> Document:
        return Document(
            id=record.id,
            title=record.title,
            doc_type=record.doc_type,
            source=record.source,
            status=record.status,
            created_by=record.created_by,
            created_at=record.created_at,
            updated_at=record.updated_at,
            latest_version=int(version.version if version else 0),
            latest_version_id=version.id if version else "",
        )

    @staticmethod
    def _version(record: AgentDocumentVersionRecord) -> DocumentVersion:
        return DocumentVersion(
            id=record.id,
            document_id=record.document_id,
            version=record.version,
            content_md=record.content_md,
            summary=record.summary,
            metadata=record.metadata_json or {},
            created_at=record.created_at,
        )


def _search_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    ascii_buffer = ""
    for char in (text or "").casefold():
        if "\u4e00" <= char <= "\u9fff":
            if ascii_buffer:
                tokens.append(ascii_buffer)
                ascii_buffer = ""
            tokens.append(char)
        elif char.isalnum():
            ascii_buffer += char
        elif ascii_buffer:
            tokens.append(ascii_buffer)
            ascii_buffer = ""
    if ascii_buffer:
        tokens.append(ascii_buffer)
    return tokens


def _lexical_score(query: str, content: str) -> float:
    query_counts = Counter(_search_tokens(query))
    content_counts = Counter(_search_tokens(content))
    if not query_counts or not content_counts:
        return 0.0
    dot = sum(count * content_counts.get(token, 0) for token, count in query_counts.items())
    left = math.sqrt(sum(value * value for value in query_counts.values()))
    right = math.sqrt(sum(value * value for value in content_counts.values()))
    return dot / (left * right) if left and right else 0.0


class LocalRagChunkRepo:
    """Durable lexical fallback used without PG, Milvus or Elasticsearch."""

    local_available = True

    def __init__(self, store, projection_repo=None):
        self.store = store
        self.projection_repo = projection_repo

    def init(self, dim: int) -> None:
        return None

    def count(self, user_id: str = "default_user") -> int:
        with self.store.transaction() as session:
            value = session.scalar(
                select(func.count(AgentRagChunkRecord.id)).where(AgentRagChunkRecord.user_id == user_id)
            )
            return int(value or 0)

    def count_by_document_id(self, document_id: str, user_id: str = "default_user") -> int:
        with self.store.transaction() as session:
            value = session.scalar(
                select(func.count(AgentRagChunkRecord.id)).where(
                    AgentRagChunkRecord.user_id == user_id,
                    AgentRagChunkRecord.document_id == str(document_id or ""),
                )
            )
            return int(value or 0)

    def save_pg(self, doc_hash: str, chunk_idx: int, content: str, embedding_json, user_id: str = "default_user") -> int:
        return self.save_pg_with_parent(
            doc_hash, chunk_idx, content, "", embedding_json, user_id=user_id
        )

    def save_pg_with_parent(
        self,
        doc_hash: str,
        chunk_idx: int,
        content: str,
        parent_content: str,
        embedding_json,
        *,
        user_id: str = "default_user",
        document_id: str = "",
        version_id: str = "",
        section: str = "",
    ) -> int:
        with self.store.transaction() as session:
            record = session.scalar(
                select(AgentRagChunkRecord).where(
                    AgentRagChunkRecord.user_id == user_id,
                    AgentRagChunkRecord.doc_hash == doc_hash,
                    AgentRagChunkRecord.chunk_idx == int(chunk_idx),
                )
            )
            if record is None:
                record = AgentRagChunkRecord(
                    user_id=user_id,
                    doc_hash=doc_hash,
                    chunk_idx=int(chunk_idx),
                    content=content,
                )
                session.add(record)
            record.content = content
            record.parent_content = parent_content or ""
            record.embedding = [float(value) for value in _json_list(embedding_json)]
            record.document_id = document_id or record.document_id or ""
            record.version_id = version_id or record.version_id or ""
            record.section = section or record.section or ""
            session.flush()
            self._enqueue_upserts(session, record)
            try:
                _ensure_fts5_table(session)
                from internal.rag.fts5_index import fts_tokenize
                session.execute(
                    text("DELETE FROM agent_rag_chunks_fts WHERE pg_id = :pg_id AND user_id = :user_id"),
                    {"pg_id": str(record.id), "user_id": str(user_id)}
                )
                session.execute(
                    text("""
                        INSERT INTO agent_rag_chunks_fts (pg_id, user_id, doc_hash, content, parent_content, search_text)
                        VALUES (:pg_id, :user_id, :doc_hash, :content, :parent_content, :search_text)
                    """),
                    {
                        "pg_id": str(record.id),
                        "user_id": str(user_id),
                        "doc_hash": str(doc_hash),
                        "content": content,
                        "parent_content": parent_content or "",
                        "search_text": fts_tokenize(f"{content}\n{parent_content}"),
                    }
                )
            except Exception:
                pass
            return int(record.id)

    @staticmethod
    def _enqueue_upserts(session, record: AgentRagChunkRecord) -> None:
        payload = {
            "pg_id": int(record.id),
            "content": record.content,
            "doc_hash": record.doc_hash,
            "chunk_idx": int(record.chunk_idx),
            "embedding": list(record.embedding or []),
        }
        revision = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for target in ("elasticsearch", "milvus"):
            dedupe_key = f"upsert:{target}:{record.user_id}:{record.id}:{revision}"
            exists = session.scalar(
                select(RagProjectionJobRecord.id).where(RagProjectionJobRecord.dedupe_key == dedupe_key)
            )
            if exists is None:
                session.add(RagProjectionJobRecord(
                    dedupe_key=dedupe_key,
                    user_id=record.user_id,
                    target=target,
                    operation="upsert",
                    pg_id=int(record.id),
                    payload=payload,
                    next_attempt_at=utcnow(),
                ))

    @staticmethod
    def _enqueue_deletes(session, *, user_id: str, pg_ids: list[int]) -> None:
        """Commit projection deletes with the authoritative chunk deletion.

        A projection may still be offline when a document is removed.  Keeping
        this intent in the same SQLite transaction prevents stale ES/Milvus
        chunks from reappearing in later searches after the dependency recovers.
        """

        for pg_id in sorted({int(value) for value in pg_ids}):
            payload = {"pg_id": pg_id}
            for target in ("elasticsearch", "milvus"):
                dedupe_key = f"delete:{target}:{user_id}:{pg_id}"
                exists = session.scalar(
                    select(RagProjectionJobRecord.id).where(
                        RagProjectionJobRecord.dedupe_key == dedupe_key
                    )
                )
                if exists is None:
                    session.add(RagProjectionJobRecord(
                        dedupe_key=dedupe_key,
                        user_id=user_id,
                        target=target,
                        operation="delete",
                        pg_id=pg_id,
                        payload=payload,
                        next_attempt_at=utcnow(),
                    ))

    def load_by_ids(self, ids: list[int], user_id: str = "default_user"):
        rows = self.load_by_ids_with_parent(ids, user_id=user_id)
        return [RagRow(id=item["id"], content=item["content"]) for item in rows], None

    def load_by_ids_with_parent(self, ids: list[int], user_id: str = "default_user") -> list[dict[str, Any]]:
        if not ids:
            return []
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentRagChunkRecord).where(
                    AgentRagChunkRecord.user_id == user_id,
                    AgentRagChunkRecord.id.in_([int(value) for value in ids]),
                )
            ).all()
            by_id = {record.id: record for record in records}
            return [
                {
                    "id": by_id[item_id].id,
                    "content": by_id[item_id].content,
                    "parent_content": by_id[item_id].parent_content,
                    "document_id": by_id[item_id].document_id,
                    "version_id": by_id[item_id].version_id,
                    "section": by_id[item_id].section,
                }
                for item_id in [int(value) for value in ids]
                if item_id in by_id
            ]

    def load_all(self, user_id: str = "default_user"):
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentRagChunkRecord)
                .where(AgentRagChunkRecord.user_id == user_id)
                .order_by(AgentRagChunkRecord.id)
            ).all()
            return [RagRow(id=record.id, content=record.content) for record in records], None

    def search_local(self, query: str, top_k: int, user_id: str = "default_user") -> list[dict[str, Any]]:
        # The lexical fallback needs no embeddings or ORM entities. Keep only
        # the best k rows while streaming one tenant's text; preserve the
        # original cosine score and deterministic id tie-break exactly.
        query_counts = Counter(_search_tokens(query))
        if not query_counts:
            return []
        query_norm = math.sqrt(sum(value * value for value in query_counts.values()))
        limit = max(1, min(int(top_k), 100))
        with self.store.transaction() as session:
            records = session.execute(
                select(
                    AgentRagChunkRecord.id,
                    AgentRagChunkRecord.content,
                    AgentRagChunkRecord.parent_content,
                    AgentRagChunkRecord.document_id,
                    AgentRagChunkRecord.version_id,
                    AgentRagChunkRecord.section,
                )
                .where(AgentRagChunkRecord.user_id == user_id)
                .execution_options(yield_per=128)
            )
            def candidates():
                for record in records:
                    counts = Counter(_search_tokens(f"{record.content}\n{record.parent_content}"))
                    dot = sum(count * counts.get(token, 0) for token, count in query_counts.items())
                    if dot:
                        norm = math.sqrt(sum(value * value for value in counts.values()))
                        yield record, dot / (query_norm * norm)
            scored = heapq.nsmallest(limit, candidates(), key=lambda item: (-item[1], item[0].id))
            return [
                {
                    "pg_id": record.id,
                    "content": record.content,
                    "parent_content": record.parent_content,
                    "score": score,
                    "source": "local_keyword",
                    "document_id": record.document_id,
                    "version_id": record.version_id,
                    "section": record.section,
                }
                for record, score in scored
            ]

    def search_fts5(self, query: str, top_k: int, user_id: str = "default_user") -> list[dict[str, Any]]:
        """Tenant-filtered BM25 search powered by SQLite FTS5."""
        from internal.rag.fts5_index import build_fts_query
        self.repair_fts5(user_id=user_id)
        fts_q = build_fts_query(query)
        if not fts_q:
            return []
        limit = max(1, min(int(top_k), 100))
        with self.store.transaction() as session:
            try:
                _ensure_fts5_table(session)
                rows = session.execute(
                    text("""
                        SELECT pg_id, content, parent_content, bm25(agent_rag_chunks_fts) as rank
                        FROM agent_rag_chunks_fts
                        WHERE agent_rag_chunks_fts MATCH :query AND user_id = :user_id
                        ORDER BY rank ASC
                        LIMIT :limit
                    """),
                    {"query": f"search_text : ({fts_q})", "user_id": str(user_id), "limit": limit}
                ).fetchall()

                by_id = {record.id: record for record in session.execute(
                    select(AgentRagChunkRecord.id, AgentRagChunkRecord.document_id,
                           AgentRagChunkRecord.version_id, AgentRagChunkRecord.section).where(AgentRagChunkRecord.user_id == user_id,
                        AgentRagChunkRecord.id.in_([int(row[0]) for row in rows]))).all()}
                results = []
                for row in rows:
                    pg_id, raw_content, raw_parent, rank = row
                    record = by_id.get(int(pg_id))
                    if record is None:
                        continue
                    strength = max(0.0, -float(rank)) if rank is not None else 0.0
                    score = strength / (1.0 + strength)
                    results.append({
                        "pg_id": int(pg_id),
                        "content": raw_content,
                        "parent_content": raw_parent,
                        "score": score,
                        "source": "local_keyword",
                        "engine": "fts5",
                        "document_id": record.document_id,
                        "version_id": record.version_id,
                        "section": record.section,
                    })
                return results
            except Exception:
                return []

    def lightweight_rows(self, user_id: str = "default_user") -> list[dict]:
        # No vector JSON is decoded for query-time source validation.
        with self.store.transaction() as session:
            rows = session.execute(select(
                AgentRagChunkRecord.id, AgentRagChunkRecord.content,
                AgentRagChunkRecord.parent_content, AgentRagChunkRecord.document_id,
                AgentRagChunkRecord.version_id, AgentRagChunkRecord.section,
            ).where(AgentRagChunkRecord.user_id == user_id).order_by(AgentRagChunkRecord.id).limit(10001)).mappings().all()
            if len(rows) > 10000:
                raise RuntimeError('Lightweight index exceeds 10000 chunks per user; use a larger backend')
            return [dict(row) for row in rows]

    def repair_fts5(self, user_id: str = "default_user") -> int:
        """Repair legacy/missing/stale entries without modifying source chunks."""
        from internal.rag.fts5_index import fts_tokenize
        with self.store.transaction() as session:
            if not _ensure_fts5_table(session):
                return 0
            session.execute(text('''DELETE FROM agent_rag_chunks_fts
                WHERE user_id=:user AND NOT EXISTS (SELECT 1 FROM agent_rag_chunks c
                WHERE c.user_id=:user AND c.id=CAST(agent_rag_chunks_fts.pg_id AS INTEGER))'''), {'user': user_id})
            rows = session.execute(text('''SELECT c.id, c.doc_hash, c.content, c.parent_content
                FROM agent_rag_chunks c WHERE c.user_id=:user AND NOT EXISTS
                (SELECT 1 FROM agent_rag_chunks_fts f WHERE f.user_id=:user
                AND CAST(f.pg_id AS INTEGER)=c.id AND f.content=c.content
                AND f.parent_content=COALESCE(c.parent_content, ''))'''), {'user': user_id}).all()
            for pid, doc_hash, content, parent in rows:
                args = {'pid': str(pid), 'user': user_id, 'doc': doc_hash, 'content': content,
                        'parent': parent or '', 'tokens': fts_tokenize(content + '\n' + (parent or ''))}
                session.execute(text('DELETE FROM agent_rag_chunks_fts WHERE pg_id=:pid AND user_id=:user'), args)
                session.execute(text('''INSERT INTO agent_rag_chunks_fts
                    (pg_id,user_id,doc_hash,content,parent_content,search_text)
                    VALUES (:pid,:user,:doc,:content,:parent,:tokens)'''), args)
            return len(rows)

    def load_all_for_reindex(self, user_id: str = "default_user") -> list[dict[str, Any]]:
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentRagChunkRecord)
                .where(AgentRagChunkRecord.user_id == user_id)
                .order_by(AgentRagChunkRecord.id)
            ).all()
            return [
                {
                    "pg_id": record.id,
                    "content": record.content,
                    "doc_hash": record.doc_hash,
                    "chunk_idx": record.chunk_idx,
                    "embedding": list(record.embedding or []),
                }
                for record in records
            ]

    def delete(self, doc_hash: str, user_id: str = "default_user") -> None:
        self.delete_by_doc_hash(doc_hash, user_id=user_id)

    def delete_by_doc_hash(self, doc_hash: str, user_id: str = "default_user") -> list[int]:
        with self.store.transaction() as session:
            ids = session.scalars(
                select(AgentRagChunkRecord.id).where(
                    AgentRagChunkRecord.user_id == user_id,
                    AgentRagChunkRecord.doc_hash == doc_hash,
                )
            ).all()
            self._enqueue_deletes(session, user_id=user_id, pg_ids=[int(value) for value in ids])
            session.execute(
                delete(AgentRagChunkRecord).where(
                    AgentRagChunkRecord.user_id == user_id,
                    AgentRagChunkRecord.doc_hash == doc_hash,
                )
            )
            try:
                session.execute(
                    text("DELETE FROM agent_rag_chunks_fts WHERE doc_hash = :doc_hash AND user_id = :user_id"),
                    {"doc_hash": str(doc_hash), "user_id": str(user_id)}
                )
            except Exception:
                pass
            return [int(value) for value in ids]

    def delete_by_document_id(self, document_id: str, user_id: str = "default_user") -> None:
        with self.store.transaction() as session:
            ids = session.scalars(
                select(AgentRagChunkRecord.id).where(
                    AgentRagChunkRecord.user_id == user_id,
                    AgentRagChunkRecord.document_id == document_id,
                )
            ).all()
            self._enqueue_deletes(session, user_id=user_id, pg_ids=[int(value) for value in ids])
            session.execute(
                delete(AgentRagChunkRecord).where(
                    AgentRagChunkRecord.user_id == user_id,
                    AgentRagChunkRecord.document_id == document_id,
                )
            )
            try:
                for pid in ids:
                    session.execute(
                        text("DELETE FROM agent_rag_chunks_fts WHERE pg_id = :pg_id AND user_id = :user_id"),
                        {"pg_id": str(pid), "user_id": str(user_id)}
                    )
            except Exception:
                pass

    def search_es_dicts(self, query: str, top_k: int,
                        user_id: str = "default_user") -> list[dict[str, Any]]:
        if self.projection_repo is None:
            return []
        return self._projection_call("search_es_dicts", query, top_k, user_id=user_id)

    def search_milvus_dicts(self, query_emb: list[float], top_k: int,
                            user_id: str = "default_user") -> list[dict[str, Any]]:
        if self.projection_repo is None:
            return []
        return self._projection_call("search_milvus_dicts", query_emb, top_k, user_id=user_id)

    def index_es(self, *args, **kwargs):
        if self.projection_repo is None:
            return RuntimeError("elasticsearch projection unavailable")
        return self._projection_call("index_es", *args, **kwargs)

    def insert_milvus(self, *args, **kwargs):
        if self.projection_repo is None:
            return RuntimeError("milvus projection unavailable")
        return self._projection_call("insert_milvus", *args, **kwargs)

    def delete_es(self, pg_ids):
        if self.projection_repo is not None and hasattr(self.projection_repo, "delete_es"):
            return self.projection_repo.delete_es(pg_ids)
        return RuntimeError("elasticsearch projection unavailable")

    def delete_milvus(self, pg_ids, user_id: str = "default_user"):
        if self.projection_repo is not None and hasattr(self.projection_repo, "delete_milvus"):
            return self._projection_call("delete_milvus", pg_ids, user_id=user_id)
        return RuntimeError("milvus projection unavailable")

    def _projection_call(self, method_name: str, *args, **kwargs):
        """Forward supported context while retaining compatibility with test/legacy adapters."""

        method = getattr(self.projection_repo, method_name)
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        supported = {
            key: value
            for key, value in kwargs.items()
            if key in parameters or accepts_kwargs
        }
        return method(*args, **supported)


class LocalTraceRepo:
    """Tenant-scoped durable Agent Trace repository."""

    def __init__(self, store, *, retention_days: int = 30):
        self.store = store
        self.retention_days = max(1, int(retention_days))

    def save(
        self,
        trace_id: str,
        user_id: str,
        mode: str,
        query_redacted: str,
        status: str,
        trace: dict[str, Any],
    ) -> str:
        with self.store.transaction() as session:
            record = session.get(AgentTraceRecord, trace_id)
            if record is None:
                record = AgentTraceRecord(id=trace_id, user_id=user_id)
                session.add(record)
            elif record.user_id != user_id:
                raise PermissionError("trace belongs to another user")
            record.mode = str(mode or "unknown")[:32]
            record.query_redacted = str(query_redacted or "")
            record.status = str(status or "completed")[:32]
            record.trace = dict(trace or {})
            session.flush()
        return trace_id

    def list(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.store.transaction() as session:
            records = session.scalars(
                select(AgentTraceRecord)
                .where(AgentTraceRecord.user_id == user_id)
                .order_by(AgentTraceRecord.created_at.desc())
                .limit(max(1, min(int(limit), 200)))
            ).all()
            return [self._row(record, include_trace=False) for record in records]

    def get(self, user_id: str, trace_id: str) -> dict[str, Any] | None:
        with self.store.transaction() as session:
            record = session.scalar(
                select(AgentTraceRecord).where(
                    AgentTraceRecord.id == trace_id,
                    AgentTraceRecord.user_id == user_id,
                )
            )
            return self._row(record, include_trace=True) if record is not None else None

    def delete(self, user_id: str, trace_id: str) -> bool:
        with self.store.transaction() as session:
            result = session.execute(
                delete(AgentTraceRecord).where(
                    AgentTraceRecord.id == trace_id,
                    AgentTraceRecord.user_id == user_id,
                )
            )
            return bool(result.rowcount)

    def purge_expired(self, user_id: str | None = None, *, retention_days: int | None = None) -> int:
        days = max(1, int(retention_days or self.retention_days))
        cutoff = utcnow() - timedelta(days=days)
        with self.store.transaction() as session:
            statement = delete(AgentTraceRecord).where(AgentTraceRecord.created_at < cutoff)
            if user_id is not None:
                statement = statement.where(AgentTraceRecord.user_id == user_id)
            result = session.execute(statement)
            return int(result.rowcount or 0)

    @staticmethod
    def _row(record: AgentTraceRecord, *, include_trace: bool) -> dict[str, Any]:
        row = {
            "trace_id": record.id,
            "mode": record.mode,
            "query": record.query_redacted,
            "status": record.status,
            "created_at": record.created_at,
        }
        if include_trace:
            row["trace"] = record.trace or {}
        return row


class MemoryOutboxWorker:
    """Small retrying dispatcher for memory events written atomically with state."""

    def __init__(self, store, publisher=None, *, poll_seconds: float = 1.0, max_attempts: int = 10, rag_worker=None):
        self.store = store
        self.publisher = publisher
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.worker_id = f"python-{uuid.uuid4()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rag_worker = rag_worker

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="memory-outbox", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict[str, int]:
        with self.store.transaction() as session:
            pending = session.scalar(
                select(func.count(MemoryOutboxRecord.id)).where(MemoryOutboxRecord.status == "pending")
            )
            failed = session.scalar(
                select(func.count(MemoryOutboxRecord.id)).where(
                    MemoryOutboxRecord.status == "pending",
                    MemoryOutboxRecord.attempts > 0,
                )
            )
            dead = session.scalar(
                select(func.count(MemoryOutboxRecord.id)).where(MemoryOutboxRecord.status == "dead")
            )
            return {"pending": int(pending or 0), "retrying": int(failed or 0), "dead": int(dead or 0)}

    def process_once(self, limit: int = 100) -> int:
        now = utcnow()
        with self.store.transaction() as session:
            records = session.scalars(
                select(MemoryOutboxRecord)
                .where(
                    MemoryOutboxRecord.status == "pending",
                    MemoryOutboxRecord.next_attempt_at <= now,
                    (MemoryOutboxRecord.lease_until.is_(None) | (MemoryOutboxRecord.lease_until <= now)),
                )
                .order_by(MemoryOutboxRecord.id)
                .limit(max(1, min(int(limit), 500)))
            ).all()
            processed = 0
            for record in records:
                try:
                    record.lease_owner = self.worker_id
                    record.lease_until = utcnow() + timedelta(seconds=30)
                    self._apply_projection(session, record)
                    if self.publisher is not None and hasattr(self.publisher, "publish"):
                        self.publisher.publish(
                            record.event_type,
                            json.dumps(record.payload or {}, ensure_ascii=False),
                        )
                    record.processed_at = utcnow()
                    record.status = "processed"
                    record.lease_owner = ""
                    record.lease_until = None
                    record.last_error = ""
                    processed += 1
                except Exception as exc:
                    record.attempts += 1
                    record.last_error = str(exc)[:2000]
                    record.lease_owner = ""
                    record.lease_until = None
                    if record.attempts >= self.max_attempts:
                        record.status = "dead"
                        record.dead_at = utcnow()
                        continue
                    backoff = min(300, 2 ** min(record.attempts, 8))
                    record.next_attempt_at = utcnow() + timedelta(seconds=backoff)
            return processed

    @staticmethod
    def _apply_projection(session, event: MemoryOutboxRecord) -> None:
        """Apply one versioned event to the durable local projection ledger."""

        payload = dict(event.payload or {})
        memory_id = str(payload.get("memory_id") or event.aggregate_id)
        version = int(payload.get("version") or event.aggregate_version)
        content_hash = str(payload.get("content_hash") or "")
        current = session.get(MemoryProjectionRecord, (event.target, memory_id))
        if current is not None and version < current.version:
            return
        if current is not None and version == current.version:
            if content_hash != current.content_hash:
                raise ProjectionConflict("projection: equal version hash conflict")
            return
        is_delete = event.event_type in {
            str(EventType.DELETE_MEMORY_VECTOR),
            str(EventType.DELETE_MEMORY_GRAPH_EDGES),
            str(EventType.DELETE_MEMORY_GRAPH_NODE),
        }
        if current is None:
            current = MemoryProjectionRecord(
                target=event.target,
                aggregate_id=memory_id,
                version=version,
                content_hash=content_hash,
                deleted=is_delete,
                payload=payload,
            )
            session.add(current)
        else:
            current.version = version
            current.content_hash = content_hash
            current.deleted = is_delete
            current.payload = payload
            current.updated_at = utcnow()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.process_once()
                if self.rag_worker is not None:
                    self.rag_worker.process_once()
            except Exception:
                # The next poll retries; request processing must not depend on dispatch.
                pass


class RagProjectionOutboxWorker:
    """Retry ES/Milvus projection jobs committed with local RAG chunks."""

    def __init__(self, store, rag_repo, readiness, *, max_attempts: int = 10):
        self.store = store
        self.rag_repo = rag_repo
        self.readiness = readiness
        self.max_attempts = max(1, int(max_attempts))

    def status(self, user_id: str | None = None) -> dict[str, int]:
        with self.store.transaction() as session:
            statement = select(RagProjectionJobRecord)
            if user_id is not None:
                statement = statement.where(RagProjectionJobRecord.user_id == user_id)
            records = session.scalars(statement).all()
        return {
            "pending": sum(record.status == "pending" for record in records),
            "retrying": sum(record.status == "pending" and record.attempts > 0 for record in records),
            "dead": sum(record.status == "dead" for record in records),
            "processed": sum(record.status == "processed" for record in records),
        }

    def retry_dead(self, user_id: str) -> int:
        with self.store.transaction() as session:
            records = session.scalars(
                select(RagProjectionJobRecord).where(
                    RagProjectionJobRecord.user_id == user_id,
                    RagProjectionJobRecord.status == "dead",
                )
            ).all()
            for record in records:
                record.status = "pending"
                record.attempts = 0
                record.last_error = ""
                record.next_attempt_at = utcnow()
            return len(records)

    def process_once(self, limit: int = 100, *, user_id: str | None = None) -> int:
        now = utcnow()
        with self.store.transaction() as session:
            statement = select(RagProjectionJobRecord).where(
                RagProjectionJobRecord.status == "pending",
                RagProjectionJobRecord.next_attempt_at <= now,
            )
            if user_id is not None:
                statement = statement.where(RagProjectionJobRecord.user_id == user_id)
            jobs = session.scalars(
                statement.order_by(RagProjectionJobRecord.id)
                .limit(max(1, min(int(limit), 500)))
            ).all()
            processed = 0
            for job in jobs:
                if not self._target_ready(job.target):
                    continue
                try:
                    self._apply(job)
                    job.status = "processed"
                    job.processed_at = utcnow()
                    job.last_error = ""
                    processed += 1
                except Exception as exc:
                    job.attempts += 1
                    job.last_error = str(exc)[:2000]
                    if job.attempts >= self.max_attempts:
                        job.status = "dead"
                    else:
                        job.next_attempt_at = utcnow() + timedelta(seconds=min(300, 2 ** min(job.attempts, 8)))
            return processed

    def _target_ready(self, target: str) -> bool:
        if target == "elasticsearch":
            return getattr(self.readiness, "elasticsearch", "") == "connected"
        if target == "milvus":
            return getattr(self.readiness, "milvus", "") == "connected"
        return False

    def _apply(self, job: RagProjectionJobRecord) -> None:
        payload = dict(job.payload or {})
        if job.operation == "delete" and job.target == "elasticsearch":
            result = self.rag_repo.delete_es([int(payload["pg_id"])])
        elif job.operation == "delete" and job.target == "milvus":
            result = self.rag_repo.delete_milvus(
                [int(payload["pg_id"])], user_id=job.user_id
            )
        elif job.operation == "upsert" and job.target == "elasticsearch":
            result = self.rag_repo.index_es(
                int(payload["pg_id"]),
                payload["content"],
                payload["doc_hash"],
                int(payload["chunk_idx"]),
                user_id=job.user_id,
            )
        elif job.operation == "upsert" and job.target == "milvus":
            result = self.rag_repo.insert_milvus(
                [int(payload["pg_id"])],
                [payload["content"]],
                [list(payload.get("embedding") or [])],
                user_id=job.user_id,
            )
        else:
            raise ValueError(f"unsupported projection operation/target: {job.operation}/{job.target}")
        if isinstance(result, Exception):
            raise result


def install_local_repositories(inf, store, *, trace_retention_days: int = 30) -> MemoryOutboxWorker | None:
    """Install durable fallbacks when the configured PG adapter is unavailable."""

    repo = getattr(inf, "repo", None)
    if repo is None:
        from types import SimpleNamespace

        repo = SimpleNamespace()
        inf.repo = repo
    if not hasattr(repo, "ragtrace"):
        repo.ragtrace = LocalTraceRepo(store, retention_days=trace_retention_days)
    from internal.harness.journal import ActionJournal
    repo.action_journal = ActionJournal(store)
    current_documents = getattr(repo, "documents", None)
    # A repository supplied by a test or host integration is intentional.  Only
    # replace the built-in PG repository (identified by its ``pg`` adapter) or
    # fill an entirely empty Infrastructure object.
    if current_documents is not None and not hasattr(current_documents, "pg"):
        return None
    pg = getattr(current_documents, "pg", None)
    postgres_ready = bool(pg is not None and hasattr(pg, "is_real") and pg.is_real())
    if postgres_ready:
        # Production PostgreSQL uses Infrastructure's durable SKIP LOCKED
        # dispatcher.  Return it so FastAPI exposes/closes the same lifecycle
        # handle instead of silently reporting that no consumer exists.
        return getattr(inf, "memory_projection", None)
    publisher = getattr(repo, "events", None)
    repo.preference = LocalPreferenceRepo(store)
    repo.chat_history = LocalChatHistoryRepo(store)
    repo.snapshot = LocalSnapshotRepo(store)
    repo.ltm = LocalLongTermRepo(store)
    repo.documents = LocalDocumentRepo(store)
    projection_repo = getattr(repo, "ragchunk", None)
    repo.ragchunk = LocalRagChunkRepo(store, projection_repo=projection_repo)
    readiness = getattr(inf, "ready", None)
    if readiness is None:
        from types import SimpleNamespace
        readiness = SimpleNamespace(elasticsearch="disconnected", milvus="disconnected")
    rag_worker = RagProjectionOutboxWorker(store, repo.ragchunk, readiness)
    repo.rag_projection_outbox = rag_worker
    worker = MemoryOutboxWorker(store, publisher, rag_worker=rag_worker)
    worker.start()
    return worker


__all__ = [
    "LocalPreferenceRepo",
    "LocalChatHistoryRepo",
    "LocalSnapshotRepo",
    "LocalLongTermRepo",
    "LocalDocumentRepo",
    "LocalRagChunkRepo",
    "LocalTraceRepo",
    "MemoryOutboxWorker",
    "RagProjectionOutboxWorker",
    "install_local_repositories",
]
