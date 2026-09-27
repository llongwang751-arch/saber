"""LangGraph research orchestration backend (``research.engine: langgraph``).

The graph (plan → interrupt() review → Send parallel research → code → report)
reuses the native engine's providers, budget, source ledger and citation
validation inside its nodes; the run ledger and SSE event vocabulary are shared
with the native backend unchanged.
"""

from .adapter import EVENT_VOCABULARY, EventBridge, review_decision
from .engine import LangGraphResearchEngine, default_checkpointer
from .graph import build_research_graph
from .state import ResearchGraphState, pending_steps, ready_steps

__all__ = [
    "EVENT_VOCABULARY",
    "EventBridge",
    "LangGraphResearchEngine",
    "ResearchGraphState",
    "build_research_graph",
    "default_checkpointer",
    "pending_steps",
    "ready_steps",
    "review_decision",
]
