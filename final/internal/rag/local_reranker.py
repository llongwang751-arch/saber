"""Dependency-free local fallback reranker.

This is intentionally not advertised as a Cross-Encoder.  It provides a
deterministic, zero-network quality fallback based on token and character
n-gram overlap when the remote LLM reranker is unavailable.
"""

from __future__ import annotations

import math
import re
from typing import Callable, List, Optional


class LocalOverlapReranker:
    """Rerank candidates locally without model weights or remote calls."""

    def rerank(self, query: str, results: List, top_k: int) -> List:
        if not results:
            return []
        query_features = _features(query)
        ordered = []
        for index, result in enumerate(results):
            content = _content(result)
            local_score = _overlap_score(query, query_features, content)
            previous = _finite_score(getattr(result, "score", 0.0))
            ordered.append((local_score, previous, -index, result))
        ordered.sort(key=lambda item: item[:3], reverse=True)
        output = []
        for local_score, _previous, _index, result in ordered:
            result.score = local_score
            source = str(getattr(result, "source", "") or "")
            if "+local_rerank" not in source:
                result.source = f"{source}+local_rerank"
            output.append(result)
        return output[:top_k] if top_k > 0 else output


class LocalCrossEncoderReranker:
    """Lazy optional Cross-Encoder with a dependency-free fallback.

    `sentence-transformers` and model weights are loaded only on the first
    fallback request.  Production deployments can preload the configured
    model; local/offline installs keep working without the extra dependency.
    """

    def __init__(
        self,
        model_name: str,
        *,
        fallback_reranker=None,
        model_loader: Optional[Callable[[str], object]] = None,
    ):
        self.model_name = str(model_name or "").strip()
        self._fallback = fallback_reranker
        self._model_loader = model_loader or _load_cross_encoder
        self._model = None

    def rerank(self, query: str, results: List, top_k: int) -> List:
        if not results:
            return []
        try:
            model = self._get_model()
            raw_scores = list(model.predict([(query, _content(result)) for result in results]))
            if len(raw_scores) != len(results):
                raise ValueError("cross encoder score count mismatch")
            scored = []
            for index, (result, raw_score) in enumerate(zip(results, raw_scores)):
                score = float(raw_score)
                if not math.isfinite(score):
                    raise ValueError("cross encoder returned non-finite score")
                scored.append((score, _finite_score(getattr(result, "score", 0.0)), -index, result))
            scored.sort(key=lambda item: item[:3], reverse=True)
            output = []
            for raw_score, _previous, _index, result in scored:
                result.score = _normalise_model_score(raw_score)
                source = str(getattr(result, "source", "") or "")
                if "+cross_encoder" not in source:
                    result.source = f"{source}+cross_encoder"
                output.append(result)
            return output[:top_k] if top_k > 0 else output
        except Exception:
            if self._fallback is None:
                raise
            return self._fallback.rerank(query, results, top_k)

    def _get_model(self):
        if self._model is None:
            if not self.model_name:
                raise ValueError("cross encoder model is not configured")
            self._model = self._model_loader(self.model_name)
        return self._model


def _load_cross_encoder(model_name: str):
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise RuntimeError(
            "Cross-Encoder fallback requires the optional sentence-transformers package"
        ) from exc
    return CrossEncoder(model_name)


def _normalise_model_score(score: float) -> float:
    if 0.0 <= score <= 1.0:
        return score
    if score >= 50:
        return 1.0
    if score <= -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-score))


def _content(result) -> str:
    if hasattr(result, "content"):
        return str(result.content or "")
    chunk = getattr(result, "chunk", None)
    return str(getattr(chunk, "content", "") or "")


def _features(text: str) -> set[str]:
    # Keep word boundaries: removing whitespace turns "Python 3.10" into
    # "python3.10" and silently discards a valid Python evidence match.
    normalised = str(text or "").casefold()
    tokens = {token.strip(".:/-") for token in re.findall(r"[a-z0-9_.:/-]+", normalised)}
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalised))
    tokens.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
    if len(cjk) == 1:
        tokens.add(cjk)
    return {token for token in tokens if token}


def _overlap_score(query: str, query_features: set[str], content: str) -> float:
    if not query_features or not content:
        return 0.0
    content_features = _features(content)
    coverage = len(query_features & content_features) / len(query_features)
    query_text = re.sub(r"\s+", "", str(query or "").casefold())
    content_text = re.sub(r"\s+", "", content.casefold())
    exact_bonus = 0.15 if query_text and query_text in content_text else 0.0
    return min(1.0, coverage * 0.85 + exact_bonus)


def _finite_score(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0
