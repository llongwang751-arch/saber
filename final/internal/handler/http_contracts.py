"""Public HTTP serialization; private runtime strategy details stay server-side."""

import logging
import hashlib
import json
import math
from typing import Any, Dict


from internal.agent.agent import Response


logger = logging.getLogger(__name__)


def _response_to_dict(resp: Response) -> Dict[str, Any]:
    return {
        "query": resp.query,
        "answer": resp.answer,
        "mode": resp.mode,
        "intent": resp.intent,
        "slots": resp.slots,
        "fallback": resp.fallback,
        "error": resp.error,
        "steps": [
            {
                "type": s.type,
                "content": s.content,
                "tool": s.tool,
                "params": s.params,
            }
            for s in resp.steps
        ],
        "tool_call": resp.tool_call,
        "tool_calls": resp.tool_calls,
        "search_results": [_rag_result_to_main_contract(r) for r in resp.search_results],
        "rag_trace": resp.rag_trace,
        "task": resp.task,
        "extracted_info": resp.extracted_info,
        "short_term_count": resp.short_term_count,
        "long_term_count": resp.long_term_count,
        "preferences": resp.preferences,
        "interrupted": resp.interrupted,
        "trace_id": resp.trace_id,
        "success": not bool(resp.error),
    }


def _rag_result_to_main_contract(result: Dict[str, Any]) -> Dict[str, Any]:
    content = result.get("content", "")
    score = result.get("score", result.get("similarity", 0.0))
    try:
        similarity = float(score)
    except Exception:
        similarity = 0.0
    if not math.isfinite(similarity):
        similarity = 0.0
    return {
        **result,
        "content": content,
        "score": similarity,
        "similarity": similarity,
        "chunk": result.get("chunk") or {"content": content},
        "source": result.get("source", "unknown"),
    }


def _tool_to_main_contract(tool: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 Go main 分支前端：工具参数字段叫 params。"""
    params = tool.get("params")
    if params is None:
        params = tool.get("parameters", [])
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", tool.get("desc", "")),
        "is_mcp": tool.get("is_mcp", False),
        "params": params or [],
    }


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _jsonable(value: Any) -> Any:
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _jsonable(asdict(value))
    return value


def _sanitize_stream_done(
    data: Any,
) -> dict[str, Any]:
    """Drop internal runtime bookkeeping keys from the public done event."""

    payload = dict(data) if isinstance(data, dict) else {}
    payload.pop("experiment_exposure_id", None)
    payload.pop("runtime_strategy_checksum", None)
    return payload


def _normalize_ingest_result(
    result: Any, text: str, document_id: str = "", version_id: str = "", section: str = ""
) -> Dict[str, Any]:
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""
    if isinstance(result, tuple):
        chunk_count = result[0] if len(result) > 0 else 0
        if len(result) > 1 and result[1]:
            doc_hash = result[1]
    elif isinstance(result, dict):
        out = dict(result)
        out.setdefault("chunk_count", 0)
        out.setdefault("doc_hash", doc_hash)
        if document_id:
            out.setdefault("document_id", document_id)
        if version_id:
            out.setdefault("version_id", version_id)
        if section:
            out.setdefault("section", section)
        return out
    else:
        chunk_count = result or 0
    return {
        "chunk_count": int(chunk_count or 0),
        "parent_count": 0,
        "indexed_count": int(chunk_count or 0),
        "doc_hash": doc_hash,
        "document_id": document_id,
        "version_id": version_id,
        "section": section,
    }
