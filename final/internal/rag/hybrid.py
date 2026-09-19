# hybrid — 企业级混合检索：Milvus 语义 + ES BM25 + Neo4j 知识图谱 + 三路 RRF 融合
#
# 三路 score 来自 reciprocal rank fusion（基于 rank，不依赖原始分尺度）：
#     score(d) = Σ_i weight_i / (k + rank_i(d))
# 为了与冻结的 Go 845e8f7 行为对齐，Milvus/ES 的权重固定为 1.0，
# KG 使用 kg_weight（非正数时回退到 1.0）。semantic_weight 保留为配置兼容字段，
# 与 Go 当前实现一样，不参与 RRF 计算。
import logging
import math
from dataclasses import dataclass, field
import inspect
import json
import threading
from internal.resilience.budget import inherit_context
import time
from typing import Callable, Dict, List, Optional

from config.config import APIConfig
from internal.graph.types import ChunkRef
from internal.graph.kgstore import KGStore
from internal.infra.infra import Infrastructure
from internal.resilience.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


# Embedding 回调签名：text -> List[float]
EmbedFn = Callable[[str], List[float]]


@dataclass
class HybridResult:
    """混合检索的单条结果（与 Go 版 HybridResult 字段对齐）"""
    pg_id: int = 0
    content: str = ""
    score: float = 0.0
    source: str = ""  # "hybrid" | "semantic" | "keyword"
    parent: str = ""
    document_id: str = ""
    version_id: str = ""
    section: str = ""


@dataclass
class _PathHits:
    """单路检索结果（rank 顺序）+ 是否成功"""
    hits: List[dict] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    circuit: Optional[dict] = None


class HybridStore:
    """企业级混合检索：
        - Milvus 语义向量检索
        - Elasticsearch BM25 关键词检索
        - Neo4j 知识图谱实体遍历检索
        - Reciprocal Rank Fusion 三路融合

    根据基础设施可用性自动选择检索模式：
        hybrid / semantic / keyword / unavailable
    """

    def __init__(
        self,
        cfg: APIConfig,
        inf: Infrastructure,
        embed_fn: Optional[EmbedFn] = None,
        kg: Optional[KGStore] = None,
        user_id: str = "default_user",
    ):
        self.cfg = cfg
        self.inf = inf
        self._embed_fn = embed_fn
        self._kg = kg
        self.user_id = str(user_id or "default_user")
        self._reranker = None
        self._lightweight = None
        self._lightweight_extractor = None
        self._lightweight_embed_batch = None
        circuit_args = {
            "failure_threshold": getattr(cfg, "rag_retrieval_failure_threshold", 3),
            "cooldown_seconds": getattr(cfg, "rag_retrieval_cooldown_seconds", 30.0),
            "half_open_max_calls": getattr(cfg, "rag_retrieval_half_open_max_calls", 1),
        }
        self._milvus_circuit = CircuitBreaker(**circuit_args)
        self._es_circuit = CircuitBreaker(**circuit_args)
        self._kg_circuit = CircuitBreaker(**circuit_args)
        self.mode = self._resolve_mode()

    # ─── 注入式 setter（与 Go 版接口对齐） ─────────────────────────────────

    def set_embed_fn(self, fn: EmbedFn) -> None:
        self._embed_fn = fn

    def set_lightweight_llm(self, llm) -> None:
        from internal.rag.lightweight import StrictExtractor
        self._lightweight_extractor = StrictExtractor(llm)
        self._lightweight_embed_batch = llm.embed_batch

    def _local_index(self):
        if self._lightweight is None:
            from internal.rag.lightweight import LightweightIndex
            identity = [self.cfg.embedding_api_url, self.cfg.embedding_model]
            self._lightweight = LightweightIndex(self.cfg.rag_lightweight_path, identity, self.user_id)
        return self._lightweight

    def set_kg_store(self, kg: Optional[KGStore]) -> None:
        self._kg = kg

    def set_reranker(self, reranker) -> None:
        self._reranker = reranker

    # ─── 入库 ──────────────────────────────────────────────────────────────

    def index_with_parents(
        self,
        doc_hash: str,
        contents: List[str],
        parents: List[str],
        embeddings: List[List[float]],
        *,
        document_id: str = "",
        version_id: str = "",
        section: str = "",
    ) -> List[int]:
        """把 PG / ES / Milvus / KG 写入收敛到 HybridStore。

        Engine 只负责切片和向量化；这里负责所有持久化扇出。KG 写入在后台线程
        best-effort 执行，异常只记录日志，不阻塞主入库流程。
        """
        pg_ids: List[int] = []
        valid_contents: List[str] = []
        valid_embeddings: List[List[float]] = []
        valid_chunk_idxs: List[int] = []

        for idx, content in enumerate(contents):
            embedding = embeddings[idx] if idx < len(embeddings) else []
            parent_content = parents[idx] if idx < len(parents) else ""
            pg_id = self._repo_call(
                "save_pg_with_parent",
                doc_hash,
                idx,
                content,
                parent_content,
                json.dumps(embedding),
                document_id=document_id,
                version_id=version_id,
                section=section,
            )
            if pg_id <= 0:
                continue
            pg_ids.append(pg_id)
            valid_contents.append(content)
            valid_embeddings.append(embedding)
            valid_chunk_idxs.append(idx)
            if self.inf.ready.elasticsearch == "connected":
                self._write_index_with_retry(
                    "elasticsearch",
                    lambda pg_id=pg_id, content=content, idx=idx: self._repo_call(
                        "index_es", pg_id, content, doc_hash, idx
                    ),
                    {"pg_id": pg_id, "doc_hash": doc_hash, "chunk_idx": idx},
                )

        milvus_ids: List[int] = []
        milvus_contents: List[str] = []
        milvus_embeddings: List[List[float]] = []
        if self.inf.ready.milvus == "connected":
            for pg_id, content, embedding in zip(pg_ids, valid_contents, valid_embeddings):
                if embedding and len(embedding) == self.cfg.rag_milvus_dim:
                    milvus_ids.append(pg_id)
                    milvus_contents.append(content)
                    milvus_embeddings.append(embedding)
        if milvus_ids:
            self._write_index_with_retry(
                "milvus",
                lambda: self._repo_call(
                    "insert_milvus", milvus_ids, milvus_contents, milvus_embeddings
                ),
                {"pg_ids": list(milvus_ids), "doc_hash": doc_hash},
            )

        if self._kg is not None and self._kg.available() and pg_ids:
            refs = [
                ChunkRef(id=idx, pg_id=pg_id, content=content)
                for idx, pg_id, content in zip(valid_chunk_idxs, pg_ids, valid_contents)
            ]
            threading.Thread(
                target=self._index_kg_safe,
                args=(doc_hash, refs),
                name="rag-kg-index",
                daemon=True,
            ).start()

        if getattr(self.cfg, 'rag_lightweight_enabled', False):
            # Explicit ingestion is retryable through reindex. Query paths do
            # not silently spend model calls to repair missing projections.
            self.rebuild_lightweight(dict(zip(pg_ids, valid_embeddings)))
        return pg_ids

    def rebuild_lightweight(self, supplied_vectors=None):
        rows = self._repo_call('lightweight_rows')
        self._repo_call('repair_fts5')
        return self._local_index().index(rows, self._embed_fn,
                                         self._lightweight_extractor, supplied_vectors,
                                         self._lightweight_embed_batch)

    def rebuild_indexes(self) -> dict:
        """Repair ES/Milvus projections from the primary chunk store."""

        if getattr(self.cfg, 'rag_lightweight_enabled', False):
            return {'mode': 'lightweight', **self.rebuild_lightweight()}

        rows = self._repo_call("load_all_for_reindex") or []
        report = {
            "source_rows": len(rows),
            "elasticsearch_indexed": 0,
            "milvus_indexed": 0,
            "failures": [],
        }
        if self._es_ok():
            for row in rows:
                ok = self._write_index_with_retry(
                    "elasticsearch",
                    lambda row=row: self._repo_call(
                        "index_es",
                        int(row["pg_id"]),
                        row["content"],
                        row["doc_hash"],
                        int(row["chunk_idx"]),
                    ),
                    {"pg_id": row.get("pg_id"), "repair": True},
                )
                if ok:
                    report["elasticsearch_indexed"] += 1
                else:
                    report["failures"].append({"target": "elasticsearch", "pg_id": row.get("pg_id")})
        if self._milvus_ok():
            valid = [
                row for row in rows
                if row.get("embedding") and len(row["embedding"]) == self.cfg.rag_milvus_dim
            ]
            if valid:
                ok = self._write_index_with_retry(
                    "milvus",
                    lambda: self._repo_call(
                        "insert_milvus",
                        [int(row["pg_id"]) for row in valid],
                        [row["content"] for row in valid],
                        [row["embedding"] for row in valid],
                    ),
                    {"pg_ids": [row.get("pg_id") for row in valid], "repair": True},
                )
                if ok:
                    report["milvus_indexed"] = len(valid)
                else:
                    report["failures"].append({"target": "milvus", "count": len(valid)})
        report["ok"] = not report["failures"]
        return report

    def _write_index_with_retry(self, target: str, operation, payload: dict) -> bool:
        attempts = max(1, min(int(getattr(self.cfg, "max_retries", 3) or 3), 5))
        delay = max(0.0, float(getattr(self.cfg, "retry_delay_ms", 200) or 0) / 1000.0)
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                result = operation()
                if isinstance(result, Exception):
                    raise result
                return True
            except Exception as exc:
                last_error = exc
                if attempt < attempts and delay:
                    time.sleep(delay * attempt)
        logger.warning("⚠️  %s 索引写入失败（%d 次）: %s", target, attempts, last_error)
        try:
            self.inf.repo.events.publish(
                "rag.index_failed",
                json.dumps({
                    "target": target,
                    "attempts": attempts,
                    "error": str(last_error)[:500],
                    **payload,
                }, ensure_ascii=False),
            )
        except Exception:
            pass
        return False

    def _index_kg_safe(self, doc_hash: str, refs: List[ChunkRef]) -> None:
        try:
            if self._kg is not None and self._kg.available():
                self._kg.index_document(doc_hash, refs)
        except Exception as e:
            logger.warning("⚠️  RAG chunks 写入 Neo4j 知识图谱失败: %s", e)

    # ─── 基础设施可用性 ──────────────────────────────────────────────────

    def _milvus_ok(self) -> bool:
        return self.inf.ready.milvus == "connected"

    def _es_ok(self) -> bool:
        return self.inf.ready.elasticsearch == "connected"

    def _kg_ok(self) -> bool:
        return self._kg is not None and self._kg.available()

    def _resolve_mode(self) -> str:
        if getattr(self.cfg, 'rag_lightweight_enabled', False):
            if not bool(getattr(self.inf.repo.ragchunk, 'local_available', False)):
                raise RuntimeError('Lightweight retrieval requires the SQLite chunk repository')
            return 'lightweight'
        m, e = self._milvus_ok(), self._es_ok()
        if m and e:
            return "hybrid"
        if m:
            return "semantic"
        if e:
            return "keyword"
        if bool(getattr(self.inf.repo.ragchunk, "local_available", False)):
            return "local"
        return "unavailable"

    # ─── 入口 ───────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int, trace: Optional[dict] = None) -> List[HybridResult]:
        # 模式可能在运行时变化（连接恢复），每次入口重新判定
        self.mode = self._resolve_mode()
        if trace is not None:
            trace["mode"] = self.mode
        if self.mode == 'lightweight':
            results = self._search_lightweight(query, top_k, trace)
        elif self.mode == "hybrid":
            results = self._search_hybrid(query, top_k, trace)
        elif self.mode == "semantic":
            results = self._search_semantic(query, top_k)
        elif self.mode == "keyword":
            results = self._search_keyword(query, top_k)
        elif self.mode == "local":
            results = self._search_local(query, top_k)
        else:
            logger.warning("⚠️  检索基础设施不可用（Milvus 和 ES 均未连接）")
            results = []
        if trace is not None:
            trace["returned_candidates"] = _trace_results(results)
        return results

    def search_multi(self, queries: List[str], top_k: int, trace: Optional[dict] = None) -> List[HybridResult]:
        queries = [q for q in (queries or []) if q]
        if not queries:
            return []
        pool = self._rerank_pool(top_k)
        if len(queries) == 1:
            query_trace: dict = {"query": queries[0]}
            results = self._finalize(queries[0], self.search(queries[0], pool, query_trace), top_k)
            if trace is not None:
                trace["query_paths"] = [query_trace]
                trace["final_candidates"] = _trace_results(results)
                trace["reranker"] = self._reranker_trace(results)
            return results

        results_by_query: List[List[HybridResult]] = [[] for _ in queries]
        query_traces: List[dict] = [{"query": query} for query in queries]
        threads = []

        def _run(idx: int, q: str) -> None:
            results_by_query[idx] = self.search(q, pool, query_traces[idx])

        for i, q in enumerate(queries):
            t = threading.Thread(target=inherit_context(_run), args=(i, q), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        k = self.cfg.rrf_constant_k if self.cfg.rrf_constant_k > 0 else 60
        merged: Dict[str, dict] = {}
        for query_results in results_by_query:
            for rank, result in enumerate(query_results):
                # Go 845e8f7 以完整 chunk content 作为跨 query 聚合键。
                # 这会将不同 PG ID 但内容相同的 chunk 视为同一证据。
                key = result.content
                score = 1.0 / float(k + rank + 1)
                if key in merged:
                    merged[key]["score"] += score
                    if result.score > merged[key]["result"].score:
                        merged[key]["result"] = result
                else:
                    merged[key] = {"score": score, "result": result}

        out: List[HybridResult] = []
        for item in merged.values():
            result = item["result"]
            result.score = item["score"]
            out.append(result)
        out.sort(key=lambda r: r.score, reverse=True)
        if len(out) > pool:
            out = out[:pool]
        results = self._finalize(queries[0], out, top_k)
        if trace is not None:
            trace["query_paths"] = query_traces
            trace["second_level_rrf"] = _trace_results(out)
            trace["final_candidates"] = _trace_results(results)
            trace["reranker"] = self._reranker_trace(results)
        return results

    def _reranker_trace(self, results: List[HybridResult]) -> dict:
        remote_applied = any("+rerank" in str(result.source or "") for result in results)
        local_fallback_applied = any(
            "+local_rerank" in str(result.source or "") or "+cross_encoder" in str(result.source or "")
            for result in results
        )
        cross_encoder_applied = any("+cross_encoder" in str(result.source or "") for result in results)
        trace = {
            "configured": self._reranker is not None,
            "applied": remote_applied or local_fallback_applied,
            "remote_applied": remote_applied,
            "local_fallback_applied": local_fallback_applied,
            "cross_encoder_applied": cross_encoder_applied,
        }
        if self._reranker is not None and hasattr(self._reranker, "circuit_snapshot"):
            trace["circuit"] = self._reranker.circuit_snapshot()
        return trace

    def _rerank_pool(self, top_k: int) -> int:
        pool = top_k * (4 if self._reranker is not None else 2)
        return max(pool, 10)

    def _finalize(self, query: str, results: List[HybridResult], top_k: int) -> List[HybridResult]:
        # Child hits sharing a parent otherwise occupy every top-k slot and
        # are only deduplicated after retrieval, losing other useful evidence.
        distinct, seen = [], set()
        for result in results:
            key = (result.document_id, result.version_id, result.parent)
            if result.parent and key in seen:
                continue
            if result.parent:
                seen.add(key)
            distinct.append(result)
        results = distinct
        if self._reranker is not None and results:
            return self._reranker.rerank(query, results, top_k)
        if top_k > 0 and len(results) > top_k:
            return results[:top_k]
        return results

    # ─── 混合检索：三路 RRF 融合 ─────────────────────────────────────────

    def _search_hybrid(self, query: str, top_k: int, trace: Optional[dict] = None) -> List[HybridResult]:
        # 从每路取 2*top_k，给融合留候选（与 Go 版一致，下限 10）
        fetch_k = max(top_k * 2, 10)

        milvus_path = self._fetch_milvus(query, fetch_k)
        es_path = self._fetch_es(query, fetch_k)
        # Go 只在 Milvus 与 ES 都成功后才执行 KG 第三路；两个主索引
        # 同时失败时不使用 KG 单路兜底。先填一个未执行占位，便于 trace 如实表达。
        kg_path = _PathHits(ok=False, reason="not_executed")
        if not milvus_path.ok and not es_path.ok:
            logger.warning("⚠️  Milvus 与 ES 检索均失败")
            if trace is not None:
                trace["retrieval_paths"] = {
                    "semantic": _trace_path(milvus_path),
                    "keyword": _trace_path(es_path),
                    "knowledge_graph": _trace_path(kg_path),
                }
            return []
        if not milvus_path.ok:
            if trace is not None:
                trace["retrieval_paths"] = {
                    "semantic": _trace_path(milvus_path),
                    "keyword": _trace_path(es_path),
                    "knowledge_graph": _trace_path(kg_path),
                }
            return self._search_keyword(query, top_k)
        if not es_path.ok:
            if trace is not None:
                trace["retrieval_paths"] = {
                    "semantic": _trace_path(milvus_path),
                    "keyword": _trace_path(es_path),
                    "knowledge_graph": _trace_path(kg_path),
                }
            return self._search_semantic(query, top_k)

        kg_path = self._fetch_kg(query, fetch_k)
        if trace is not None:
            trace["retrieval_paths"] = {
                "semantic": _trace_path(milvus_path),
                "keyword": _trace_path(es_path),
                "knowledge_graph": _trace_path(kg_path),
            }

        k = self.cfg.rrf_constant_k if self.cfg.rrf_constant_k > 0 else 60
        rrf_scores: Dict[int, float] = {}

        for rank, hit in enumerate(milvus_path.hits):
            pg_id = hit.get("pg_id")
            if pg_id is None:
                continue
            rrf_scores[pg_id] = rrf_scores.get(pg_id, 0.0) + 1.0 / (k + rank + 1)

        for rank, hit in enumerate(es_path.hits):
            pg_id = hit.get("pg_id")
            if pg_id is None:
                continue
            rrf_scores[pg_id] = rrf_scores.get(pg_id, 0.0) + 1.0 / (k + rank + 1)

        if kg_path.ok and self._kg is not None:
            kg_w = float(getattr(self.cfg, "kg_weight", 0.0) or 0.0)
            if kg_w <= 0:
                kg_w = 1.0
            for rank, hit in enumerate(kg_path.hits):
                pg_id = hit.pg_id if hasattr(hit, "pg_id") else hit.get("pg_id", 0)
                if not pg_id:
                    continue
                rrf_scores[pg_id] = rrf_scores.get(pg_id, 0.0) + kg_w / (k + rank + 1)

        sorted_ids = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_ids) > top_k:
            sorted_ids = sorted_ids[:top_k]
        if not sorted_ids:
            return []

        ids = [pid for pid, _ in sorted_ids]
        rows = self._repo_call("load_by_ids_with_parent", ids)
        row_map: Dict[int, dict] = {r["id"]: r for r in rows}
        results: List[HybridResult] = []
        for pid, score in sorted_ids:
            row = row_map.get(pid)
            if row is None:
                continue
            results.append(HybridResult(
                pg_id=pid,
                content=row.get("content", ""),
                score=score,
                source="hybrid",
                parent=row.get("parent_content", "") or row.get("parent", ""),
                document_id=row.get("document_id", ""), version_id=row.get("version_id", ""), section=row.get("section", ""),
            ))
        return results

    # ─── 单路：Milvus 语义 ───────────────────────────────────────────────

    def _search_semantic(self, query: str, top_k: int) -> List[HybridResult]:
        path = self._fetch_milvus(query, top_k)
        if not path.ok:
            return []
        ids = [h["pg_id"] for h in path.hits if h.get("pg_id") is not None]
        rows = self._repo_call("load_by_ids_with_parent", ids) if ids else []
        row_map: Dict[int, dict] = {r["id"]: r for r in rows}
        results: List[HybridResult] = []
        for h in path.hits:
            pid = h.get("pg_id")
            if pid is None:
                continue
            row = row_map.get(pid, {})
            content = row.get("content") or h.get("content") or ""
            if not content:
                continue
            results.append(HybridResult(
                pg_id=pid, content=content,
                score=float(h.get("score", 0.0)), source="semantic",
                parent=row.get("parent_content", "") or row.get("parent", ""),
                document_id=row.get("document_id", ""), version_id=row.get("version_id", ""), section=row.get("section", ""),
            ))
        return results

    # ─── 单路：ES BM25 ───────────────────────────────────────────────────

    def _search_keyword(self, query: str, top_k: int) -> List[HybridResult]:
        path = self._fetch_es(query, top_k)
        if not path.ok:
            return []
        ids = [h["pg_id"] for h in path.hits if h.get("pg_id") is not None]
        rows = self._repo_call("load_by_ids_with_parent", ids) if ids else []
        row_map: Dict[int, dict] = {r["id"]: r for r in rows}
        results: List[HybridResult] = []
        for h in path.hits:
            pid = h.get("pg_id")
            if pid is None:
                continue
            row = row_map.get(pid, {})
            content = row.get("content") or h.get("content") or ""
            if not content:
                continue
            results.append(HybridResult(
                pg_id=pid, content=content,
                score=float(h.get("score", 0.0)), source="keyword",
                parent=row.get("parent_content", "") or row.get("parent", ""),
                document_id=row.get("document_id", ""), version_id=row.get("version_id", ""), section=row.get("section", ""),
            ))
        return results

    def _search_local(self, query: str, top_k: int) -> List[HybridResult]:
        hits = self._repo_call("search_fts5", query, top_k)
        if not hits:
            hits = self._repo_call("search_local", query, top_k) or []
        return [
            HybridResult(
                pg_id=int(hit.get("pg_id") or 0),
                content=hit.get("content", ""),
                parent=hit.get("parent_content", ""),
                document_id=hit.get("document_id", ""), version_id=hit.get("version_id", ""), section=hit.get("section", ""),
                score=float(hit.get("score") or 0.0),
                source=hit.get("source", "local_fts5"),
            )
            for hit in hits
            if hit.get("content")
        ]

    def _search_lightweight(self, query, top_k, trace=None):
        rows = self._repo_call('lightweight_rows')
        by_id = {int(row['id']): row for row in rows}
        paths, state = {}, {}
        keyword = self._repo_call('search_fts5', query, top_k) or []
        paths['bm25'] = [int(hit['pg_id']) for hit in keyword]
        state['bm25'] = {'ok': True, 'hits': len(keyword)}
        try:
            index = self._local_index()
            current = index.reconcile(rows)
            state['index'] = index.status(rows)
            for name, fetch in (
                ('semantic', lambda: index.semantic(self._embed_fn(query), top_k, current)),
                ('graph', lambda: index.graph(query, top_k, current)),
            ):
                try:
                    paths[name] = fetch() if rows else []
                    state[name] = {'ok': True, 'hits': len(paths[name])}
                except Exception as exc:
                    paths[name] = []
                    state[name] = {'ok': False, 'reason': type(exc).__name__}
                    logger.warning('Lightweight %s retrieval failed: %s', name, type(exc).__name__)
        except Exception as exc:
            state['index'] = {'ok': False, 'reason': type(exc).__name__}
        # RRF combines ranks, never compares Chroma distance with BM25 score.
        scores = {}
        k = max(1, self.cfg.rrf_constant_k)
        for name, ids in paths.items():
            weight = max(0.0, self.cfg.kg_weight) if name == 'graph' else 1.0
            for rank, pid in enumerate(ids, 1):
                if pid in by_id:
                    scores[pid] = scores.get(pid, 0) + weight / (k + rank)
        if trace is not None:
            trace['lightweight_paths'] = state
            trace['path_ids'] = paths
        result = []
        for pid in sorted(scores, key=lambda pid: (-scores[pid], pid))[:top_k]:
            row = by_id[pid]
            result.append(HybridResult(pg_id=pid, content=row['content'],
                parent=row.get('parent_content', ''), document_id=row.get('document_id', ''),
                version_id=row.get('version_id', ''), section=row.get('section', ''),
                score=scores[pid], source='lightweight_hybrid'))
        return result

    def _search_kg_hits(self, hits: List, top_k: int) -> List[HybridResult]:
        selected = list(hits or [])[:top_k]
        ids = [int(getattr(hit, "pg_id", 0) or 0) for hit in selected]
        rows = self._repo_call("load_by_ids_with_parent", [pid for pid in ids if pid])
        row_map = {int(row.get("id") or 0): row for row in rows or []}
        results: List[HybridResult] = []
        for hit in selected:
            pg_id = int(getattr(hit, "pg_id", 0) or 0)
            row = row_map.get(pg_id, {})
            content = row.get("content", "")
            if not pg_id or not content:
                continue
            results.append(HybridResult(
                pg_id=pg_id,
                content=content,
                parent=row.get("parent_content", "") or row.get("parent", ""),
                document_id=row.get("document_id", ""), version_id=row.get("version_id", ""), section=row.get("section", ""),
                score=float(getattr(hit, "score", 0.0) or 0.0),
                source="knowledge_graph",
            ))
        return results

    def _repo_call(self, method_name: str, *args, **kwargs):
        method = getattr(self.inf.repo.ragchunk, method_name)
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if "user_id" in parameters or accepts_kwargs:
            kwargs["user_id"] = self.user_id
        supported = {
            key: value
            for key, value in kwargs.items()
            if key in parameters or accepts_kwargs
        }
        return method(*args, **supported)

    # ─── 三路 fetch（统一 try/except，失败 → ok=False） ──────────────────

    def _fetch_milvus(self, query: str, fetch_k: int) -> _PathHits:
        if not self._milvus_ok():
            return _PathHits(ok=False)
        if self._embed_fn is None:
            logger.warning("⚠️  embed_fn 未注入，跳过 Milvus 语义路")
            return _PathHits(ok=False)
        try:
            query_emb = self._embed_fn(query)
        except Exception as e:
            logger.warning("⚠️  查询向量化失败: %s", e)
            return _PathHits(ok=False, reason="embedding_failed")
        if not query_emb:
            return _PathHits(ok=False, reason="embedding_empty")
        # 维度不匹配时跳过（与 rag.py 行为一致），避免 Milvus 服务端报错
        if self.cfg.rag_milvus_dim and len(query_emb) != self.cfg.rag_milvus_dim:
            logger.warning(
                "⚠️  embedding 维度 %d 与 rag_milvus_dim=%d 不匹配，跳过语义路",
                len(query_emb), self.cfg.rag_milvus_dim,
            )
            return _PathHits(ok=False, reason="embedding_dimension_mismatch")
        if not self._milvus_circuit.allow_request():
            return _PathHits(
                ok=False,
                reason="circuit_open",
                circuit=self._milvus_circuit.snapshot().to_dict(),
            )
        try:
            hits = self._repo_call("search_milvus_dicts", query_emb, fetch_k) or []
            self._milvus_circuit.record_success()
            return _PathHits(
                hits=hits,
                ok=True,
                circuit=self._milvus_circuit.snapshot().to_dict(),
            )
        except Exception as e:
            self._milvus_circuit.record_failure()
            logger.warning("⚠️  Milvus 检索失败: %s", e)
            return _PathHits(
                ok=False,
                reason="backend_error",
                circuit=self._milvus_circuit.snapshot().to_dict(),
            )

    def _fetch_es(self, query: str, fetch_k: int) -> _PathHits:
        if not self._es_ok():
            return _PathHits(ok=False)
        if not self._es_circuit.allow_request():
            return _PathHits(
                ok=False,
                reason="circuit_open",
                circuit=self._es_circuit.snapshot().to_dict(),
            )
        try:
            hits = self._repo_call("search_es_dicts", query, fetch_k) or []
            self._es_circuit.record_success()
            return _PathHits(hits=hits, ok=True, circuit=self._es_circuit.snapshot().to_dict())
        except Exception as e:
            self._es_circuit.record_failure()
            logger.warning("⚠️  ES 检索失败: %s", e)
            return _PathHits(
                ok=False,
                reason="backend_error",
                circuit=self._es_circuit.snapshot().to_dict(),
            )

    def _fetch_kg(self, query: str, fetch_k: int) -> _PathHits:
        if not self._kg_ok():
            return _PathHits(ok=False)
        if not self._kg_circuit.allow_request():
            return _PathHits(
                ok=False,
                reason="circuit_open",
                circuit=self._kg_circuit.snapshot().to_dict(),
            )
        try:
            hits = self._kg.search(query, fetch_k) or []
            self._kg_circuit.record_success()
            return _PathHits(hits=hits, ok=True, circuit=self._kg_circuit.snapshot().to_dict())
        except Exception as e:
            self._kg_circuit.record_failure()
            logger.warning("⚠️  KG 检索失败: %s", e)
            return _PathHits(
                ok=False,
                reason="backend_error",
                circuit=self._kg_circuit.snapshot().to_dict(),
            )

    # ─── 权重归一 ─────────────────────────────────────────────────────────


def _trace_path(path: _PathHits) -> dict:
    hits = []
    for rank, hit in enumerate(path.hits or [], start=1):
        if isinstance(hit, dict):
            pg_id = hit.get("pg_id")
            score = hit.get("score")
            content = hit.get("content") or ""
        else:
            pg_id = getattr(hit, "pg_id", 0)
            score = getattr(hit, "score", None)
            content = getattr(hit, "content", "") or ""
        item = {"rank": rank, "pg_id": pg_id}
        if score is not None:
            try:
                numeric_score = float(score)
                if math.isfinite(numeric_score):
                    item["score"] = numeric_score
            except (TypeError, ValueError):
                pass
        if content:
            item["content_preview"] = str(content)[:200]
        hits.append(item)
    result = {"ok": bool(path.ok), "hits": hits}
    if path.reason:
        result["reason"] = path.reason
    if path.circuit is not None:
        result["circuit"] = path.circuit
    return result


def _trace_results(results: List[HybridResult]) -> List[dict]:
    traced: List[dict] = []
    for rank, result in enumerate(results or [], start=1):
        item = {
            "rank": rank,
            "pg_id": result.pg_id,
            "source": result.source,
            "content_preview": (result.parent or result.content or "")[:200],
        }
        try:
            score = float(result.score)
            if math.isfinite(score):
                item["score"] = score
        except (TypeError, ValueError):
            pass
        traced.append(item)
    return traced
