"""Remote API Reranker for SiliconFlow and OpenAI-compatible rerank endpoints."""

from __future__ import annotations

import logging
from typing import List, Optional
import requests

from internal.rag.local_reranker import LocalOverlapReranker, _content

logger = logging.getLogger(__name__)


class RemoteAPIReranker:
    """Reranker using SiliconFlow or compatible remote Rerank API with local fallback."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str = "BAAI/bge-reranker-v2-m3",
        timeout: float = 10.0,
        fallback_reranker: Optional[object] = None,
    ):
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout
        self.fallback = fallback_reranker or LocalOverlapReranker()

    def rerank(self, query: str, results: List, top_k: int = 3) -> List:
        if not results:
            return []

        if not self.api_url or not self.api_key:
            return self.fallback.rerank(query, results, top_k)

        docs = [_content(r) for r in results]
        payload = {
            "model": self.model,
            "query": query,
            "documents": docs,
            "top_n": min(len(results), top_k) if top_k > 0 else len(results),
            "return_documents": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(self.api_url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("Remote rerank failed with status %d: %s, falling back to local", resp.status_code, resp.text)
                return self.fallback.rerank(query, results, top_k)

            data = resp.json()
            items = data.get("results") or []
            if not items:
                return self.fallback.rerank(query, results, top_k)

            output = []
            for item in items:
                idx = item.get("index", 0)
                rel_score = float(item.get("relevance_score", 0.0))
                if idx < len(results):
                    res = results[idx]
                    res.score = rel_score
                    source = str(getattr(res, "source", "") or "")
                    if "+remote_rerank" not in source:
                        res.source = f"{source}+remote_rerank"
                    output.append(res)

            return output[:top_k] if top_k > 0 else output
        except Exception as e:
            logger.warning("Remote rerank request exception: %s, falling back to local", e)
            return self.fallback.rerank(query, results, top_k)
