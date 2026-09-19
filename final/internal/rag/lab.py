"""RAG 流程实验台。

这个模块复用正式 RAG 的父子分块器、Embedding 客户端和答案生成函数，
但所有向量与检索结果都只保存在当前请求内，不写 PostgreSQL、Milvus、
Elasticsearch 或 Neo4j，避免教学演示污染用户知识库。
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Sequence

from internal.rag.splitter import RecursiveSplitter


LAB_MAX_CHUNKS = 80
LAB_CONTEXT_CHARS = 8_000
LAB_VECTOR_PREVIEW = 8


def run_rag_lab(agent: Any, document: str, query: str, top_k: int = 5) -> Dict[str, Any]:
    """在内存里运行一次可解释的 RAG 五阶段流程。"""

    started = time.perf_counter()
    document = (document or "").strip()
    query = (query or "").strip()
    top_k = max(1, min(int(top_k or 5), 10))
    if not document:
        raise ValueError("实验文档不能为空")
    if not query:
        raise ValueError("查询问题不能为空")

    warnings: List[str] = []
    timings: Dict[str, float] = {}
    rag = getattr(agent, "rag", None)
    parent_splitter = getattr(rag, "parent_splitter", None) or RecursiveSplitter(800, 100)
    child_splitter = getattr(rag, "child_splitter", None) or RecursiveSplitter(200, 50)

    # 1. 文档切分：完全复用正式 RAG 的 parent -> child 规则。
    stage_started = time.perf_counter()
    parent_chunks = parent_splitter.split(document)
    parents: List[Dict[str, Any]] = []
    children: List[Dict[str, Any]] = []
    for parent_index, parent in enumerate(parent_chunks):
        child_ids: List[int] = []
        for child in child_splitter.split(parent.content):
            if len(children) >= LAB_MAX_CHUNKS:
                break
            child_id = len(children)
            child_ids.append(child_id)
            children.append(
                {
                    "id": child_id,
                    "parent_id": parent_index,
                    "content": child.content,
                    "char_count": len(child.content),
                }
            )
        parents.append(
            {
                "id": parent_index,
                "content": parent.content,
                "char_count": len(parent.content),
                "child_ids": child_ids,
            }
        )
        if len(children) >= LAB_MAX_CHUNKS:
            break
    if not children:
        raise ValueError("文档切分后没有得到有效子块")
    if sum(len(child_splitter.split(parent.content)) for parent in parent_chunks) > LAB_MAX_CHUNKS:
        warnings.append(f"实验台最多向量化 {LAB_MAX_CHUNKS} 个子块，本次已截断；正式入库不受此限制。")
    timings["split_ms"] = _elapsed_ms(stage_started)

    # 2. 查询和文档向量化。优先使用项目配置的真实 Embedding；失败时使用
    # 明确标记的本地哈希向量，让页面在离线环境仍能演示完整算法。
    stage_started = time.perf_counter()
    texts = [query] + [item["content"] for item in children]
    vectors: List[List[float]]
    embedding_mode = "remote_embedding"
    embedding_label = "项目 Embedding API"
    llm = getattr(rag, "_llm", None)
    try:
        if llm is None:
            raise RuntimeError("RAG Embedding 客户端不可用")
        embed_batch = getattr(llm, "embed_batch", None)
        if callable(embed_batch):
            vectors = embed_batch(texts)
        else:
            embed_one = getattr(llm, "embed", None)
            if not callable(embed_one):
                raise RuntimeError("Embedding 方法不可用")
            vectors = [embed_one(text) for text in texts]
        if len(vectors) != len(texts) or any(not vector for vector in vectors):
            raise RuntimeError("Embedding 返回数量不匹配或包含空向量")
    except Exception as exc:
        embedding_mode = "local_hash_fallback"
        embedding_label = "本地哈希向量（降级演示）"
        warnings.append(f"真实 Embedding 不可用，已回落到本地哈希向量：{exc}")
        vectors = [_local_hash_embedding(text) for text in texts]
    timings["embedding_ms"] = _elapsed_ms(stage_started)

    query_vector = vectors[0]
    child_vectors = vectors[1:]
    query_norm = _norm(query_vector)

    # 3. 子块向量召回：余弦相似度排序，小块命中后保留父块用于下一阶段。
    stage_started = time.perf_counter()
    ranked: List[Dict[str, Any]] = []
    for child, vector in zip(children, child_vectors):
        ranked.append(
            {
                **child,
                "score": _cosine(query_vector, vector),
                "vector_dimension": len(vector),
                "vector_norm": _round(_norm(vector), 6),
                "vector_preview": [_round(value, 5) for value in vector[:LAB_VECTOR_PREVIEW]],
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    candidates = ranked[: min(top_k, len(ranked))]
    for rank, item in enumerate(candidates, start=1):
        item["rank"] = rank
        item["score"] = _round(item["score"], 6)
        parent = parents[item["parent_id"]]
        item["parent_content"] = parent["content"]
        item["parent_char_count"] = parent["char_count"]
    timings["retrieval_ms"] = _elapsed_ms(stage_started)

    # 4. 小块召回、父块补全、去重、按字符预算组装增强 Prompt。
    stage_started = time.perf_counter()
    context_parts: List[str] = []
    seen_parents = set()
    context_chars = 0
    for candidate in candidates:
        parent_id = candidate["parent_id"]
        if parent_id in seen_parents:
            continue
        parent_content = candidate["parent_content"].strip()
        if not parent_content:
            continue
        remaining = LAB_CONTEXT_CHARS - context_chars
        if remaining <= 0:
            break
        if len(parent_content) > remaining:
            parent_content = parent_content[:remaining]
            warnings.append("增强上下文达到实验台字符预算，末尾父块已截断。")
        context_parts.append(f"[证据 {len(context_parts) + 1}｜父块 {parent_id}]\n{parent_content}")
        seen_parents.add(parent_id)
        context_chars += len(parent_content)

    context = "\n\n".join(context_parts)
    system_prompt = (
        "你是一个基于知识库回答问题的助手。请仅根据提供的上下文内容回答问题，"
        "不要编造信息。如果上下文不足以回答，请明确说明。"
    )
    user_prompt = f"上下文：\n{context}\n\n问题：{query}"
    timings["prompt_ms"] = _elapsed_ms(stage_started)

    # 5. 复用正式 RAG 注入的生成函数。没有模型时仍返回可验证的检索结果，
    # 但不伪造“模型已经生成答案”。
    stage_started = time.perf_counter()
    generate_fn = getattr(rag, "_generate_fn", None)
    generation_mode = "configured_llm"
    if callable(generate_fn):
        try:
            answer = str(generate_fn(system_prompt, user_prompt) or "")
        except Exception as exc:
            generation_mode = "generation_failed"
            warnings.append(f"答案生成失败：{exc}")
            answer = "答案生成失败，但前四个 RAG 阶段已经完成，请根据检索证据排查模型配置。"
    else:
        generation_mode = "context_only"
        warnings.append("当前 Agent 未注入答案生成函数，实验台只展示检索与增强 Prompt。")
        answer = "当前未配置答案生成模型；请查看上方增强 Prompt 与召回证据。"
    timings["generation_ms"] = _elapsed_ms(stage_started)
    timings["total_ms"] = _elapsed_ms(started)

    vector_dimension = len(query_vector)
    stages = [
        {
            "id": "split",
            "index": 1,
            "name": "文档切分",
            "summary": f"{len(parents)} 个父块 / {len(children)} 个子块",
            "duration_ms": timings["split_ms"],
        },
        {
            "id": "query",
            "index": 2,
            "name": "用户查询",
            "summary": f"向量维度 {vector_dimension}",
            "duration_ms": timings["embedding_ms"],
        },
        {
            "id": "retrieve",
            "index": 3,
            "name": "向量检索",
            "summary": f"召回 Top {len(candidates)}",
            "duration_ms": timings["retrieval_ms"],
        },
        {
            "id": "augment",
            "index": 4,
            "name": "提示增强",
            "summary": f"补全 {len(context_parts)} 个父块",
            "duration_ms": timings["prompt_ms"],
        },
        {
            "id": "generate",
            "index": 5,
            "name": "答案生成",
            "summary": "已完成" if generation_mode == "configured_llm" else "降级完成",
            "duration_ms": timings["generation_ms"],
        },
    ]

    return {
        "run_id": f"raglab_{uuid.uuid4().hex[:12]}",
        "stages": stages,
        "document": {
            "char_count": len(document),
            "parent_count": len(parents),
            "child_count": len(children),
            "parent_chunk_size": getattr(parent_splitter, "chunk_size", None),
            "child_chunk_size": getattr(child_splitter, "chunk_size", None),
            "child_overlap": getattr(child_splitter, "chunk_overlap", None),
        },
        "parents": parents,
        "chunks": children,
        "query": {
            "text": query,
            "embedding_mode": embedding_mode,
            "embedding_label": embedding_label,
            "vector_dimension": vector_dimension,
            "vector_norm": _round(query_norm, 6),
            "vector_preview": [_round(value, 5) for value in query_vector[:LAB_VECTOR_PREVIEW]],
        },
        "retrieval": {
            "metric": "cosine_similarity",
            "top_k": top_k,
            "candidate_count": len(ranked),
            "results": candidates,
        },
        "prompt": {
            "system": system_prompt,
            "user": user_prompt,
            "context": context,
            "context_char_count": len(context),
            "estimated_tokens": max(1, math.ceil(len(user_prompt) / 2)),
            "parent_count": len(context_parts),
        },
        "answer": answer,
        "generation_mode": generation_mode,
        "warnings": warnings,
        "timings": timings,
    }


def _local_hash_embedding(text: str, dimension: int = 256) -> List[float]:
    """稳定、无外部依赖的字符 n-gram 哈希向量，仅用于离线演示降级。"""

    cleaned = re.sub(r"\s+", "", (text or "").lower())
    features: List[str] = []
    features.extend(cleaned)
    features.extend(cleaned[index : index + 2] for index in range(max(0, len(cleaned) - 1)))
    features.extend(re.findall(r"[a-z0-9_\-.]+", (text or "").lower()))
    vector = [0.0] * dimension
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    length = _norm(vector)
    if length > 0:
        vector = [value / length for value in vector]
    return vector


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = _norm(left) * _norm(right)
    if denominator <= 0:
        return 0.0
    score = sum(float(a) * float(b) for a, b in zip(left, right)) / denominator
    return score if math.isfinite(score) else 0.0


def _norm(vector: Iterable[float]) -> float:
    value = math.sqrt(sum(float(item) * float(item) for item in vector))
    return value if math.isfinite(value) else 0.0


def _elapsed_ms(started: float) -> float:
    return _round((time.perf_counter() - started) * 1000.0, 2)


def _round(value: float, digits: int) -> float:
    value = float(value)
    return round(value, digits) if math.isfinite(value) else 0.0
