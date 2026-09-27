"""Retrieval service for an explicitly supplied conversation runtime.

The caller owns mutable state and lifecycle; services do not retain a user or conversation.
"""

import inspect
import logging
from typing import Any, Dict, List, Optional

from internal.rag.rewriter import HistoryMessage


logger = logging.getLogger(__name__)


def rag_ingest(agent, document: str) -> int:
    if agent.rag is None:
        return 0
    return agent.rag.ingest(document)


def rag_query(agent, question: str) -> tuple:
    if agent.rag is None:
        return ("RAG 不可用", [])
    return agent.rag.query(question)


def recent_history_for_rag(agent) -> List[HistoryMessage]:
    msgs = []
    rolling_summary = getattr(agent, "_rolling_summary", "")
    if rolling_summary:
        msgs.append(HistoryMessage(role="system", content=f"【前情提要与历史背景摘要】\n{rolling_summary}"))
    stm_msgs = [HistoryMessage(role=m["role"], content=m["content"]) for m in agent.stm.get()]
    msgs.extend(stm_msgs[-5:] if rolling_summary else stm_msgs[-6:])
    return msgs


def run_rag_query(agent, query: str):
    answer, results, _trace = agent._run_rag_query_with_trace(query)
    return answer, results


def run_rag_query_with_trace(agent, query: str, *, runtime_overrides: Optional[Dict[str, Any]] = None):
    if agent.rag is None:
        return (
            "RAG 不可用",
            [],
            {
                "original_query": query,
                "decision": "unavailable",
                "reason": "rag_engine_unavailable",
            },
        )
    if hasattr(agent.rag, "query_with_history_trace"):
        method = agent.rag.query_with_history_trace
        overrides = dict(runtime_overrides or {})
        if overrides:
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                parameters = {}
            accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
            if "runtime_overrides" in parameters or accepts_kwargs:
                return method(
                    query,
                    agent._recent_history_for_rag(),
                    runtime_overrides=overrides,
                )
            raise RuntimeError("RAG engine cannot apply the compiled request-scoped strategy")
        return method(query, agent._recent_history_for_rag())
    if hasattr(agent.rag, "query_with_history"):
        answer, results = agent.rag.query_with_history(query, agent._recent_history_for_rag())
    else:
        answer, results = agent.rag.query(query)
    return (
        answer,
        results,
        {
            "original_query": query,
            "decision": "answer" if results else "no_answer",
            "reason": "legacy_rag_engine",
        },
    )
