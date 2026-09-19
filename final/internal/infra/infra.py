# infra — 管理所有外部基础设施连接：Milvus / PostgreSQL / Elasticsearch / Kafka
# 每个连接失败时优雅降级，不影响应用启动。
#
# 业务持久化逻辑统一收敛到 internal.repo 包：Infrastructure 仅负责连接生命周期
# (connect / schema bootstrap / health) 与跨域装配 self.repo 仓储入口。
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Tuple

from config.config import APIConfig
from internal.resilience.circuit_breaker import CircuitBreaker
from internal.repo import (
    chathistory,
    documentrepo,
    eventbus,
    longterm,
    memory_projection,
    preference,
    ragchunk,
    snapshot,
)

logger = logging.getLogger(__name__)

def _guarded(breaker: CircuitBreaker, degraded, call, *, label: str):
    """熔断门禁内的远程调用：打开时快速返回降级结果，异常计入熔断。"""
    if not breaker.allow_request():
        logger.warning("⚠️  熔断打开，%s 调用直接降级", label)
        return degraded
    try:
        result = call()
    except Exception as e:
        breaker.record_failure()
        logger.warning("⚠️  %s 调用失败: %s", label, e)
        return degraded
    breaker.record_success()
    return result


# 尝试导入可选依赖，失败则标记不可用
try:
    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
    _HAS_PG = True
except ImportError:
    ThreadedConnectionPool = None  # type: ignore
    _HAS_PG = False

try:
    from elasticsearch import Elasticsearch
    _HAS_ES = True
except ImportError:
    _HAS_ES = False

try:
    from kafka import KafkaProducer
    _HAS_KAFKA = True
except ImportError:
    _HAS_KAFKA = False

try:
    from pymilvus import DataType, MilvusClient
    _HAS_MILVUS = True
except ImportError:
    DataType = None  # type: ignore
    _HAS_MILVUS = False


# 默认 RAG 集合名（与 main 分支 Go 实现 internal/infrastructure/persistence/ragchunk 对齐）
RAG_COLLECTION = "rag_chunks"
# 索引参数（与 Go 端 entity.NewIndexIvfFlat(entity.L2, 128) 对齐）
_RAG_INDEX_TYPE = "IVF_FLAT"
_RAG_METRIC_TYPE = "L2"
_RAG_INDEX_NLIST = 128
# content 字段最大长度（与 Go 端 max_length=4096 对齐）
_RAG_CONTENT_MAX_LEN = 4096
_RAG_USER_ID_MAX_LEN = 256


@dataclass
class Status:
    milvus: str = "disconnected"
    postgresql: str = "disconnected"
    elasticsearch: str = "disconnected"
    kafka: str = "disconnected"


@dataclass
class LongTermRow:
    id: int
    content: str
    importance: float
    embedding: Optional[List[float]] = None


# ─────────────────────── repo 适配层 ──────────────────────────────────────
# repo 期望 internal.platform 风格的 client（含 is_real()/query/exec/conn 等）。
# 这里把 Infrastructure 已经持有的 raw 连接句柄包成符合接口的 thin adapter，
# 避免重复 connect。

class _PGAdapter:
    """把 psycopg2 raw connection 包成 PostgresClient-like 接口供 repo 使用。

    单条语句走 bootstrap 连接（autocommit）；多语句事务必须走 ``transaction()``，
    从独立连接池 checkout 专用连接，避免并发线程在同一连接上互踩事务。

    断线韧性：
    - 连接类错误（OperationalError/InterfaceError）视为基础设施故障：
      bootstrap 连接重连一次；池内连接 checkout 时 ping 预检，死连接丢弃换新。
    - 连续失败达到阈值后熔断打开：读路径快速返回空结果，事务路径快速抛错，
      避免数据库宕机期间每个请求都付完整连接超时。SQL 错误不影响熔断计数。
    """

    def __init__(self, conn, pool=None, connect_fn=None):
        self._conn = conn
        self._pool = pool
        self._connect_fn = connect_fn
        self._dead_conn_errors: Tuple[Any, ...] = tuple(
            exc for exc in (
                getattr(psycopg2, "OperationalError", None) if _HAS_PG else None,
                getattr(psycopg2, "InterfaceError", None) if _HAS_PG else None,
            ) if exc is not None
        )
        self._breaker = CircuitBreaker(
            failure_threshold=3, cooldown_seconds=30.0, half_open_max_calls=1,
        )

    def is_real(self) -> bool:
        return self._conn is not None or self._pool is not None

    @property
    def conn(self):
        return self._conn

    def _is_dead_conn_error(self, exc: Exception) -> bool:
        return isinstance(exc, self._dead_conn_errors)

    def _reconnect(self) -> Any:
        """重建 bootstrap 连接并 ping 验证；失败向上抛连接错误。"""
        if self._connect_fn is None:
            raise RuntimeError("postgres reconnect not configured")
        try:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
        except Exception:
            pass
        fresh = self._connect_fn()
        fresh.autocommit = True
        with fresh.cursor() as cur:
            cur.execute("SELECT 1")
        self._conn = fresh
        logger.info("✅ PostgreSQL 连接已恢复")
        return self._conn

    def _execute_read(self, sql: str, params, fetch: str):
        """单语句执行路径：熔断快速失败 → 断线重连一次重试。"""
        if not self._breaker.allow_request():
            logger.warning("⚠️  PG 熔断打开，语句执行直接降级: %.120s", sql)
            return [] if fetch == "all" else None
        attempt = 0
        while True:
            attempt += 1
            conn = self._conn
            if conn is None:
                return [] if fetch == "all" else None
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params or ())
                    if fetch == "all":
                        result = list(cur.fetchall())
                    elif fetch == "one":
                        result = cur.fetchone()
                    else:
                        result = cur.rowcount
                self._breaker.record_success()
                return result
            except Exception as exc:
                if self._is_dead_conn_error(exc):
                    self._breaker.record_failure()
                    if attempt == 1:
                        try:
                            self._reconnect()
                            continue
                        except Exception:
                            return [] if fetch == "all" else None
                else:
                    # SQL 层错误说明数据库本身可达，不计入熔断。
                    self._breaker.record_success()
                logger.warning("⚠️  PG 语句执行失败: %s", exc)
                return [] if fetch == "all" else None

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Tuple[Any, ...]]:
        result = self._execute_read(sql, params, "all")
        return result if isinstance(result, list) else []

    def query_one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[Tuple[Any, ...]]:
        return self._execute_read(sql, params, "one")

    def exec(self, sql: str, params: Optional[Sequence[Any]] = None) -> int:
        result = self._execute_read(sql, params, "rowcount")
        return -1 if result is None else int(result)

    def exec_many(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> int:
        if not self._breaker.allow_request():
            logger.warning("⚠️  PG 熔断打开，批量执行直接降级")
            return -1
        conn = self._conn
        if conn is None:
            return -1
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, list(seq_of_params))
                rowcount = cur.rowcount
            self._breaker.record_success()
            return rowcount
        except Exception as e:
            if self._is_dead_conn_error(e):
                self._breaker.record_failure()
            logger.warning("⚠️  PG exec_many 失败: %s", e)
            return -1

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """专用连接上的一个事务：with 块正常退出提交，异常回滚。

        checkout 后先 ping 预检，死连接丢弃换新重试一次；进入事务体后不再重试
        （contextmanager 语义不允许对已中断的 with 体重新 yield）。
        事务路径绝不静默降级为成功：拿不到健康连接或熔断打开时直接抛错。
        """
        if self._pool is None:
            with self._bootstrap_transaction() as conn:
                yield conn
            return

        if not self._breaker.allow_request():
            raise RuntimeError("postgres circuit open")
        conn = None
        attempt = 0
        while True:
            attempt += 1
            candidate = self._pool.getconn()
            try:
                candidate.autocommit = True
                with candidate.cursor() as cur:
                    cur.execute("SELECT 1")
                conn = candidate
                break
            except Exception as exc:
                try:
                    self._pool.putconn(candidate, close=True)
                except Exception:
                    pass
                if attempt < 2:
                    logger.warning("⚠️  PG 池内死连接已丢弃，重试 checkout: %s", exc)
                    continue
                raise RuntimeError(f"postgres unavailable: {exc}")

        try:
            conn.autocommit = False
            yield conn
            conn.commit()
            self._breaker.record_success()
            self._pool.putconn(conn)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            dead = self._is_dead_conn_error(exc)
            try:
                self._pool.putconn(conn, close=dead)
            except Exception:
                pass
            if dead:
                self._breaker.record_failure()
            else:
                # SQL 层错误说明连接本身可用，不计入熔断。
                self._breaker.record_success()
            raise

    @contextmanager
    def _bootstrap_transaction(self) -> Iterator[Any]:
        conn = self._conn
        if conn is None:
            raise RuntimeError("postgres unavailable")
        if not self._breaker.allow_request():
            raise RuntimeError("postgres circuit open")
        prior_autocommit = conn.autocommit
        try:
            conn.autocommit = False
            yield conn
            conn.commit()
            self._breaker.record_success()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if self._is_dead_conn_error(exc):
                self._breaker.record_failure()
            else:
                self._breaker.record_success()
            raise
        finally:
            conn.autocommit = prior_autocommit


class _ESAdapter:
    """把 raw Elasticsearch 客户端包成 ESClient-like 接口供 repo 使用。"""

    def __init__(self, es):
        self._es = es
        self._breaker = CircuitBreaker(
            failure_threshold=3, cooldown_seconds=30.0, half_open_max_calls=1,
        )

    def is_real(self) -> bool:
        return self._es is not None

    @property
    def client(self):
        return self._es

    def index(self, index: str, doc_id: Any, body: dict) -> bool:
        if self._es is None:
            return False
        def _call():
            self._es.index(index=index, id=doc_id, body=body)
            return True
        return _guarded(self._breaker, False, _call, label="ES index")

    def search(self, index: str, body: dict) -> dict:
        if self._es is None:
            return {}
        def _call():
            return dict(self._es.search(index=index, body=body))
        return _guarded(self._breaker, {}, _call, label="ES search")

    def delete_many(self, index: str, doc_ids: List[Any]) -> None:
        if self._es is None:
            return
        for doc_id in doc_ids:
            def _call(doc_id=doc_id):
                self._es.delete(index=index, id=doc_id)
                return None
            _guarded(self._breaker, None, _call, label=f"ES delete {doc_id}")


class _MilvusAdapter:
    """把 raw MilvusClient 包成 MilvusClientWrapper-like 接口供 repo 使用。"""

    def __init__(self, client):
        self._client = client
        self._breaker = CircuitBreaker(
            failure_threshold=3, cooldown_seconds=30.0, half_open_max_calls=1,
        )

    def is_real(self) -> bool:
        return self._client is not None

    @property
    def client(self):
        return self._client

    def ensure_collection(self, collection_name: str, dimension: int,
                          auto_id: bool = True, enable_dynamic_field: bool = True) -> bool:
        # rag_chunks 集合由 _init_milvus_collections 在启动期建好；这里仅做存在性确认。
        if self._client is None:
            return False
        def _call():
            return bool(self._client.has_collection(collection_name))
        return _guarded(self._breaker, False, _call, label="Milvus ensure_collection")

    def insert(self, collection_name: str, data: List[dict]) -> bool:
        if self._client is None or not data:
            return False
        def _call():
            self._client.insert(collection_name=collection_name, data=data)
            return True
        return _guarded(self._breaker, False, _call, label="Milvus insert")

    def upsert(self, collection_name: str, data: List[dict]) -> bool:
        """幂等写入；兼容没有原生 upsert 的旧版 Milvus client。"""
        if self._client is None or not data:
            return False
        def _call():
            upsert = getattr(self._client, "upsert", None)
            if callable(upsert):
                upsert(collection_name=collection_name, data=data)
                return True
            ids = [int(item["pg_id"]) for item in data if item.get("pg_id") is not None]
            if ids:
                self._client.delete(
                    collection_name=collection_name,
                    filter=f"pg_id in [{', '.join(str(item) for item in ids)}]",
                )
            self._client.insert(collection_name=collection_name, data=data)
            return True
        return _guarded(self._breaker, False, _call, label="Milvus upsert")

    def search(self, collection_name: str, query_emb: List[float], top_k: int,
               output_fields: Optional[List[str]] = None,
               filter_expr: Optional[str] = None) -> List[dict]:
        if self._client is None:
            return []
        search_kwargs = {
            "collection_name": collection_name,
            "data": [query_emb],
            "limit": top_k,
            "output_fields": output_fields or ["pg_id", "content"],
        }
        if filter_expr:
            search_kwargs["filter"] = filter_expr

        def _call():
            hits: List[dict] = []
            results = self._client.search(**search_kwargs)
            if not results:
                return hits
            for result in results[0]:
                entity = result.get("entity", {}) if isinstance(result, dict) else {}
                hits.append({
                    "pg_id": entity.get("pg_id"),
                    "content": entity.get("content"),
                    "score": result.get("distance", 0.0) if isinstance(result, dict) else 0.0,
                })
            return hits
        return _guarded(self._breaker, [], _call, label="Milvus search")

    def delete(self, collection_name: str, filter_expr: str) -> bool:
        if self._client is None:
            return False
        def _call():
            self._client.delete(collection_name=collection_name, filter=filter_expr)
            return True
        return _guarded(self._breaker, False, _call, label="Milvus delete")


class _KafkaAdapter:
    """把 raw KafkaProducer 包成 KafkaClient-like 接口供 repo.eventbus 使用。"""

    def __init__(self, producer, cfg: APIConfig, ready: Status):
        self._producer = producer
        self._cfg = cfg
        self._ready = ready

    def is_real(self) -> bool:
        return self._producer is not None and self._ready.kafka == "connected"

    def produce(self, event_type: str, payload, topic: Optional[str] = None) -> bool:
        target_topic = topic or self._cfg.kafka_topic
        if not self.is_real():
            return False
        try:
            self._producer.send(
                target_topic,
                key=event_type.encode("utf-8"),
                value=payload,
            )
            return True
        except Exception as e:
            logger.warning("⚠️  Kafka 写入失败: %s", e)
            return False


class Infrastructure:
    """持有所有外部连接句柄"""

    def __init__(self, cfg: APIConfig):
        self.cfg = cfg
        self.ready = Status()
        self._pg = None
        self._pg_pool = None
        self._es = None
        self._kafka_producer = None
        self._milvus = None
        self._neo4j_memory = None
        self.memory_projection = None

        self._connect_postgres()
        self._connect_es()
        self._connect_kafka()
        self._connect_milvus()
        self._connect_memory_neo4j()

        # ─── repo 装配（统一持久化入口） ────────────────────────────────
        # 业务侧应通过 inf.repo.<domain>.<method>(...) 访问；此处不重复 connect，
        # 只把已有句柄包成符合 internal.platform 接口的 thin adapter 注入 repo。
        pg_client = _PGAdapter(
            self._pg,
            pool=self._pg_pool,
            connect_fn=(lambda: psycopg2.connect(self.cfg.pg_dsn())) if _HAS_PG else None,
        )
        es_client = _ESAdapter(self._es)
        milvus_client = _MilvusAdapter(self._milvus)
        kafka_client = _KafkaAdapter(self._kafka_producer, self.cfg, self.ready)

        self.repo = SimpleNamespace(
            ragchunk=ragchunk.Store(pg_client, milvus_client, es_client),
            ltm=longterm.PGRepo(pg_client),
            chat_history=chathistory.PGRepo(pg_client),
            preference=preference.PGRepo(pg_client),
            snapshot=snapshot.PGRepo(pg_client),
            documents=documentrepo.Store(pg_client),
            events=eventbus.KafkaPublisher(kafka_client),
        )
        self._start_memory_projection()

    # ─────────────────────────────── PostgreSQL ───────────────────────────────

    def _connect_postgres(self):
        if not _HAS_PG:
            logger.warning("⚠️  psycopg2 未安装，PostgreSQL 不可用")
            return
        if not self.cfg.pg_host:
            logger.warning("⚠️  PostgreSQL 未配置")
            return
        try:
            self._pg = psycopg2.connect(self.cfg.pg_dsn())
            self._pg.autocommit = True
            with self._pg.cursor() as cur:
                cur.execute("SELECT 1")
            self.ready.postgresql = "connected"
            # 事务专用连接池：memorytx / documentrepo 等多语句事务从池中
            # checkout 独立连接，避免在共享连接上切换 autocommit 的并发竞态。
            self._pg_pool = None
            if ThreadedConnectionPool is not None:
                try:
                    self._pg_pool = ThreadedConnectionPool(1, 10, self.cfg.pg_dsn())
                except Exception as pool_err:
                    logger.warning("⚠️  PG 事务连接池创建失败，退回共享连接事务: %s", pool_err)
            self._init_pg_schema()
            logger.info("✅ PostgreSQL 已连接: %s", self.cfg.pg_dsn())
        except Exception as e:
            logger.warning("⚠️  PostgreSQL 连接失败: %s", e)
            self._pg = None

    def _init_pg_schema(self):
        if not self._pg:
            return
        ddls = [
            """CREATE TABLE IF NOT EXISTS user_preferences (
                user_id    TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, key)
            )""",
            """CREATE TABLE IF NOT EXISTS task_snapshots (
                task_id    TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL DEFAULT 'legacy',
                state      JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS chat_history (
                id         SERIAL PRIMARY KEY,
                user_id    TEXT NOT NULL DEFAULT 'legacy',
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS long_term_memory (
                id            SERIAL PRIMARY KEY,
                user_id       TEXT NOT NULL DEFAULT 'legacy',
                content       TEXT NOT NULL,
                importance    FLOAT NOT NULL DEFAULT 0.5,
                embedding     JSONB,
                created_at    DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                last_accessed DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                category      VARCHAR(64) NOT NULL DEFAULT '',
                tags          JSONB NOT NULL DEFAULT '[]'::jsonb,
                slot_hint     VARCHAR(64) NOT NULL DEFAULT '',
                score         DOUBLE PRECISION NOT NULL DEFAULT 0.0
            )""",
            # 老库平滑加列：每条 ALTER 独立执行，互不阻塞
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS created_at    DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS last_accessed DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS category      VARCHAR(64) NOT NULL DEFAULT ''",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS tags          JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS slot_hint     VARCHAR(64) NOT NULL DEFAULT ''",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS score         DOUBLE PRECISION NOT NULL DEFAULT 0.0",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS user_id       TEXT NOT NULL DEFAULT 'legacy'",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS status        VARCHAR(32) NOT NULL DEFAULT 'active'",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS superseded_by BIGINT",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS quarantine_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS embedding_revision TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ",
            "ALTER TABLE long_term_memory ADD COLUMN IF NOT EXISTS supersedes JSONB NOT NULL DEFAULT '[]'::jsonb",
            "CREATE INDEX IF NOT EXISTS idx_ltm_active_user_id ON long_term_memory(user_id, id) WHERE deleted_at IS NULL",
            # 注意：此 memory_outbox 属于 PostgreSQL 业务库，仅由 repo/memory_projection.py
            # （locked_by/locked_at 列）读写。alembic 迁移里的同名表属于应用/评测
            # SQLite 库（lease_owner/lease_until 列，见 internal/application/models.py），
            # 两套存储互相独立，改列名前先确认目标库，勿混用。
            """CREATE TABLE IF NOT EXISTS memory_outbox (
                id BIGSERIAL PRIMARY KEY, event_id UUID NOT NULL UNIQUE,
                aggregate_id BIGINT NOT NULL, user_id TEXT NOT NULL,
                aggregate_version BIGINT NOT NULL, event_type TEXT NOT NULL,
                target TEXT NOT NULL, payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
                available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), locked_at TIMESTAMPTZ,
                locked_by TEXT, processed_at TIMESTAMPTZ, last_error TEXT,
                repair_dedupe_key TEXT UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
            "CREATE INDEX IF NOT EXISTS idx_memory_outbox_ready ON memory_outbox(target, available_at, id) WHERE status = 'pending'",
            "CREATE INDEX IF NOT EXISTS idx_memory_outbox_stale_lock ON memory_outbox(target, locked_at) WHERE status = 'processing'",
            "CREATE INDEX IF NOT EXISTS idx_memory_outbox_aggregate ON memory_outbox(aggregate_id, aggregate_version)",
            "ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'legacy'",
            "ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS conversation_id TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS ix_chat_history_conversation ON chat_history(user_id, conversation_id, id)",
            "ALTER TABLE task_snapshots ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'legacy'",
            "CREATE INDEX IF NOT EXISTS idx_lti_category ON long_term_memory(category)",
            "CREATE INDEX IF NOT EXISTS idx_lti_tags     ON long_term_memory USING GIN(tags)",
            "CREATE INDEX IF NOT EXISTS idx_lti_user_status ON long_term_memory(user_id, status, id)",
            "CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_snapshots_user ON task_snapshots(user_id, created_at DESC)",
            """CREATE TABLE IF NOT EXISTS rag_chunks (
                id          BIGSERIAL PRIMARY KEY,
                user_id     TEXT NOT NULL DEFAULT 'default_user',
                doc_hash    TEXT NOT NULL,
                chunk_idx   INT NOT NULL,
                content     TEXT NOT NULL,
                parent_content TEXT,
                embedding   JSONB,
                created_at  TIMESTAMP DEFAULT NOW()
            )""",
            """ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS parent_content TEXT""",
            "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS document_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS version_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS section TEXT NOT NULL DEFAULT ''",
            """ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS user_id
                   TEXT NOT NULL DEFAULT 'default_user'""",
            "ALTER TABLE rag_chunks DROP CONSTRAINT IF EXISTS rag_chunks_doc_hash_chunk_idx_key",
            "DROP INDEX IF EXISTS rag_chunks_doc_hash_chunk_idx_key",
            """CREATE UNIQUE INDEX IF NOT EXISTS rag_chunks_user_doc_hash_chunk_idx_key
                   ON rag_chunks (user_id, doc_hash, chunk_idx)""",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_user_id ON rag_chunks(user_id, id)",
            """CREATE TABLE IF NOT EXISTS documents (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                doc_type    TEXT NOT NULL,
                source      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'active',
                created_by  TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS document_versions (
                id          TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                version     INT NOT NULL,
                content_md  TEXT NOT NULL,
                summary     TEXT,
                metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (document_id, version)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_documents_updated_at ON documents(updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_document_versions_document_id_version ON document_versions(document_id, version DESC)",
        ]
        with self._pg.cursor() as cur:
            for ddl in ddls:
                try:
                    cur.execute(ddl)
                except Exception as e:
                    logger.warning("⚠️  PG 建表失败: %s", e)
        logger.info("✅ PostgreSQL 表结构已初始化")

    # ─────────────────────────────── Elasticsearch ───────────────────────────

    def _connect_es(self):
        if not _HAS_ES:
            logger.warning("⚠️  elasticsearch-py 未安装，ES 不可用")
            return
        if not self.cfg.es_addresses:
            logger.warning("⚠️  Elasticsearch 未配置")
            return
        try:
            auth = (self.cfg.es_username, self.cfg.es_password) if self.cfg.es_username else None
            self._es = Elasticsearch(
                self.cfg.es_addresses,
                basic_auth=auth,
            )
            if self._es.ping():
                self.ready.elasticsearch = "connected"
                logger.info("✅ Elasticsearch 已连接: %s", self.cfg.es_addresses)
            else:
                self._es = None
        except Exception as e:
            logger.warning("⚠️  Elasticsearch 连接失败: %s", e)
            self._es = None

    # ─────────────────────────────── Kafka ───────────────────────────────────

    def _connect_kafka(self):
        if not _HAS_KAFKA:
            logger.warning("⚠️  kafka-python 未安装，Kafka 不可用")
            return
        if not self.cfg.kafka_brokers:
            logger.warning("⚠️  Kafka 未配置")
            return
        try:
            self._kafka_producer = KafkaProducer(
                bootstrap_servers=self.cfg.kafka_brokers,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            )
            self.ready.kafka = "connected"
            logger.info("✅ Kafka 已连接: %s", self.cfg.kafka_brokers)
        except Exception as e:
            logger.warning("⚠️  Kafka 连接失败: %s (事件将输出到日志)", e)
            self._kafka_producer = None

    # ─────────────────────────────── Milvus ───────────────────────────────────

    def _connect_milvus(self):
        if not _HAS_MILVUS:
            logger.warning("⚠️  pymilvus 未安装，Milvus 不可用")
            self.ready.milvus = "memory-mode"
            return
        if not self.cfg.milvus_host or not self.cfg.milvus_port:
            logger.warning("⚠️  Milvus 未配置")
            self.ready.milvus = "memory-mode"
            return
        try:
            uri = f"http://{self.cfg.milvus_host}:{self.cfg.milvus_port}"
            self._milvus = MilvusClient(uri=uri)
            self.ready.milvus = "connected"
            logger.info("✅ Milvus 已连接: %s", uri)
            self._init_milvus_collections()
        except Exception as e:
            logger.warning("⚠️  Milvus 连接失败: %s (降级到内存模式)", e)
            self._milvus = None
            self.ready.milvus = "memory-mode"

    def _connect_memory_neo4j(self) -> None:
        """Create the strict Neo4j handle used by the durable memory worker."""

        if not bool(getattr(self.cfg, "kg_enabled", False)):
            return
        try:
            from internal.platform.neo4j import Neo4jClient

            client = Neo4jClient(self.cfg)
            if client.is_real():
                self._neo4j_memory = client
            else:
                client.close()
        except Exception as exc:
            logger.warning("⚠️  Neo4j 记忆投影不可用: %s", exc)
            self._neo4j_memory = None

    def _start_memory_projection(self) -> None:
        """Wire PostgreSQL outbox workers and periodic target reconciliation."""

        if self._pg is None or not _HAS_PG:
            return
        repository = memory_projection.PostgresMemoryOutboxRepository(
            lambda: psycopg2.connect(self.cfg.pg_dsn())
        )
        workers = []
        reconcilers = []

        if self._milvus is not None:
            try:
                store = memory_projection.MilvusMemoryProjectionStore(
                    self._milvus, int(self.cfg.rag_milvus_dim or 1024)
                )
                store.initialize()
                projector = memory_projection.MemoryTargetProjector(
                    memory_projection.Target.MILVUS, store
                )
                workers.append(
                    memory_projection.MemoryProjectionWorker(
                        repository,
                        projector,
                        worker_id="python-milvus-memory",
                    )
                )
                reconcilers.append(
                    memory_projection.MemoryReconciler(
                        repository, store, memory_projection.Target.MILVUS
                    )
                )
            except Exception as exc:
                logger.warning("⚠️  Milvus 记忆投影 worker 未启动: %s", exc)

        if self._neo4j_memory is not None:
            try:
                store = memory_projection.Neo4jMemoryProjectionStore(self._neo4j_memory)
                projector = memory_projection.MemoryTargetProjector(
                    memory_projection.Target.NEO4J, store
                )
                workers.append(
                    memory_projection.MemoryProjectionWorker(
                        repository,
                        projector,
                        worker_id="python-neo4j-memory",
                    )
                )
                reconcilers.append(
                    memory_projection.MemoryReconciler(
                        repository, store, memory_projection.Target.NEO4J
                    )
                )
            except Exception as exc:
                logger.warning("⚠️  Neo4j 记忆投影 worker 未启动: %s", exc)

        supervisor = memory_projection.MemoryProjectionSupervisor(
            repository,
            workers,
            reconcilers,
            poll_seconds=float(getattr(self.cfg, "memory_projection_poll_seconds", 0.5) or 0.5),
            reconcile_seconds=float(
                getattr(self.cfg, "memory_reconcile_seconds", 6 * 60 * 60)
                or 6 * 60 * 60
            ),
        )
        self.repo.memory_outbox = repository
        self.repo.memory_projection = supervisor
        self.memory_projection = supervisor
        supervisor.start()
        logger.info(
            "✅ 长期记忆投影已启动 workers=%d reconcile=%d",
            len(workers),
            len(reconcilers),
        )

    def _init_milvus_collections(self):
        if not self._milvus:
            return
        dim = int(self.cfg.rag_milvus_dim or 1024)
        try:
            if self._milvus.has_collection(RAG_COLLECTION):
                # 集合已存在：仅校验 schema，不一致只 warning，不删用户数据
                self._verify_milvus_rag_schema(RAG_COLLECTION, dim)
                return
            self._create_milvus_rag_collection(RAG_COLLECTION, dim)
        except Exception as e:
            logger.warning("⚠️  Milvus 创建集合失败: %s", e)

    def _create_milvus_rag_collection(self, collection_name: str, dim: int):
        """以显式 schema 创建 RAG 集合：pg_id / content / user_id / embedding。

        与 main 分支 Go 实现 EnsureMilvusCollection 对齐。
        """
        if not self._milvus or DataType is None:
            return
        schema = self._milvus.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(field_name="pg_id", datatype=DataType.INT64, is_primary=True)
        schema.add_field(
            field_name="content",
            datatype=DataType.VARCHAR,
            max_length=_RAG_CONTENT_MAX_LEN,
        )
        schema.add_field(
            field_name="user_id",
            datatype=DataType.VARCHAR,
            max_length=_RAG_USER_ID_MAX_LEN,
        )
        schema.add_field(
            field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=dim
        )

        index_params = self._milvus.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type=_RAG_INDEX_TYPE,
            metric_type=_RAG_METRIC_TYPE,
            params={"nlist": _RAG_INDEX_NLIST},
        )

        self._milvus.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
        )
        logger.info(
            "✅ Milvus 集合 %s 已创建 (dim=%d, index=%s, metric=%s, nlist=%d)",
            collection_name, dim, _RAG_INDEX_TYPE, _RAG_METRIC_TYPE, _RAG_INDEX_NLIST,
        )

    def _verify_milvus_rag_schema(self, collection_name: str, expected_dim: int):
        """校验已存在集合的 PK 字段名 / 向量维度，不一致只打 warning，不 drop。"""
        if not self._milvus:
            return
        try:
            desc = self._milvus.describe_collection(collection_name)
        except Exception as e:
            logger.warning("⚠️  Milvus describe_collection(%s) 失败: %s", collection_name, e)
            return

        if isinstance(desc, dict):
            fields = desc.get("fields") or []
        else:
            fields = getattr(desc, "fields", []) or []

        pk_name: Optional[str] = None
        embedding_dim: Optional[int] = None
        field_names = set()
        for f in fields:
            name = f.get("name") if isinstance(f, dict) else getattr(f, "name", None)
            if name:
                field_names.add(name)
            is_primary = (
                f.get("is_primary") if isinstance(f, dict) else getattr(f, "is_primary", False)
            )
            if is_primary:
                pk_name = name
            if name == "embedding":
                params = (
                    f.get("params") if isinstance(f, dict) else getattr(f, "params", {})
                ) or {}
                if isinstance(params, dict):
                    raw_dim = params.get("dim")
                    try:
                        embedding_dim = int(raw_dim) if raw_dim is not None else None
                    except (TypeError, ValueError):
                        embedding_dim = None

        if pk_name is not None and pk_name != "pg_id":
            logger.warning(
                "⚠️  Milvus 集合 %s 主键字段名不一致 (expected=pg_id, actual=%s)，"
                "请手动 drop 旧集合后重新 ingest 全量数据",
                collection_name, pk_name,
            )
        if embedding_dim is not None and embedding_dim != expected_dim:
            logger.warning(
                "⚠️  Milvus 集合 %s embedding 维度不一致 (expected=%d, actual=%d)，"
                "请手动 drop 旧集合后重新 ingest 全量数据",
                collection_name, expected_dim, embedding_dim,
            )
        if "user_id" not in field_names:
            logger.warning(
                "⚠️  Milvus 集合 %s 缺少 user_id 租户字段；为防止跨租户检索，"
                "请使用 PG 真相源重建该集合",
                collection_name,
            )

    # ─────────────────────────────── 生命周期 ────────────────────────────────

    def close(self):
        if self.memory_projection is not None:
            try:
                self.memory_projection.close()
            except Exception as e:
                logger.warning("⚠️  记忆投影关闭失败: %s", e)
            finally:
                self.memory_projection = None
        if self._neo4j_memory is not None:
            try:
                self._neo4j_memory.close()
            except Exception as e:
                logger.warning("⚠️  Neo4j 记忆投影连接关闭失败: %s", e)
            finally:
                self._neo4j_memory = None
        if self._pg:
            try:
                self._pg.close()
            except Exception as e:
                logger.warning("⚠️  PG 关闭失败: %s", e)
        if self._pg_pool is not None:
            try:
                self._pg_pool.closeall()
            except Exception as e:
                logger.warning("⚠️  PG 连接池关闭失败: %s", e)
        if self._kafka_producer:
            try:
                self._kafka_producer.flush()
                self._kafka_producer.close()
            except Exception as e:
                logger.warning("⚠️  Kafka 关闭失败: %s", e)
        if self._es:
            try:
                self._es.close()
            except Exception as e:
                logger.warning("⚠️  ES 关闭失败: %s", e)
        if self._milvus:
            try:
                self._milvus.close()
            except Exception as e:
                logger.warning("⚠️  Milvus 关闭失败: %s", e)
