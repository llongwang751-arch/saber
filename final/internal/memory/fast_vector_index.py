"""Lightweight in-memory vector index with matrix acceleration.

Eliminates the O(N) pure-Python loop bottleneck in LongTerm memory recall.
Utilizes NumPy vectorization when available, with a resilient fallback.
"""

from __future__ import annotations

import heapq
import math
from typing import Any, List, Optional, Tuple

try:
    import numpy as np  # type: ignore
    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    _HAS_NUMPY = False


class FastVectorIndex:
    """High-performance in-memory vector index for lightweight Agent memory & RAG recall."""

    def __init__(self, dim: Optional[int] = None, metric: str = "cosine"):
        self.dim = dim
        self.metric = metric.lower()
        self.ids: List[str] = []
        self.vectors: List[List[float]] = []
        self.metadatas: List[Optional[dict]] = []

    def __len__(self) -> int:
        return len(self.ids)

    def add(self, item_id: str, vector: List[float], metadata: Optional[dict] = None) -> None:
        if self.dim is not None and len(vector) != self.dim:
            raise ValueError(f"Vector dimension mismatch: expected {self.dim}, got {len(vector)}")
        self.ids.append(item_id)
        self.vectors.append(vector)
        self.metadatas.append(metadata or {})

    def search(self, query_emb: List[float], top_k: int = 5) -> List[Tuple[str, float, Optional[dict]]]:
        """Search nearest items by cosine similarity or dot product."""
        if not self.ids or not query_emb:
            return []

        if _HAS_NUMPY:
            try:
                q = np.array(query_emb, dtype=np.float32)
                mat = np.array(self.vectors, dtype=np.float32)
                if self.metric == "cosine":
                    q_norm = np.linalg.norm(q)
                    if q_norm == 0:
                        return []
                    q = q / q_norm
                    norms = np.linalg.norm(mat, axis=1, keepdims=True)
                    norms[norms == 0] = 1e-9
                    mat = mat / norms

                sims = np.dot(mat, q)
                # Get top_k indices
                actual_k = min(top_k, len(self.ids))
                top_indices = np.argsort(sims)[::-1][:actual_k]
                return [(self.ids[idx], float(sims[idx]), self.metadatas[idx]) for idx in top_indices]
            except Exception:
                pass

        # Pure python fallback
        q_norm = math.sqrt(sum(x * x for x in query_emb)) if self.metric == "cosine" else 1.0
        scores: List[Tuple[float, int]] = []
        for idx, vec in enumerate(self.vectors):
            dot = sum(a * b for a, b in zip(query_emb, vec))
            if self.metric == "cosine":
                v_norm = math.sqrt(sum(x * x for x in vec))
                sim = dot / (q_norm * v_norm) if (q_norm * v_norm) > 0 else 0.0
            else:
                sim = dot
            scores.append((sim, idx))

        scores.sort(key=lambda x: x[0], reverse=True)
        return [(self.ids[idx], score, self.metadatas[idx]) for score, idx in scores[:top_k]]

    @staticmethod
    def search_top_k(
        query_emb: List[float],
        items: List[Any],
        top_k: int = 3,
        threshold: float = 0.4,
        semantic_weight: float = 0.7,
        importance_weight: float = 0.3,
    ) -> List[Tuple[Any, float]]:
        """Compute cosine similarity and importance score in batch, returning sorted Top-K items."""
        if not query_emb or not items or top_k <= 0:
            return []

        # Filter active candidate items with valid embeddings
        valid_items: List[Any] = []
        valid_embs: List[List[float]] = []
        importances: List[float] = []

        q_dim = len(query_emb)
        for item in items:
            if getattr(item, "status", "active") != "active":
                continue
            emb = getattr(item, "embedding", None)
            if emb and len(emb) == q_dim:
                valid_items.append(item)
                valid_embs.append(emb)
                importances.append(float(getattr(item, "importance", 0.5) or 0.5))

        if not valid_items:
            return []

        if _HAS_NUMPY:
            try:
                # 1. NumPy 矩阵向量化批量计算
                q = np.array(query_emb, dtype=np.float32)
                q_norm = np.linalg.norm(q)
                if q_norm == 0:
                    return []
                q_unit = q / q_norm

                mat = np.array(valid_embs, dtype=np.float32)
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                norms[norms == 0] = 1e-9
                mat_unit = mat / norms

                # Cosine similarities: shape (N,)
                sims = np.dot(mat_unit, q_unit)
                imps = np.array(importances, dtype=np.float32)

                # Composite score
                scores = (sims * semantic_weight) + (imps * importance_weight)

                # Top-K filtering
                results: List[Tuple[Any, float]] = []
                for idx, sc in enumerate(scores):
                    score_val = float(sc)
                    if score_val >= threshold:
                        results.append((valid_items[idx], score_val))

                results.sort(key=lambda x: x[1], reverse=True)
                return results[:top_k]
            except Exception:
                pass  # Fall back to optimized pure-Python if array conversion fails

        # 2. Optimized pure-Python fallback (pre-computes query norm once)
        q_norm = math.sqrt(sum(x * x for x in query_emb))
        if q_norm == 0:
            return []

        candidates: List[Tuple[float, Any]] = []
        for idx, item in enumerate(valid_items):
            emb = valid_embs[idx]
            dot = sum(a * b for a, b in zip(query_emb, emb))
            norm = math.sqrt(sum(x * x for x in emb))
            if norm == 0:
                continue
            sim = dot / (q_norm * norm)
            score = sim * semantic_weight + importances[idx] * importance_weight
            if score >= threshold:
                candidates.append((score, item))

        top_candidates = heapq.nlargest(top_k, candidates, key=lambda x: x[0])
        return [(item, score) for score, item in top_candidates]
