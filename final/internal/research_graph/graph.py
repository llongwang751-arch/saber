"""StateGraph wiring for the LangGraph research engine.

plan → interrupt() review → Send fan-out research branches → code → report → END.
``host`` supplies the node implementations; see engine.LangGraphResearchEngine.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .state import ResearchGraphState


def build_research_graph(host, *, checkpointer):
    graph = StateGraph(ResearchGraphState)
    graph.add_node("plan", host.plan_node)
    graph.add_node("review", host.review_node)
    graph.add_node("dispatch", host.dispatch_node)
    graph.add_node("research", host.research_node)
    graph.add_node("code", host.code_node)
    graph.add_node("report", host.report_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "review")
    graph.add_conditional_edges("review", host.after_review, ["dispatch", END])
    # One Send branch per ready research step; fan-in re-enters dispatch for the
    # next wave, or falls through to code/report when research is exhausted.
    graph.add_conditional_edges("dispatch", host.fan_out, ["research", "code", "report", END])
    graph.add_edge("research", "dispatch")
    graph.add_edge("code", "report")
    graph.add_conditional_edges("report", host.after_report, ["dispatch", END])
    return graph.compile(checkpointer=checkpointer)
