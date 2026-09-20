# ragchunk — RAG chunk 的统一仓储。
#
# 一条 chunk 的写入会扇出到三个存储：
#   - PG 持久化原文 + embedding（可恢复源）
#   - Milvus 持久化向量索引（向量近邻搜索）
#   - ES 持久化倒排索引（BM25 关键词搜索）
#
# 三路写入若 Milvus / ES 任一缺席，仍以 PG 为真相源，应用整体可降级。
import json
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from internal.platform.es import ESClient
from internal.platform.milvus import MilvusClientWrapper
from internal.platform.postgres import PostgresClient

logger = logging.getLogger(__name__)

# Milvus / ES 中的 collection / index 名
COLLECTION_NAME = "rag_chunks"
ES_INDEX_NAME = "rag_chunks"
DEFAULT_USER_ID = "default_user"


@dataclass
class Row:
    """从 PG 读取的 RAG chunk 行。"""
    id: int = 0
    content: str = ""


@dataclass
class ESHit:
    """ES BM25 检索的单条结果。"""
    pg_id: int = 0
    score: float = 0.0


@dataclass
class MilvusHit:
    """Milvus 向量检索的单条结果（含距离分数）。"""
    id: int = 0
    distance: float = 0.0


def _is_missing_conflict_target_error(err: Exception) -> bool:
    msg = str(err).lower()
    return "on conflict" in msg and "unique or exclusion constraint" in msg


class Store:
    """默认实现，组合 PG / Milvus / ES 三个底层 client。"""

    def __init__(self,
                 pg: Optional[PostgresClient],
                 milvus: Optional[MilvusClientWrapper],
                 es: Optional[ESClient]):
        self.pg = pg
        self.milvus = milvus
        self.es = es

    # ─── 后端可用性 ───
    def milvus_available(self) -> bool:
        return self.milvus is not None and self.milvus.is_real()

    def es_available(self) -> bool:
        return self.es is not None and self.es.is_real()

    def _pg_real(self) -> bool:
        return self.pg is not None and self.pg.is_real() and self.pg.conn is not None

    # ─────────────────────────── PG ─────────────────────────────────────────

    # upsert chunk 到 PG，返回数据库自增 ID
    def save_pg(self, doc_hash: str, chunk_idx: int, content: str,
                embedding_json, user_id: str = DEFAULT_USER_ID) -> int:
        return self.save_pg_with_parent(
            doc_hash, chunk_idx, content, "", embedding_json, user_id=user_id
        )

    # 同 save_pg，但额外写入 parent_content（用于 child→parent 回填）
    def save_pg_with_parent(self, doc_hash: str, chunk_idx: int, content: str,
                            parent_content: str, embedding_json,
                            user_id: str = DEFAULT_USER_ID, *, document_id: str = "",
                            version_id: str = "", section: str = "") -> int:
        if not self._pg_real():
            return -1
        emb_param = embedding_json
        if isinstance(emb_param, (bytes, bytearray)):
            try:
                emb_param = bytes(emb_param).decode("utf-8")
            except Exception:
                emb_param = "[]"
        try:
            with self.pg.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO rag_chunks (user_id, doc_hash, chunk_idx, content, parent_content, embedding, document_id, version_id, section) "
                    "VALUES (%s, %s, %s, %s, NULLIF(%s, ''), %s, %s, %s, %s) "
                    "ON CONFLICT (user_id, doc_hash, chunk_idx) DO UPDATE SET "
                    "content = EXCLUDED.content, "
                    "parent_content = EXCLUDED.parent_content, "
                    "embedding = EXCLUDED.embedding, "
                    "document_id = COALESCE(NULLIF(EXCLUDED.document_id, ''), rag_chunks.document_id), "
                    "version_id = COALESCE(NULLIF(EXCLUDED.version_id, ''), rag_chunks.version_id), "
                    "section = COALESCE(NULLIF(EXCLUDED.section, ''), rag_chunks.section) "
                    "RETURNING id",
                    (user_id, doc_hash, chunk_idx, content, parent_content, emb_param, document_id, version_id, section),
                )
                row = cur.fetchone()
                return int(row[0]) if row else -1
        except Exception as e:
            if _is_missing_conflict_target_error(e):
                return self._save_pg_with_parent_fallback(
                    doc_hash, chunk_idx, content, parent_content, emb_param, user_id=user_id,
                    document_id=document_id, version_id=version_id, section=section
                )
            logger.warning("⚠️  RAG chunk 保存失败: %s", e)
            return -1

    def _save_pg_with_parent_fallback(self, doc_hash: str, chunk_idx: int, content: str,
                                      parent_content: str, embedding_json,
                                      user_id: str = DEFAULT_USER_ID, *, document_id: str = "",
                                      version_id: str = "", section: str = "") -> int:
        """兼容旧库唯一约束异常：不用 ON CONFLICT，显式 select/update/insert。"""
        try:
            with self.pg.conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM rag_chunks "
                    "WHERE user_id = %s AND doc_hash = %s AND chunk_idx = %s ORDER BY id LIMIT 1",
                    (user_id, doc_hash, chunk_idx),
                )
                row = cur.fetchone()
                if row:
                    pg_id = int(row[0])
                    cur.execute(
                        "UPDATE rag_chunks SET content = %s, parent_content = NULLIF(%s, ''), embedding = %s, "
                        "document_id = COALESCE(NULLIF(%s, ''), document_id), "
                        "version_id = COALESCE(NULLIF(%s, ''), version_id), "
                        "section = COALESCE(NULLIF(%s, ''), section) WHERE id = %s AND user_id = %s",
                        (content, parent_content, embedding_json, document_id, version_id, section, pg_id, user_id),
                    )
                    return pg_id

                cur.execute(
                    "INSERT INTO rag_chunks (user_id, doc_hash, chunk_idx, content, parent_content, embedding, document_id, version_id, section) "
                    "VALUES (%s, %s, %s, %s, NULLIF(%s, ''), %s, %s, %s, %s) RETURNING id",
                    (user_id, doc_hash, chunk_idx, content, parent_content, embedding_json, document_id, version_id, section),
                )
                row = cur.fetchone()
                return int(row[0]) if row else -1
        except Exception as e:
            logger.warning("⚠️  RAG chunk 保存失败: %s", e)
            return -1

    # 统计 PG 中 chunk 总数（用于启动期判断是否已有知识库）
    def count(self, user_id: str = DEFAULT_USER_ID) -> int:
        if not self._pg_real():
            return 0
        try:
            with self.pg.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM rag_chunks WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.warning("⚠️  统计 RAG chunks 失败: %s", e)
            return 0

    # 按 ID 列表批量读取 chunk
    def load_by_ids(self, ids: List[int],
                    user_id: str = DEFAULT_USER_ID) -> Tuple[List[Row], Optional[Exception]]:
        if not self._pg_real() or not ids:
            return [], RuntimeError("postgres not connected or empty ids")
        placeholders = ",".join(["%s"] * len(ids))
        query = f"SELECT id, content FROM rag_chunks WHERE id IN ({placeholders}) AND user_id = %s"
        try:
            rows = self.pg.query(query, (*ids, user_id))
        except Exception as e:
            return [], e
        result: List[Row] = []
        for r in rows:
            try:
                result.append(Row(id=int(r[0]), content=r[1] or ""))
            except Exception:
                continue
        return result, None

    # 按 ID 列表批量读取 chunk，含 parent_content（hybrid 检索回填用）
    def load_by_ids_with_parent(self, ids: List[int],
                                user_id: str = DEFAULT_USER_ID) -> List[dict]:
        if not self._pg_real() or not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        sql = (
            "SELECT id, content, COALESCE(parent_content, ''), document_id, version_id, section "
            f"FROM rag_chunks WHERE id IN ({placeholders}) AND user_id = %s"
        )
        try:
            rows = self.pg.query(sql, (*ids, user_id))
        except Exception as e:
            logger.warning("⚠️  加载 RAG chunks 失败: %s", e)
            return []
        out: List[dict] = []
        for r in rows:
            try:
                out.append({"id": int(r[0]), "content": r[1] or "", "parent_content": r[2] or "",
                            "document_id": r[3] or "", "version_id": r[4] or "", "section": r[5] or ""})
            except Exception:
                continue
        return out

    # 加载所有 chunk（启动期 TF 索引重建用）
    def load_all(self, user_id: str = DEFAULT_USER_ID) -> Tuple[List[Row], Optional[Exception]]:
        if not self._pg_real():
            return [], RuntimeError("postgres not connected")
        try:
            rows = self.pg.query(
                "SELECT id, content FROM rag_chunks WHERE user_id = %s ORDER BY id",
                (user_id,),
            )
        except Exception as e:
            return [], e
        result: List[Row] = []
        for r in rows:
            try:
                result.append(Row(id=int(r[0]), content=r[1] or ""))
            except Exception:
                continue
        return result, None

    # ─────────────────────────── ES ─────────────────────────────────────────

    # 创建 rag_chunks ES 索引（如不存在）
    def ensure_es_index(self) -> Optional[Exception]:
        if not self.es_available():
            return RuntimeError("elasticsearch not connected")
        try:
            client = self.es.client
            if client.indices.exists(index=ES_INDEX_NAME):
                # 旧索引平滑加列；不删除、不重建用户数据。
                try:
                    client.indices.put_mapping(
                        index=ES_INDEX_NAME,
                        body={"properties": {"user_id": {"type": "keyword"}}},
                    )
                except Exception as e:
                    logger.warning("⚠️  ES rag_chunks 租户字段映射升级失败: %s", e)
                return None
            mapping = {
                "mappings": {
                    "properties": {
                        "pg_id": {"type": "long"},
                        "content": {"type": "text", "analyzer": "standard"},
                        "doc_hash": {"type": "keyword"},
                        "chunk_idx": {"type": "integer"},
                        "user_id": {"type": "keyword"},
                    }
                }
            }
            client.indices.create(index=ES_INDEX_NAME, body=mapping)
            logger.info("✅ ES rag_chunks 索引已创建")
            return None
        except Exception as e:
            logger.warning("⚠️  创建 rag_chunks ES 索引失败: %s", e)
            return e

    # 索引一条 chunk 到 ES
    def index_es(self, pg_id: int, content: str, doc_hash: str, chunk_idx: int,
                 user_id: str = DEFAULT_USER_ID) -> Optional[Exception]:
        if not self.es_available():
            return RuntimeError("elasticsearch not connected")
        ok = self.es.index(
            ES_INDEX_NAME,
            pg_id,
            {
                "pg_id": pg_id,
                "content": content,
                "doc_hash": doc_hash,
                "chunk_idx": chunk_idx,
                "user_id": user_id,
            },
        )
        if not ok:
            return RuntimeError("es index failed")
        return None

    # 在 ES 上做 BM25 关键词检索（含 content 字段，rag.py 直接用作展示）
    def search_es_dicts(self, query: str, top_k: int,
                        user_id: str = DEFAULT_USER_ID) -> List[dict]:
        if not self.es_available():
            return []
        try:
            resp = self.es.search(
                ES_INDEX_NAME,
                {
                    "query": {
                        "bool": {
                            "must": [{"match": {"content": query}}],
                            "filter": [{"term": {"user_id": user_id}}],
                        }
                    },
                    "size": top_k,
                },
            )
        except Exception as e:
            logger.warning("⚠️  ES 检索失败: %s", e)
            raise
        hits: List[dict] = []
        for hit in resp.get("hits", {}).get("hits", []):
            try:
                src = hit.get("_source", {}) or {}
                pg_id = src.get("pg_id")
                if pg_id is None:
                    try:
                        pg_id = int(hit.get("_id"))
                    except Exception:
                        continue
                hits.append({
                    "pg_id": int(pg_id),
                    "content": src.get("content", ""),
                    "score": float(hit.get("_score") or 0.0),
                })
            except Exception:
                continue
        return hits

    # 在 ES 上做 BM25 关键词检索
    def search_es(self, query: str, top_k: int,
                  user_id: str = DEFAULT_USER_ID) -> Tuple[List[ESHit], Optional[Exception]]:
        if not self.es_available():
            return [], RuntimeError("elasticsearch not connected")
        body = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [{"match": {"content": {"query": query}}}],
                    "filter": [{"term": {"user_id": user_id}}],
                }
            },
            "_source": ["pg_id"],
        }
        try:
            resp = self.es.search(ES_INDEX_NAME, body)
        except Exception as e:
            return [], e
        hits: List[ESHit] = []
        for hit in resp.get("hits", {}).get("hits", []):
            try:
                source = hit.get("_source", {}) or {}
                pg_id = source.get("pg_id")
                if pg_id is None:
                    # 兜底用 _id 还原
                    try:
                        pg_id = int(hit.get("_id"))
                    except Exception:
                        continue
                hits.append(ESHit(pg_id=int(pg_id), score=float(hit.get("_score") or 0.0)))
            except Exception:
                continue
        return hits, None

    # ─────────────────────────── Milvus ─────────────────────────────────────

    # 创建 / 校验 rag_chunks collection
    def ensure_milvus_collection(self, dim: int) -> Optional[Exception]:
        if not self.milvus_available():
            return RuntimeError("milvus not connected")
        try:
            ok = self.milvus.ensure_collection(
                collection_name=COLLECTION_NAME,
                dimension=dim,
                auto_id=False,
                enable_dynamic_field=False,
            )
            if not ok:
                return RuntimeError("ensure milvus collection failed")
            return None
        except Exception as e:
            logger.warning("⚠️  Milvus rag_chunks 集合创建失败: %s", e)
            return e

    # 批量插入 chunk 向量到 Milvus
    def insert_milvus(self, pg_ids: List[int], contents: List[str],
                      embeddings: List[List[float]],
                      user_id: str = DEFAULT_USER_ID) -> Optional[Exception]:
        if not self.milvus_available():
            return RuntimeError("milvus not connected")
        if not pg_ids or not embeddings:
            return None
        try:
            data = []
            for pid, ct, emb in zip(pg_ids, contents, embeddings):
                # 字段命名严格与显式 schema (pg_id/content/embedding) 对齐
                data.append({
                    "pg_id": int(pid),
                    "content": ct,
                    "embedding": list(emb),
                    "user_id": user_id,
                })
            ok = self.milvus.upsert(COLLECTION_NAME, data)
            if not ok:
                return RuntimeError("milvus insert failed")
            return None
        except Exception as e:
            logger.warning("⚠️  Milvus rag_chunks 插入失败: %s", e)
            return e

    def load_all_for_reindex(self, user_id: str = DEFAULT_USER_ID) -> List[dict]:
        if not self._pg_real():
            return []
        rows = self.pg.query(
            "SELECT id, content, doc_hash, chunk_idx, embedding "
            "FROM rag_chunks WHERE user_id = %s ORDER BY id",
            (user_id,),
        )
        output = []
        for row in rows or []:
            try:
                embedding = row[4]
                if isinstance(embedding, str):
                    embedding = json.loads(embedding)
                output.append({
                    "pg_id": int(row[0]),
                    "content": str(row[1] or ""),
                    "doc_hash": str(row[2] or ""),
                    "chunk_idx": int(row[3]),
                    "embedding": [float(value) for value in (embedding or [])],
                })
            except Exception:
                continue
        return output

    # 在 Milvus 上做向量近邻检索（含距离分数）
    def search_milvus(self, vector: List[float], top_k: int,
                      user_id: str = DEFAULT_USER_ID) -> Tuple[List[MilvusHit], Optional[Exception]]:
        if not self.milvus_available():
            return [], RuntimeError("milvus not connected")
        try:
            raw = self._search_milvus_scoped(
                vector, top_k, ["pg_id"], user_id=user_id
            )
        except Exception as e:
            return [], e
        hits: List[MilvusHit] = []
        for r in raw:
            try:
                pid = r.get("pg_id")
                if pid is None:
                    continue
                hits.append(MilvusHit(id=int(pid), distance=float(r.get("score") or 0.0)))
            except Exception:
                continue
        return hits, None

    # 兼容旧 infra.milvus_search_with_scores：返回 dict 列表（含 content/score）
    def search_milvus_dicts(self, query_emb: List[float], top_k: int,
                            user_id: str = DEFAULT_USER_ID) -> List[dict]:
        if not self.milvus_available():
            return []
        try:
            return self._search_milvus_scoped(
                query_emb, top_k, ["pg_id", "content"], user_id=user_id
            ) or []
        except Exception as e:
            logger.warning("⚠️  Milvus 检索失败: %s", e)
            raise

    def _search_milvus_scoped(self, query_emb: List[float], top_k: int,
                               output_fields: List[str], *, user_id: str) -> List[dict]:
        """Execute a fail-closed tenant-filtered Milvus search.

        The platform adapters accept ``filter_expr`` and translate it to the
        pymilvus ``filter`` argument.  Keeping the filter mandatory here avoids
        accidentally falling back to an unscoped search when an old adapter is
        wired in.
        """

        filter_expr = f"user_id == {json.dumps(user_id)}"
        return self.milvus.search(
            COLLECTION_NAME,
            query_emb,
            top_k,
            output_fields=output_fields,
            filter_expr=filter_expr,
        )

    # ─────────────────────────── 删除（三路级联）───────────────────────────

    # 按 doc_hash 删除三个存储中相关的 chunk
    def delete(self, doc_hash: str, user_id: str = DEFAULT_USER_ID) -> Optional[Exception]:
        pg_ids, err = self._delete_pg(doc_hash, user_id=user_id)
        if err is not None:
            return RuntimeError(f"PG 删除失败: {err}")
        if not pg_ids:
            return None
        if self.es_available():
            self._delete_es(pg_ids)
        if self.milvus_available():
            self._delete_milvus(pg_ids, user_id=user_id)
        return None

    # 仅 PG 删除并返回被删除的 ID（与旧 infra.delete_rag_chunks_by_doc_hash 行为一致）
    def delete_by_doc_hash(self, doc_hash: str, user_id: str = DEFAULT_USER_ID) -> List[int]:
        ids, _ = self._delete_pg(doc_hash, user_id=user_id)
        return ids

    # ES 上按 pg_id 列表删除（公开版）
    def delete_es(self, pg_ids: List[int]) -> None:
        self._delete_es(pg_ids)

    # Milvus 上按 pg_id 列表删除（公开版）
    def delete_milvus(self, pg_ids: List[int], user_id: str = DEFAULT_USER_ID) -> None:
        self._delete_milvus(pg_ids, user_id=user_id)

    # 从 PG 删除并返回被删除的 ID
    def _delete_pg(self, doc_hash: str,
                   user_id: str = DEFAULT_USER_ID) -> Tuple[List[int], Optional[Exception]]:
        if not self._pg_real():
            return [], RuntimeError("postgres not connected")
        # 单条 DELETE ... RETURNING：先前"先 SELECT 再 DELETE"的两步在
        # autocommit 连接上存在 TOCTOU——并发写入会漏删或返回已被删除的 id。
        try:
            rows = self.pg.query(
                "DELETE FROM rag_chunks WHERE user_id = %s AND doc_hash = %s RETURNING id",
                (user_id, doc_hash),
            )
        except Exception as e:
            return [], e
        ids: List[int] = []
        for r in rows:
            try:
                ids.append(int(r[0]))
            except Exception:
                continue
        return ids, None

    def _delete_es(self, pg_ids: List[int]) -> None:
        if self.es is None:
            return
        try:
            self.es.delete_many(ES_INDEX_NAME, list(pg_ids))
        except Exception as e:
            logger.warning("⚠️  ES 删除失败: %s", e)

    def _delete_milvus(self, pg_ids: List[int], user_id: str = DEFAULT_USER_ID) -> None:
        if self.milvus is None or not pg_ids:
            return
        id_strs = ", ".join(str(int(i)) for i in pg_ids)
        expr = f"user_id == {json.dumps(user_id)} and pg_id in [{id_strs}]"
        try:
            self.milvus.delete(COLLECTION_NAME, expr)
        except Exception as e:
            logger.warning("⚠️  Milvus 删除失败: %s", e)

    # ─────────────────────────── 启动初始化 ─────────────────────────────────

    # 启动期建表 / 建索引
    def init(self, dim: int) -> None:
        if self.milvus_available():
            err = self.ensure_milvus_collection(dim)
            if err is not None:
                logger.warning("⚠️  Milvus rag_chunks 初始化失败: %s", err)
        if self.es_available():
            err = self.ensure_es_index()
            if err is not None:
                logger.warning("⚠️  ES rag_chunks 初始化失败: %s", err)
