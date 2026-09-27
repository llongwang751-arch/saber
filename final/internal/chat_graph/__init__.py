"""LangGraph chat orchestration (``chat.engine: langgraph | native``).

The graph orchestrates one chat turn — policy gate → prepare/route (the
verbatim two-level intent funnel) → react / research(rag_agent) / rag / chat
branches → finalize → done — while every node delegates to the same agent
services the retired ``internal/agent/turn_service.py`` used, so budget
charging, harness policy, memory writes and the SSE event vocabulary/order are
byte-identical between engines. ``native.py`` keeps the pre-graph turn flow as
the selectable reference implementation and the parity baseline.
"""

from .engine import ChatGraphEngine, dispatch, selected_engine
from .native import (
    dispatch_mode,
    dispatch_once,
    prepare,
    report_intent,
    report_intent_refined,
    route_decide,
)

__all__ = [
    "ChatGraphEngine",
    "dispatch",
    "selected_engine",
    "dispatch_mode",
    "dispatch_once",
    "prepare",
    "report_intent",
    "report_intent_refined",
    "route_decide",
]
