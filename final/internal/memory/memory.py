# memory — 三层记忆系统（短期 / 长期 / 用户偏好）
import json
import inspect
import logging
import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, TYPE_CHECKING

from config.config import APIConfig
from internal.infra.infra import Infrastructure
from internal.memory.consistency import (
    CommittedChangeSet,
    ConsolidationPlan,
    MemoryCommitError,
    MemoryDelete,
    MemoryRecord,
    MemoryUpdate,
    compute_content_hash,
)

if TYPE_CHECKING:
    from internal.memory.graph_memory import GraphMemory  # noqa: F401

logger = logging.getLogger(__name__)


def _publish_event(inf, event_type: str, payload: Dict[str, Any]) -> None:
    try:
        events = getattr(getattr(inf, "repo", None), "events", None)
        if events is not None and hasattr(events, "publish"):
            events.publish(event_type, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning("⚠️  publish_event(%s) 失败: %s", event_type, e)


@dataclass
class Item:
    content: str
    importance: float = 0.5
    embedding: Optional[List[float]] = None
    # 用于在 GraphMemory 中追踪节点；未设置时由 GraphMemory 用 content hash 派生
    id: Optional[int] = None
    # 与 main 分支 Go Item 对齐的 Schema-driven 字段
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    category: str = ""
    tags: List[str] = field(default_factory=list)
    slot_hint: str = ""
    score: float = 0.0
    status: str = "active"
    superseded_by: Optional[int] = None
    superseded_at: Optional[float] = None
    # Forward provenance: this item replaced these older memory ids.  The
    # field is part of the projection hash and must survive cache reloads.
    supersedes: List[int] = field(default_factory=list)
    quarantine_reason: str = ""
    version: int = 1
    content_hash: str = ""
    updated_at: float = 0.0
    deleted_at: Optional[float] = None


@dataclass
class RecallFilter:
    """LongTerm.recall_by_filter 的过滤参数（与 main 分支 RecallFilter 对齐）。

    定义在 memory 包内以避免对 promptctx 的反向依赖；recall_by_filter 接受任何
    具备同名字段的 duck-typed 对象（如 promptctx.RecallFilter）。
    """

    categories: List[str] = field(default_factory=list)
    require_tags: List[str] = field(default_factory=list)
    max_age_hours: int = 0
    min_score: float = 0.0
    top_k: int = 0


@dataclass
class ConsolidationResult:
    """LongTerm.consolidate 的结构化返回。

    与 main 分支 Go ConsolidationResult 对齐：调用方据此向 PG / 图同步删除与更新，
    consolidate 自身只改内存。
    """
    deduped: int = 0
    merged: int = 0
    expired: int = 0
    delete_from_db: List[int] = field(default_factory=list)
    update_in_db: List["Item"] = field(default_factory=list)


class ShortTerm:
    """短期记忆 - 滑动窗口存储最近 N 轮对话。

    与 main 分支 Go ShortTerm 对齐：
    - 每条消息带 ``timestamp``（"HH:MM:SS"）
    - 通过可重入锁保护并发读写（写 add / clear，读 get / count）
    - 底层用 ``collections.deque(maxlen=max_turns*2)``，达到上限自动淘汰最早消息
    """

    def __init__(self, max_turns: int = 10):
        self.max_turns = max(1, max_turns)
        self.messages: Deque[Dict[str, str]] = deque(maxlen=self.max_turns * 2)
        self._lock = threading.RLock()

    def add(self, role: str, content: str):
        ts = time.strftime("%H:%M:%S", time.localtime())
        with self._lock:
            self.messages.append({"role": role, "content": content, "timestamp": ts})

    def get(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(m) for m in self.messages]

    def clear(self):
        with self._lock:
            self.messages.clear()

    def count(self) -> int:
        with self._lock:
            return len(self.messages)


def _tokenize_zh(text: str) -> List[str]:
    """中英文混合分词：中文按字、英文/数字按词。"""
    tokens: List[str] = []
    word = ""
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            if word:
                tokens.append(word.lower())
                word = ""
            tokens.append(ch)
        elif ch.isalnum():
            word += ch
        else:
            if word:
                tokens.append(word.lower())
                word = ""
    if word:
        tokens.append(word.lower())
    return tokens


class LongTerm:
    """长期记忆 - 基于 embedding 的语义记忆。"""

    def __init__(self, cfg: APIConfig, inf: Infrastructure, user_id: str = "default_user"):
        self.cfg = cfg
        self.inf = inf
        self.user_id = str(user_id or "default_user")
        self.items: List[Item] = []
        self._embed_fn: Optional[Any] = None
        self._last_consolidate_ts = 0.0
        self._items_since_last = 0
        # 图增强记忆（可选；None 表示未启用 / Neo4j 不可用，所有 hook 均跳过）
        self.graph_memory: Optional["GraphMemory"] = None
        self._next_id: int = 0
        # consolidate 阶段保护 self.items 的可重入锁
        self._lock = threading.RLock()

    def _copy_item(self, item: Item, score: Optional[float] = None) -> Item:
        return Item(
            content=item.content,
            importance=item.importance,
            embedding=list(item.embedding) if item.embedding else None,
            id=item.id,
            created_at=item.created_at,
            last_accessed=item.last_accessed,
            category=item.category,
            tags=list(item.tags),
            slot_hint=item.slot_hint,
            score=item.score if score is None else score,
            status=item.status,
            superseded_by=item.superseded_by,
            superseded_at=item.superseded_at,
            supersedes=list(item.supersedes),
            quarantine_reason=item.quarantine_reason,
            version=item.version,
            content_hash=item.content_hash,
            updated_at=item.updated_at,
            deleted_at=item.deleted_at,
        )

    def set_embed_fn(self, fn):
        self._embed_fn = fn

    def set_graph_memory(self, graph_memory: Optional["GraphMemory"]) -> None:
        """注入图增强记忆层；可在任意时刻调用，None 表示解除注入。

        同时反向把 self 注入到 graph_memory.ltm 上（如果对方支持），让 main 分支
        风格的代理方法（sync_prev_id / need_consolidation / set_consolidation_config）
        在装配后即可使用。
        """
        self.graph_memory = graph_memory
        if graph_memory is not None and hasattr(graph_memory, "set_ltm"):
            try:
                graph_memory.set_ltm(self)
            except Exception:
                pass

    def load_from_storage(self, *, strict: bool = False):
        method = (
            "load_committed"
            if strict and hasattr(self.inf.repo.ltm, "load_committed")
            else "load"
        )
        rows = self._repo_call(method)
        loaded: List[Item] = []
        for r in rows:
            created_ts = getattr(r, "created_at", None)
            if hasattr(created_ts, "timestamp"):
                created_ts = created_ts.timestamp()
            elif not isinstance(created_ts, (int, float)) or not created_ts:
                created_ts = time.time()
            last_ts = getattr(r, "last_accessed", None)
            if hasattr(last_ts, "timestamp"):
                last_ts = last_ts.timestamp()
            elif not isinstance(last_ts, (int, float)) or not last_ts:
                last_ts = created_ts
            loaded.append(Item(
                id=getattr(r, "id", None),
                content=r.content,
                importance=r.importance,
                embedding=r.embedding,
                created_at=float(created_ts),
                last_accessed=float(last_ts),
                category=getattr(r, "category", "") or "",
                tags=list(getattr(r, "tags", []) or []),
                slot_hint=getattr(r, "slot_hint", "") or "",
                score=float(getattr(r, "score", 0.0) or 0.0),
                status=getattr(r, "status", "") or "active",
                superseded_by=getattr(r, "superseded_by", None),
                superseded_at=(
                    self._timestamp(getattr(r, "superseded_at", None))
                    if getattr(r, "superseded_at", None) is not None
                    else None
                ),
                supersedes=[int(value) for value in (getattr(r, "supersedes", []) or [])],
                quarantine_reason=getattr(r, "quarantine_reason", "") or "",
                version=int(getattr(r, "version", 1) or 1),
                content_hash=getattr(r, "content_hash", "") or "",
                updated_at=float(getattr(r, "updated_at", 0.0) or 0.0),
                deleted_at=getattr(r, "deleted_at", None),
            ))
        # 重建 id 序列，确保后续 add 不与已有 item 冲突
        with self._lock:
            self.items = loaded
            existing_ids = [int(item.id) for item in self.items if item.id is not None]
            self._next_id = (max(existing_ids) + 1) if existing_ids else 0
        logger.info("✅ 从存储恢复了 %d 条长期记忆", len(loaded))
        # 图增强记忆 hook：批量索引（启动期把 LTM 全量同步进图）
        if self.graph_memory is not None:
            try:
                self.graph_memory.bulk_index(loaded)
            except Exception as e:
                logger.warning("⚠️  graph_memory.bulk_index 失败: %s", e)

    @staticmethod
    def _timestamp(value: Any, fallback: float = 0.0) -> float:
        if hasattr(value, "timestamp"):
            try:
                return float(value.timestamp())
            except Exception:
                return float(fallback)
        try:
            return float(value) if value is not None else float(fallback)
        except (TypeError, ValueError):
            return float(fallback)

    def _item_from_committed(self, value: Any, fallback: Item) -> Item:
        """Convert a repository commit result into the cache representation."""

        memory_id = getattr(value, "memory_id", getattr(value, "id", fallback.id))
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError) as exc:
            raise MemoryCommitError("memorytx: commit returned an invalid memory id") from exc
        if memory_id <= 0:
            raise MemoryCommitError("memorytx: commit returned a non-positive memory id")
        owner = getattr(value, "user_id", self.user_id)
        if owner and str(owner) != self.user_id:
            raise MemoryCommitError("memorytx: commit returned a row from another tenant")
        version = int(getattr(value, "version", fallback.version) or 1)
        if version <= 0:
            raise MemoryCommitError("memorytx: commit returned an invalid version")
        deleted_at = getattr(value, "deleted_at", fallback.deleted_at)
        quarantined = bool(getattr(value, "quarantined", False))
        superseded = bool(getattr(value, "superseded", False))
        status = getattr(value, "status", "") or (
            "quarantined" if quarantined else "superseded" if superseded else fallback.status
        )
        item = Item(
            id=memory_id,
            content=str(getattr(value, "content", fallback.content) or ""),
            importance=float(getattr(value, "importance", fallback.importance) or 0.0),
            embedding=list(getattr(value, "embedding", fallback.embedding) or []) or None,
            created_at=self._timestamp(getattr(value, "created_at", None), fallback.created_at),
            last_accessed=self._timestamp(
                getattr(value, "last_accessed", None), fallback.last_accessed
            ),
            category=str(getattr(value, "category", fallback.category) or ""),
            tags=[str(tag) for tag in (getattr(value, "tags", fallback.tags) or [])],
            slot_hint=str(getattr(value, "slot_hint", fallback.slot_hint) or ""),
            score=float(getattr(value, "score", fallback.score) or 0.0),
            status=status,
            superseded_by=getattr(value, "superseded_by", fallback.superseded_by),
            superseded_at=(
                self._timestamp(getattr(value, "superseded_at", None))
                if getattr(value, "superseded_at", None) is not None
                else fallback.superseded_at
            ),
            supersedes=[
                int(entry)
                for entry in (getattr(value, "supersedes", fallback.supersedes) or [])
            ],
            quarantine_reason=str(
                getattr(value, "quarantine_reason", fallback.quarantine_reason) or ""
            ),
            version=version,
            content_hash=str(getattr(value, "content_hash", "") or ""),
            updated_at=self._timestamp(getattr(value, "updated_at", None), fallback.updated_at),
            deleted_at=(
                self._timestamp(deleted_at) if deleted_at is not None else None
            ),
        )
        return item

    def _commit_create(self, item: Item) -> Item:
        """Persist row+outbox first and return the authoritative cache value."""

        repo = self.inf.repo.ltm
        create = getattr(repo, "create_committed", None)
        if callable(create):
            committed = self._repo_call(
                "create_committed",
                item.content,
                item.importance,
                list(item.embedding or []),
                created_at=item.created_at,
                last_accessed=item.last_accessed,
                category=item.category,
                tags=item.tags,
                slot_hint=item.slot_hint,
                score=item.score,
            )
            return self._item_from_committed(committed, item)

        # Legacy/test repositories retain save()->id.  A sentinel failure is
        # not success: accepting -1 used to create an in-memory ghost.
        stored_id = self._repo_call(
            "save",
            item.content,
            item.importance,
            json.dumps(item.embedding) if item.embedding else "null",
            created_at=item.created_at,
            last_accessed=item.last_accessed,
            category=item.category,
            tags=item.tags,
            slot_hint=item.slot_hint,
            score=item.score,
        )
        if not isinstance(stored_id, int) or stored_id <= 0:
            raise MemoryCommitError("memorytx: legacy repository did not commit the memory")
        record = MemoryRecord(
            memory_id=stored_id,
            user_id=self.user_id,
            content=item.content,
            importance=item.importance,
            embedding=list(item.embedding or []),
            category=item.category,
            tags=list(item.tags),
            slot_hint=item.slot_hint,
            version=1,
            created_at=item.created_at,
            updated_at=item.created_at,
            last_accessed=item.last_accessed,
        )
        record.content_hash = compute_content_hash(record)
        return self._item_from_committed(record, item)

    def _commit_classified_update(self, proposed: Item, expected_version: int) -> Item:
        repo = self.inf.repo.ltm
        commit = getattr(repo, "update_classified_committed", None)
        if callable(commit):
            result = self._repo_call(
                "update_classified_committed",
                int(proposed.id),
                proposed.importance,
                proposed.tags,
                proposed.category,
                proposed.slot_hint,
                proposed.last_accessed,
                expected_version=expected_version,
            )
            return self._item_from_committed(result, proposed)

        result = self._repo_call(
            "update_classified",
            int(proposed.id),
            proposed.importance,
            proposed.tags,
            proposed.category,
            proposed.slot_hint,
            proposed.last_accessed,
        )
        if result is not None:
            return self._item_from_committed(result, proposed)
        # Compatibility-only repositories signal success by returning None.
        record = MemoryRecord(
            memory_id=int(proposed.id),
            user_id=self.user_id,
            content=proposed.content,
            importance=proposed.importance,
            embedding=list(proposed.embedding or []),
            category=proposed.category,
            tags=list(proposed.tags),
            slot_hint=proposed.slot_hint,
            version=expected_version + 1,
            created_at=proposed.created_at,
            updated_at=proposed.last_accessed,
            last_accessed=proposed.last_accessed,
        )
        record.content_hash = compute_content_hash(record)
        return self._item_from_committed(record, proposed)

    def add(self, content: str, importance: float = 0.5) -> Item:
        embedding = None
        if self._embed_fn:
            try:
                embedding = self._embed_fn(content)
            except Exception as e:
                logger.warning("⚠️  向量化失败: %s", e)

        now_ts = time.time()
        item = Item(
            content=content,
            importance=importance,
            embedding=embedding,
            id=None,
            created_at=now_ts,
            last_accessed=now_ts,
        )
        committed = self._commit_create(item)
        with self._lock:
            prior = [self._copy_item(existing) for existing in self.items]
            self.items.append(committed)
            self._items_since_last += 1
            self._next_id = max(self._next_id, int(committed.id) + 1)
        _publish_event(self.inf, "memory.longterm.add", {
            "id": committed.id,
            "content": content,
            "importance": importance,
            "category": committed.category,
            "tags": committed.tags,
            "version": committed.version,
            "content_hash": committed.content_hash,
        })

        # 图增强记忆 hook：新增条目同步进图
        if self.graph_memory is not None:
            try:
                self.graph_memory.add_to_graph(committed, neighbors=prior[-50:])
            except Exception as e:
                logger.warning("⚠️  graph_memory.add_to_graph 失败: %s", e)
        return self._copy_item(committed)

    def store_classified(
        self,
        content: str,
        importance: float,
        emb: Optional[List[float]],
        category: str,
        tags: Optional[List[str]],
        slot_hint: str,
    ) -> bool:
        """Schema-driven 写入：写前对内存中已有 items 做 cosine dedup（与 main 分支
        Go StoreClassified 对齐）。命中阈值则只更新 importance / tags / category /
        slot_hint，并通过 repo.ltm.update_classified 同步到 PG，不插新行；未命中则
        走完整 add 路径写入 PG 并 append 到 self.items。

        无 emb 时跳过 dedup 直接插（fallback 不做 TF 相似度去重，避免误合并）。
        返回 True 表示新增成功，False 表示命中 dedup 仅做更新。
        """
        tags = list(tags or [])
        category = category or ""
        slot_hint = slot_hint or ""

        dedup_threshold = float(getattr(self.cfg, "memory_consolidation_dedup", 0.95) or 0.95)
        fact_keys = {tag for tag in tags if tag.startswith("factkey:")}
        if fact_keys:
            with self._lock:
                for idx, existing in enumerate(self.items):
                    if existing.status != "active" or not fact_keys.intersection(existing.tags):
                        continue
                    if existing.content == content:
                        return False
                    # Revise the same atomic fact with CAS + transactional outbox.
                    # Old versions remain in outbox provenance; cache changes only after commit.
                    repo = self.inf.repo.ltm
                    if not callable(getattr(repo, "update_committed", None)):
                        raise MemoryCommitError("atomic fact correction requires a transactional repository")
                    proposed = self._copy_item(existing)
                    proposed.content = content
                    proposed.embedding = list(emb or [])
                    proposed.importance = float(importance)
                    committed = self._repo_call("update_committed", existing.id, content, importance,
                        list(emb or []), expected_version=existing.version)
                    self.items[idx] = self._item_from_committed(committed, proposed)
                    return False

        if emb:
            with self._lock:
                best_idx = -1
                best_sim = -1.0
                for idx, existing in enumerate(self.items):
                    if existing.status != "active":
                        continue
                    existing_keys = {tag for tag in existing.tags if tag.startswith("factkey:")}
                    if fact_keys or existing_keys:
                        continue  # Distinct factual slots must never merge by cosine alone.
                    if not existing.embedding or len(existing.embedding) != len(emb):
                        continue
                    sim = self._cosine_similarity(emb, existing.embedding)
                    if sim > best_sim:
                        best_sim = sim
                        best_idx = idx
                if best_idx >= 0 and best_sim >= dedup_threshold:
                    target = self.items[best_idx]
                    if target.id is None:
                        raise MemoryCommitError(
                            "memorytx: cannot update an uncommitted cached memory"
                        )
                    proposed = self._copy_item(target)
                    proposed.importance = max(proposed.importance, float(importance))
                    if tags:
                        proposed.tags = list(
                            dict.fromkeys(
                                tag for tag in [*proposed.tags, *tags] if tag
                            )
                        )
                    if category and (proposed.category == "" or proposed.category == "general"):
                        proposed.category = category
                    if slot_hint and proposed.slot_hint == "":
                        proposed.slot_hint = slot_hint
                    proposed.last_accessed = time.time()

                    # Keep the cache unchanged while the authoritative row and
                    # its outbox events are being committed.  The version CAS
                    # prevents a stale local candidate from overwriting a
                    # concurrent writer.
                    committed = self._commit_classified_update(
                        proposed, expected_version=int(target.version or 1)
                    )
                    self.items[best_idx] = committed

                else:
                    committed = None
            if committed is not None:
                if self.graph_memory is not None:
                    try:
                        self.graph_memory.update_node(committed)
                    except Exception as e:
                        logger.warning("⚠️  graph_memory.update_node 失败: %s", e)
                _publish_event(self.inf, "memory.longterm.update", {
                    "id": committed.id,
                    "importance": committed.importance,
                    "category": committed.category,
                    "tags": committed.tags,
                    "slot_hint": committed.slot_hint,
                    "version": committed.version,
                    "content_hash": committed.content_hash,
                    "reason": "classified_dedup",
                })
                return False

        now_ts = time.time()
        new_item = Item(
            content=content,
            importance=importance,
            embedding=list(emb) if emb else None,
            id=None,
            created_at=now_ts,
            last_accessed=now_ts,
            category=category if category else "general",
            tags=tags,
            slot_hint=slot_hint,
            score=0.0,
        )
        committed = self._commit_create(new_item)
        with self._lock:
            prior = [self._copy_item(existing) for existing in self.items]
            self.items.append(committed)
            self._items_since_last += 1
            self._next_id = max(self._next_id, int(committed.id) + 1)

        if self.graph_memory is not None:
            try:
                self.graph_memory.add_to_graph(committed, neighbors=prior[-50:])
            except Exception as e:
                logger.warning("⚠️  graph_memory.add_to_graph 失败: %s", e)
        _publish_event(self.inf, "memory.longterm.add", {
            "id": committed.id,
            "content": content,
            "importance": importance,
            "category": committed.category,
            "tags": committed.tags,
            "slot_hint": committed.slot_hint,
            "version": committed.version,
            "content_hash": committed.content_hash,
            "reason": "classified",
        })
        return True

    def _repo_call(self, method_name: str, *args, **kwargs):
        """Pass user_id to modern repositories while keeping test doubles compatible."""
        method = getattr(self.inf.repo.ltm, method_name)
        try:
            parameters = inspect.signature(method).parameters.values()
            supports_user = any(
                parameter.name == "user_id" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_user = False
        if supports_user:
            kwargs["user_id"] = self.user_id
        return method(*args, **kwargs)

    def recall(self, query: str, top_k: int = 3) -> List[Item]:
        active = [item for item in self.items if item.status == "active"]
        if not active or top_k <= 0:
            return []

        query_emb = None
        if self._embed_fn:
            try:
                query_emb = self._embed_fn(query)
            except Exception as e:
                logger.warning("⚠️  查询向量化失败: %s", e)
                query_emb = None

        if query_emb:
            from .fast_vector_index import FastVectorIndex
            top_scored = FastVectorIndex.search_top_k(
                query_emb,
                active,
                top_k=top_k,
                threshold=0.4,
                semantic_weight=0.7,
                importance_weight=0.3,
            )
            return [self._copy_item(item, score=score) for item, score in top_scored]

        # 向量模型不可用时安全降级：采用词法重合度过滤，严禁无脑截取无关早期历史
        q_tokens = set(_tokenize_zh(query))
        if not q_tokens:
            return []

        scored: List[tuple] = []
        for item in active:
            item_tokens = set(_tokenize_zh(item.content))
            overlap = len(q_tokens & item_tokens)
            if overlap > 0:
                sim = overlap / max(len(q_tokens | item_tokens), 1)
                score = sim * 0.7 + item.importance * 0.3
                if score >= 0.4:
                    scored.append((item, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [self._copy_item(item, score=score) for item, score in scored[:top_k]]

    def recall_by_filter(
        self,
        query: str,
        query_embedding: Optional[List[float]],
        filter,
    ) -> List[Item]:
        """带 filter 的语义召回（对齐 main 分支 LongTerm.RecallByFilter）。

        参数 ``filter`` 走 duck typing（需具备 categories / require_tags /
        max_age_hours / min_score / top_k 字段），既兼容 ``memory.RecallFilter``
        也兼容 ``promptctx.RecallFilter``。

        过滤顺序：categories（命中其一）→ require_tags（必须全部包含）→
        max_age_hours（按 created_at 计算）→ 计算 sim → score = sim*0.7 +
        importance*0.3，>= threshold 时回写 ``last_accessed`` 并加入候选；
        最后按 score desc 排序，可选按 top_k 截断。
        """
        with self._lock:
            active = [item for item in self.items if item.status == "active"]
            if not active:
                return []

            min_score = float(getattr(filter, "min_score", 0.0) or 0.0)
            threshold = min_score if min_score > 0 else 0.4
            categories = list(getattr(filter, "categories", []) or [])
            require_tags = list(getattr(filter, "require_tags", []) or [])
            max_age_hours = int(getattr(filter, "max_age_hours", 0) or 0)
            top_k = int(getattr(filter, "top_k", 0) or 0)

            cat_set = set(categories) if categories else None
            now = time.time()
            use_tf = not query_embedding

            # 当走 TF fallback 时，预先分词 query 用于复用
            query_tokens: Optional[List[str]] = None
            if use_tf:
                query_tokens = _tokenize_zh(query)

            candidates: List[Item] = []
            for item in active:
                # categories：命中其一
                if cat_set is not None:
                    item_cat = item.category or "general"
                    if item_cat not in cat_set:
                        continue
                # require_tags：必须全部包含
                if require_tags:
                    item_tags = set(item.tags or [])
                    if not all(t in item_tags for t in require_tags):
                        continue
                # max_age_hours：按 created_at 计算
                if max_age_hours > 0:
                    age_hours = (now - item.created_at) / 3600.0
                    if age_hours > float(max_age_hours):
                        continue

                # 计算 sim：emb 缺失或维度不匹配 → TF fallback
                use_tf_for_item = use_tf or (
                    not item.embedding
                    or (query_embedding is not None
                        and len(query_embedding) != len(item.embedding))
                )
                if use_tf_for_item:
                    sim = self._tf_cosine(query_tokens, item.content)
                else:
                    sim = self._cosine_similarity(query_embedding, item.embedding)

                score = sim * 0.7 + item.importance * 0.3
                if score < threshold:
                    continue

                # 命中：回写 last_accessed，并产出 Item 副本（设置 score 字段）
                item.last_accessed = now
                candidates.append(self._copy_item(item, score=score))

            candidates.sort(key=lambda it: it.score, reverse=True)
            if top_k > 0 and len(candidates) > top_k:
                candidates = candidates[:top_k]
            return candidates

    def _tf_cosine(self, query_tokens: Optional[List[str]], content: str) -> float:
        """TF 词袋 cosine：复用 _tokenize_zh 切词后做 TF cosine。"""
        if query_tokens is None:
            query_tokens = _tokenize_zh("")
        item_tokens = _tokenize_zh(content)
        if not query_tokens or not item_tokens:
            return 0.0
        vocab: Dict[str, int] = {}
        for t in query_tokens:
            if t not in vocab:
                vocab[t] = len(vocab)
        for t in item_tokens:
            if t not in vocab:
                vocab[t] = len(vocab)
        size = len(vocab)
        va = [0.0] * size
        vb = [0.0] * size
        for t, c in Counter(query_tokens).items():
            va[vocab[t]] = float(c)
        for t, c in Counter(item_tokens).items():
            vb[vocab[t]] = float(c)
        return self._cosine_similarity(va, vb)

    def filter_by_category(self, categories: List[str], limit: int) -> List[Item]:
        """按 category 严格过滤 LTM 条目（与 main FilterByCategory 对齐）。

        空 items 或空 categories 直接返回 []；命中即追加，limit > 0 时按命中顺序截断。
        """
        with self._lock:
            if not self.items or not categories:
                return []
            cats = set(categories)
            result: List[Item] = []
            for it in self.items:
                if it.status == "active" and it.category in cats:
                    result.append(self._copy_item(it))
                    if limit > 0 and len(result) >= limit:
                            break
            return result

    def mark_superseded(self, old_ids: List[int], new_id: int) -> List[int]:
        """把被新事实取代的旧长期记忆标记为 superseded。

        这一步不物理删除旧记忆，方便审计和后续同步；默认 recall / filter
        只返回 active 条目，所以已取代事实不会继续进入提示词。
        """
        repo = getattr(getattr(self.inf, "repo", None), "ltm", None)
        if repo is not None and hasattr(repo, "mark_superseded_committed"):
            old_set = {int(value) for value in old_ids if value is not None}
            old_set.discard(int(new_id))
            if not old_set:
                return []
            with self._lock:
                by_id = {
                    int(item.id): self._copy_item(item)
                    for item in self.items
                    if item.id is not None
                }
                marked = sorted(
                    item_id
                    for item_id in old_set
                    if item_id in by_id and by_id[item_id].status != "superseded"
                )
                if not marked:
                    return []
                if int(new_id) not in by_id:
                    raise MemoryCommitError("memorytx: superseding memory is not cached")
                expected_versions = {
                    item_id: max(1, int(by_id[item_id].version or 1))
                    for item_id in [*marked, int(new_id)]
                }
                changes = self._repo_call(
                    "mark_superseded_committed",
                    marked,
                    int(new_id),
                    expected_versions=expected_versions,
                )
                if not isinstance(changes, CommittedChangeSet):
                    raise MemoryCommitError(
                        "memorytx: invalid committed supersession result"
                    )
                try:
                    self.apply_committed(changes)
                except MemoryCommitError:
                    self.load_from_storage(strict=True)
                # superseded_by is a compatibility/UI reverse pointer and is
                # not in the portable Go projection record/hash.
                for item in self.items:
                    if item.id in marked:
                        item.superseded_by = int(new_id)
            return marked
        if repo is not None and (
            hasattr(repo, "set_status_committed") or hasattr(repo, "set_status")
        ):
            return self.set_status_committed(
                old_ids,
                "superseded",
                superseded_by=int(new_id) if new_id is not None else None,
            )

        # Compatibility for isolated domain tests without a repository.  Real
        # application repositories always take the transactional branch above.
        with self._lock:
            marked: List[int] = []
            old_set = {int(x) for x in old_ids if x is not None}
            now = time.time()
            for item in self.items:
                if item.id in old_set and item.status == "active":
                    item.status = "superseded"
                    item.superseded_by = int(new_id) if new_id is not None else None
                    item.superseded_at = now
                    marked.append(int(item.id))
            if new_id is not None and marked:
                for item in self.items:
                    if item.id == int(new_id):
                        item.supersedes = list(
                            dict.fromkeys([*item.supersedes, *marked])
                        )
                        break
            return marked

    def set_status_committed(
        self,
        ids: List[int],
        status: str,
        *,
        reason: str = "",
        superseded_by: Optional[int] = None,
    ) -> List[int]:
        """Persist a status transition before publishing it to the cache."""

        if status not in {"active", "quarantined", "superseded"}:
            raise ValueError(f"unsupported memory status: {status}")
        wanted = {int(value) for value in ids if value is not None}
        if not wanted:
            return []
        with self._lock:
            indexed = {
                int(item.id): (index, self._copy_item(item))
                for index, item in enumerate(self.items)
                if item.id is not None and int(item.id) in wanted and item.status != status
            }
            if not indexed:
                return []
            expected_versions = {
                item_id: int(item.version or 1) for item_id, (_index, item) in indexed.items()
            }
            method_name = (
                "set_status_committed"
                if hasattr(self.inf.repo.ltm, "set_status_committed")
                else "set_status"
            )
            kwargs = {
                "reason": reason,
                "superseded_by": superseded_by,
            }
            if method_name == "set_status_committed":
                kwargs["expected_versions"] = expected_versions
            result = self._repo_call(method_name, sorted(indexed), status, **kwargs)
            committed_by_id: Dict[int, Item] = {}
            if result is not None:
                for value in result:
                    fallback = indexed[int(getattr(value, "memory_id", getattr(value, "id", -1)))][1]
                    committed = self._item_from_committed(value, fallback)
                    committed.status = status
                    committed.quarantine_reason = reason or ""
                    committed.superseded_by = superseded_by
                    committed.superseded_at = (
                        committed.superseded_at or time.time()
                        if status == "superseded"
                        else None
                    )
                    committed_by_id[int(committed.id)] = committed
            else:
                # Legacy repository: a normal return still means the call
                # completed.  Synthesize only after that return, never before.
                for item_id, (_index, fallback) in indexed.items():
                    fallback.status = status
                    fallback.quarantine_reason = reason or ""
                    fallback.superseded_by = superseded_by
                    fallback.superseded_at = time.time() if status == "superseded" else None
                    fallback.version = expected_versions[item_id] + 1
                    committed_by_id[item_id] = fallback
            if set(committed_by_id) != set(indexed):
                raise MemoryCommitError("memorytx: status commit returned an incomplete change set")
            for item_id, (index, _fallback) in indexed.items():
                self.items[index] = committed_by_id[item_id]
        return sorted(committed_by_id)

    def active_items(self) -> List[Item]:
        """返回 active 状态的长期记忆副本。"""
        with self._lock:
            return [self._copy_item(item) for item in self.items if item.status == "active"]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def need_consolidation(self) -> bool:
        with self._lock:
            return self._items_since_last >= max(1, self.cfg.memory_consolidation_trigger)

    # ─── main 分支访问器对齐 ───────────────────────────────────────────────

    def snapshot(self) -> List["Item"]:
        """返回 self.items 的浅拷贝（值拷贝 Item，但 embedding/tags 为引用）。

        与 main 分支 LongTerm.Snapshot 对齐：调用方可安全遍历，但不应修改
        Item 内嵌的 list 字段。
        """
        with self._lock:
            return [self._copy_item(it) for it in self.items]

    def find_by_id(self, item_id: int):
        """按 id 线性查找；命中返回 (Item 值拷贝, True)，否则 (None, False)。"""
        with self._lock:
            for it in self.items:
                if it.id == item_id:
                    return (self._copy_item(it), True)
        return None, False

    def last_id(self) -> int:
        """返回最后一条 item 的 id；空返回 -1（与 main 一致）。"""
        with self._lock:
            if not self.items:
                return -1
            last = self.items[-1]
            return -1 if last.id is None else int(last.id)

    def last_item(self):
        """返回 (last item 值拷贝, True)；空返回 (None, False)。"""
        with self._lock:
            if not self.items:
                return None, False
            it = self.items[-1]
            return (self._copy_item(it), True)

    def sync_last_item_pg_id(self, pg_id: int) -> None:
        """把最后一条 item 的 id 改写为 PG 真实主键，并推高 _next_id。

        对齐 main 分支：写入 PG 后 RETURNING 拿到的真实 id 通过此方法回写到内存
        item 上，保证后续图同步用的 mem_id 与 PG 一致。
        """
        if pg_id <= 0:
            return
        with self._lock:
            if not self.items:
                return
            self.items[-1].id = int(pg_id)
            if pg_id + 1 > self._next_id:
                self._next_id = pg_id + 1

    def set_consolidation_config(self, cfg) -> None:
        """覆盖 consolidate 用到的配置对象（duck-typed）。

        cfg 需具备：``memory_consolidation_similarity / memory_consolidation_dedup /
        memory_consolidation_ttl_days / memory_consolidation_decay_rate /
        memory_consolidation_min_import / memory_consolidation_trigger`` 字段。
        与 main 分支 SetConsolidationConfig 一致：替换内部引用即可生效。
        """
        if cfg is None:
            return
        with self._lock:
            self.cfg = cfg

    def _record_from_item(self, item: Item) -> MemoryRecord:
        record = MemoryRecord(
            memory_id=int(item.id or 0),
            user_id=self.user_id,
            content=item.content,
            importance=float(item.importance),
            embedding=list(item.embedding or []),
            category=item.category or "general",
            tags=list(item.tags or []),
            slot_hint=item.slot_hint or "",
            version=max(1, int(item.version or 1)),
            content_hash=item.content_hash or "",
            created_at=float(item.created_at or 0.0),
            updated_at=float(item.updated_at or 0.0),
            last_accessed=float(item.last_accessed or 0.0),
            deleted_at=item.deleted_at,
            quarantined=item.status == "quarantined",
            quarantine_reason=item.quarantine_reason or "",
            superseded=item.status == "superseded",
            superseded_at=item.superseded_at,
            supersedes=list(item.supersedes or []),
        )
        record.content_hash = compute_content_hash(record)
        return record

    def plan_consolidation(self, now: Optional[float] = None) -> ConsolidationPlan:
        """Build a version-CAS plan without mutating the active cache."""

        now = float(now if now is not None else time.time())
        with self._lock:
            items = [self._copy_item(item) for item in self.items]
        if len(items) <= 1:
            return ConsolidationPlan()

        decay_rate = float(getattr(self.cfg, "memory_consolidation_decay_rate", 0.99) or 0.99)
        similarity = float(getattr(self.cfg, "memory_consolidation_similarity", 0.85) or 0.85)
        dedup = float(getattr(self.cfg, "memory_consolidation_dedup", 0.95) or 0.95)
        ttl_days = int(getattr(self.cfg, "memory_consolidation_ttl_days", 30) or 30)
        min_importance = float(getattr(self.cfg, "memory_consolidation_min_import", 0.1) or 0.1)

        removed: Dict[int, str] = {}
        updates: Dict[int, MemoryUpdate] = {}
        for index, item in enumerate(items):
            if item.id is None:
                continue
            days = max(0.0, (now - item.created_at) / 86400.0)
            previous = item.importance
            item.importance = previous * (decay_rate ** days)
            if previous - item.importance >= 0.01:
                updates[int(item.id)] = MemoryUpdate(
                    self._record_from_item(item), max(1, int(item.version or 1))
                )

        for i in range(len(items)):
            if i in removed:
                continue
            for j in range(i + 1, len(items)):
                if j in removed:
                    continue
                left, right = items[i], items[j]
                if any(str(tag).startswith('factkey:') for tag in (*left.tags, *right.tags)):
                    continue  # Atomic facts are corrected at write time, not merged by similarity.
                score = self._compute_similarity(
                    left.content, right.content, left.embedding, right.embedding
                )
                if score >= dedup:
                    remove_index = i if right.importance >= left.importance else j
                    removed[remove_index] = "deduplicated"
                    if items[remove_index].id is not None:
                        updates.pop(int(items[remove_index].id), None)
                    if remove_index == i:
                        break
                elif score >= similarity:
                    survivor, remove_index = (j, i) if right.importance > left.importance else (i, j)
                    merged = self._merge_pair(items[i], items[j], now)
                    merged.id = items[survivor].id
                    merged.version = items[survivor].version
                    items[survivor] = merged
                    removed[remove_index] = "merged"
                    if items[remove_index].id is not None:
                        updates.pop(int(items[remove_index].id), None)
                    if merged.id is not None:
                        updates[int(merged.id)] = MemoryUpdate(
                            self._record_from_item(merged),
                            max(1, int(merged.version or 1)),
                        )
                    if remove_index == i:
                        break

        for index, item in enumerate(items):
            if index in removed:
                continue
            days = max(0.0, (now - item.created_at) / 86400.0)
            if ttl_days > 0 and days > float(ttl_days) and item.importance < min_importance:
                removed[index] = "expired"
                if item.id is not None:
                    updates.pop(int(item.id), None)

        if self.graph_memory is not None and removed:
            candidates = [
                int(items[index].id)
                for index in removed
                if items[index].id is not None
            ]
            try:
                threshold = int(getattr(self.cfg, "graph_protect_indegree", 3) or 3)
                protected = set(
                    self.graph_memory.filter_protected(candidates, threshold) or []
                )
                for index in list(removed):
                    if items[index].id in protected:
                        removed.pop(index, None)
            except Exception as exc:
                logger.warning("⚠️  graph_memory.filter_protected 失败: %s", exc)

        deletes = [
            MemoryDelete(
                memory_id=int(items[index].id),
                user_id=self.user_id,
                expected_version=max(1, int(items[index].version or 1)),
                reason=reason,
            )
            for index, reason in removed.items()
            if items[index].id is not None
        ]
        return ConsolidationPlan(
            updates=sorted(updates.values(), key=lambda value: value.record.memory_id),
            deletes=sorted(deletes, key=lambda value: value.memory_id),
        )

    def apply_committed(self, changes: CommittedChangeSet) -> None:
        """Atomically apply already-committed authoritative rows to the cache."""

        upserts = list(getattr(changes, "upserts", []) or [])
        deletes = list(getattr(changes, "deletes", []) or [])
        if any(record.user_id != self.user_id for record in [*upserts, *deletes]):
            raise MemoryCommitError("memorytx: committed change set crossed tenant boundary")
        with self._lock:
            next_items = [self._copy_item(item) for item in self.items]
            by_id = {
                int(item.id): index
                for index, item in enumerate(next_items)
                if item.id is not None
            }
            for record in upserts:
                index = by_id.get(int(record.memory_id))
                if index is None or int(next_items[index].version or 1) != record.version - 1:
                    raise MemoryCommitError("memorytx: cache version changed after commit")
                next_items[index] = self._item_from_committed(record, next_items[index])
            deleted_ids = set()
            for record in deletes:
                index = by_id.get(int(record.memory_id))
                if index is None or int(next_items[index].version or 1) != record.version - 1:
                    raise MemoryCommitError("memorytx: cache version changed after commit")
                deleted_ids.add(int(record.memory_id))
            self.items = [
                item for item in next_items
                if item.id is None or int(item.id) not in deleted_ids
            ]
            self._next_id = max(
                [int(item.id) + 1 for item in self.items if item.id is not None] or [0]
            )

    def _sync_graph_after_commit(self, changes: CommittedChangeSet) -> None:
        """Commit 已落库后同步图层：删除被合并/淘汰的节点，更新存续节点。

        与 main GraphAwareConsolidate 的图同步语义对齐。失败只告警，
        不影响已提交的 PG 状态（图层可由 bulk_index 全量重建兜底）。
        """
        graph = self.graph_memory
        if graph is None:
            return
        try:
            for record in list(getattr(changes, "deletes", []) or []):
                graph.delete_from_graph(int(record.memory_id))
            for record in list(getattr(changes, "upserts", []) or []):
                graph.update_node(
                    self._item_from_committed(record, Item(content=str(getattr(record, "content", "") or "")))
                )
        except Exception as exc:
            logger.warning("⚠️  graph_memory consolidate 同步失败: %s", exc)

    def consolidate_committed(self) -> ConsolidationResult:
        """Persist one consolidation plan atomically, then update the cache."""

        plan = self.plan_consolidation()
        if not plan.updates and not plan.deletes:
            return ConsolidationResult()
        repo = getattr(getattr(self.inf, "repo", None), "ltm", None)
        if repo is None or not hasattr(repo, "apply_consolidation_committed"):
            raise MemoryCommitError("memorytx: consolidation repository unavailable")
        changes = self._repo_call("apply_consolidation_committed", plan)
        if not isinstance(changes, CommittedChangeSet):
            raise MemoryCommitError("memorytx: invalid committed consolidation result")
        try:
            self.apply_committed(changes)
        except MemoryCommitError:
            # PostgreSQL already committed.  Reloading is the only safe answer
            # when another local mutation raced cache application.
            self.load_from_storage(strict=True)
        # 提交后同步图层：删除被合并/淘汰的节点，更新存续节点。
        # plan_consolidation 已做中心度保护，这里的删除集与 PG 删除集一致。
        self._sync_graph_after_commit(changes)
        with self._lock:
            self._last_consolidate_ts = time.time()
            self._items_since_last = 0
        result = ConsolidationResult(
            deduped=sum(entry.reason == "deduplicated" for entry in plan.deletes),
            merged=sum(entry.reason == "merged" for entry in plan.deletes),
            expired=sum(entry.reason == "expired" for entry in plan.deletes),
            delete_from_db=[entry.memory_id for entry in plan.deletes],
            update_in_db=[
                self._item_from_committed(update.record, Item(content=update.record.content))
                for update in plan.updates
            ],
        )
        _publish_event(self.inf, "memory.consolidate", {
            "deduped": result.deduped,
            "merged": result.merged,
            "expired": result.expired,
            "delete_from_db": result.delete_from_db,
            "update_count": len(result.update_in_db),
            "committed": True,
        })
        return result

    def consolidate(self) -> ConsolidationResult:
        """周期性合并：阶段 1 衰减 → 阶段 2 去重/合并 → 阶段 3 双条件淘汰。

        与 main 分支 Go LongTerm.Consolidate 严格对齐：
          - 衰减按每条 item 自己的 created_at 而非全局 elapsed_days
          - 去重保留 i、删除 j（吸收 importance 取 max 和 tags 合并）
          - 合并产出新 item 替换 i，j 删除
          - 淘汰需同时满足 days > ttl_days 且 importance < min_importance

        本方法只改内存 self.items，不写 PG（持久化由 Task 20 的
        sync_consolidation_to_db 处理）。返回 ConsolidationResult，
        delete_from_db 含被去重和合并删除的 j.id；update_in_db 含被替换为
        merged 后的 i 副本。
        """
        result = ConsolidationResult()
        with self._lock:
            if len(self.items) <= 1:
                return result

            self._last_consolidate_ts = time.time()
            self._items_since_last = 0

            decay_rate = float(getattr(self.cfg, "memory_consolidation_decay_rate", 0.99) or 0.99)
            sim_threshold = float(getattr(self.cfg, "memory_consolidation_similarity", 0.85) or 0.85)
            dedup_threshold = float(getattr(self.cfg, "memory_consolidation_dedup", 0.95) or 0.95)
            ttl_days = int(getattr(self.cfg, "memory_consolidation_ttl_days", 30) or 30)
            min_importance = float(getattr(self.cfg, "memory_consolidation_min_import", 0.1) or 0.1)

            now = time.time()

            # 阶段 1：按条目 created_at 单独指数衰减
            for item in self.items:
                days = max(0.0, (now - item.created_at) / 86400.0)
                item.importance *= decay_rate ** days

            # 阶段 2：两两比对 dedup + merge
            removed = [False] * len(self.items)
            for i in range(len(self.items)):
                if removed[i]:
                    continue
                for j in range(i + 1, len(self.items)):
                    if removed[j]:
                        continue
                    item_i = self.items[i]
                    item_j = self.items[j]
                    if any(str(tag).startswith('factkey:') for tag in (*item_i.tags, *item_j.tags)):
                        continue
                    sim = self._compute_similarity(
                        item_i.content,
                        item_j.content,
                        item_i.embedding,
                        item_j.embedding,
                    )
                    if sim >= dedup_threshold:
                        # 去重：保留 i，删除 j；i 吸收 j 的 importance 与 tags
                        item_i.importance = max(item_i.importance, item_j.importance)
                        item_i.tags = list(dict.fromkeys(list(item_i.tags) + list(item_j.tags)))
                        item_i.last_accessed = now
                        removed[j] = True
                        result.deduped += 1
                        if item_j.id is not None:
                            result.delete_from_db.append(item_j.id)
                    elif sim >= sim_threshold:
                        # 合并：把 i 与 j 合成一条，替换 i，j 删除
                        merged = self._merge_pair(item_i, item_j, now)
                        self.items[i] = merged
                        removed[j] = True
                        result.merged += 1
                        if item_j.id is not None:
                            result.delete_from_db.append(item_j.id)
                        result.update_in_db.append(merged)

            # 阶段 3：双条件淘汰（days > ttl_days 且 importance < min_importance）
            for idx in range(len(self.items)):
                if removed[idx]:
                    continue
                item = self.items[idx]
                days = max(0.0, (now - item.created_at) / 86400.0)
                if ttl_days > 0 and days > float(ttl_days) and item.importance < min_importance:
                    removed[idx] = True
                    result.expired += 1
                    if item.id is not None:
                        result.delete_from_db.append(item.id)

            self.items = [it for k, it in enumerate(self.items) if not removed[k]]

            # 图中心度保护：入度 ≥ threshold 的节点不进入 PG 删除列表（与 main
            # GraphAwareConsolidate 对齐；只过滤 delete_from_db，不复活内存条目）。
            if self.graph_memory is not None and result.delete_from_db:
                try:
                    threshold = int(getattr(self.cfg, "graph_protect_indegree", 3) or 3)
                    protected = self.graph_memory.filter_protected(
                        list(result.delete_from_db), threshold
                    ) or []
                    if protected:
                        protected_set = set(protected)
                        before = len(result.delete_from_db)
                        result.delete_from_db = [
                            rid for rid in result.delete_from_db if rid not in protected_set
                        ]
                        logger.info(
                            "🛡️  图中心度保护：%d 条记忆免于 PG 删除（入度≥%d）",
                            before - len(result.delete_from_db), threshold,
                        )
                except Exception as e:
                    logger.warning("⚠️  graph_memory.filter_protected 失败: %s", e)

            # 图增强记忆 hook：被淘汰 / 被合并删除的条目同步从图删除
            if self.graph_memory is not None:
                try:
                    for rid in result.delete_from_db:
                        self.graph_memory.delete_from_graph(rid)
                    for it in self.items:
                        self.graph_memory.update_node(it)
                except Exception as e:
                    logger.warning("⚠️  graph_memory consolidate hook 失败: %s", e)

        logger.info(
            "✅ 记忆合并完成 deduped=%d merged=%d expired=%d 剩余 %d 条",
            result.deduped, result.merged, result.expired, len(self.items),
        )
        _publish_event(self.inf, "memory.consolidate", {
            "deduped": result.deduped,
            "merged": result.merged,
            "expired": result.expired,
            "delete_from_db": list(result.delete_from_db),
            "update_count": len(result.update_in_db),
            "remaining": len(self.items),
        })
        return result

    def _merge_pair(self, item_i: Item, item_j: Item, now: float) -> Item:
        """Pure Go-parity merge retaining the stronger record's identity."""

        base, other = (
            (item_j, item_i)
            if item_j.importance > item_i.importance
            else (item_i, item_j)
        )
        if other.content not in base.content and base.content not in other.content:
            content = f"{base.content}；{other.content}"
        elif len(other.content) > len(base.content):
            content = other.content
        else:
            content = base.content

        emb: Optional[List[float]] = None
        if (
            base.embedding
            and other.embedding
            and len(base.embedding) == len(other.embedding)
        ):
            wi = base.importance
            wj = other.importance
            total = wi + wj
            if total > 0:
                emb = [
                    (base.embedding[k] * wi + other.embedding[k] * wj) / total
                    for k in range(len(base.embedding))
                ]
            else:
                emb = list(base.embedding)
        elif base.embedding:
            emb = list(base.embedding)
        elif other.embedding:
            emb = list(other.embedding)

        tags = list(dict.fromkeys([*base.tags, *other.tags]))
        supersedes = list(
            dict.fromkeys(
                [*base.supersedes, *([int(other.id)] if other.id is not None else []), *other.supersedes]
            )
        )

        return Item(
            content=content,
            importance=max(base.importance, other.importance),
            embedding=emb,
            id=base.id,
            created_at=base.created_at,
            last_accessed=now,
            category=base.category,
            tags=tags,
            slot_hint=base.slot_hint,
            score=base.score,
            status=base.status,
            superseded_by=base.superseded_by,
            superseded_at=base.superseded_at,
            supersedes=supersedes,
            quarantine_reason=base.quarantine_reason,
            version=base.version,
            content_hash=base.content_hash,
            updated_at=base.updated_at,
            deleted_at=base.deleted_at,
        )

    def _compute_similarity(
        self,
        a: str,
        b: str,
        emb_a: Optional[List[float]] = None,
        emb_b: Optional[List[float]] = None,
    ) -> float:
        """中英文 cosine 相似度。

        优先使用 embedding 余弦：当 emb_a / emb_b 都非空且维度一致时，直接计算；
        否则降级为 TF 词袋（按 _tokenize_zh 切词）后做 cosine。与 main 分支 Go
        实现 itemSimilarity / Cosine 对齐。
        """
        if emb_a and emb_b and len(emb_a) == len(emb_b):
            return self._cosine_similarity(emb_a, emb_b)

        tokens_a = _tokenize_zh(a)
        tokens_b = _tokenize_zh(b)
        if not tokens_a or not tokens_b:
            return 0.0
        vocab: Dict[str, int] = {}
        for t in tokens_a:
            if t not in vocab:
                vocab[t] = len(vocab)
        for t in tokens_b:
            if t not in vocab:
                vocab[t] = len(vocab)
        size = len(vocab)
        va = [0.0] * size
        vb = [0.0] * size
        for t, c in Counter(tokens_a).items():
            va[vocab[t]] = float(c)
        for t, c in Counter(tokens_b).items():
            vb[vocab[t]] = float(c)
        return self._cosine_similarity(va, vb)


class Preference:
    """已迁移至 ``internal.memory.preference.Preference``；本别名仅为向后兼容。

    未来将逐步把外部 import 切到 preference 子模块，最终移除此别名。
    """

    def __new__(cls, *args, **kwargs):
        from internal.memory.preference import Preference as _Pref
        return _Pref(*args, **kwargs)


class MemoryManager:
    """三层记忆 + 可选 GraphMemory 的统一容器。

    现有 ShortTerm/LongTerm/Preference 行为完全保留；GraphMemory 作为可选注入点：
      - 存入新长期记忆 → 自动调用 graph_memory.add_to_graph(item)
      - 删除/更新长期记忆（consolidate）→ 自动调用 delete_from_graph / update_node

    所有 hook 实际位于 LongTerm 内部；MemoryManager 负责装配并把 graph_memory
    注入到 LongTerm 上。Neo4j 不可用或未注入时，所有 hook 都是 no-op。
    """

    def __init__(
        self,
        cfg: APIConfig,
        inf: Infrastructure,
        user_id: str = "default_user",
        graph_memory: Optional["GraphMemory"] = None,
    ):
        self.cfg = cfg
        self.inf = inf
        self.short_term = ShortTerm(cfg.short_term_max_turns)
        self.long_term = LongTerm(cfg, inf, user_id=user_id)
        self.preference = Preference(user_id, inf)
        # 可选注入点：可以构造时传入，也可以后续 set_graph_memory() 注入
        self.graph_memory: Optional["GraphMemory"] = graph_memory
        if graph_memory is not None:
            self.long_term.set_graph_memory(graph_memory)

    def set_graph_memory(self, graph_memory: Optional["GraphMemory"]) -> None:
        """挂载 / 解除 GraphMemory；同步透传到 LongTerm。"""
        self.graph_memory = graph_memory
        self.long_term.set_graph_memory(graph_memory)

    # ── 长期记忆操作的薄包装（保持调用方一致；hook 已位于 LongTerm 内部）──

    def add_long_term(self, content: str, importance: float = 0.5) -> None:
        """存入新长期记忆；命中 LongTerm.add hook 自动同步图。"""
        self.long_term.add(content, importance)

    def consolidate(self) -> None:
        """触发长期记忆 consolidate；命中 LongTerm.consolidate hook 自动同步图。"""
        self.long_term.consolidate()

    def recall(
        self,
        query: str,
        top_k: int = 3,
        query_embedding: Optional[List[float]] = None,
        categories: Optional[List[str]] = None,
    ) -> List[Item]:
        """语义召回（与 main GraphMemory.RecallByFilter 对齐）。

        流程：
          1) 走 ``LongTerm.recall_by_filter`` 拿种子（带 score = sim*0.7 + imp*0.3）。
          2) 若挂载 graph_memory：对种子 id 做 1-hop ``find_related`` 扩展。
          3) 在 ``LongTerm.items`` 中查到扩展条目，命中 categories 过滤后 ``score=0.45``。
          4) 种子 + 扩展按 score desc 排序，截 top_k。

        ``query_embedding`` 由调用方提供；为 None 时尝试用 ``LongTerm._embed_fn`` 计算，
        失败则走 TF fallback。
        """
        if not self.long_term.items:
            return []

        if query_embedding is None and self.long_term._embed_fn is not None:
            try:
                query_embedding = self.long_term._embed_fn(query)
            except Exception as e:
                logger.warning("⚠️  recall 查询向量化失败: %s", e)
                query_embedding = None

        rfilter = RecallFilter(
            categories=list(categories or []),
            top_k=top_k,
            min_score=0.4,
        )
        seed = self.long_term.recall_by_filter(query, query_embedding, rfilter)
        if not self.graph_memory or not seed:
            return seed

        seed_id_set = {it.id for it in seed if it.id is not None}
        if not seed_id_set:
            return seed

        try:
            expanded_ids: set = set()
            for sid in seed_id_set:
                expanded_ids.update(self.graph_memory.find_related(sid) or [])
        except Exception as e:
            logger.warning("⚠️  graph_memory.find_related 失败: %s", e)
            return seed

        cat_set = set(rfilter.categories) if rfilter.categories else None
        extras: List[Item] = []
        for it in self.long_term.items:
            if it.id is None or it.id not in expanded_ids or it.id in seed_id_set:
                continue
            if it.status != "active":
                continue
            if cat_set is not None and it.category not in cat_set:
                continue
            extras.append(self.long_term._copy_item(it, score=0.45))

        all_items = list(seed) + extras
        all_items.sort(key=lambda x: x.score, reverse=True)
        if top_k > 0 and len(all_items) > top_k:
            all_items = all_items[:top_k]
        return all_items

    def need_consolidation(self) -> bool:
        return self.long_term.need_consolidation()
