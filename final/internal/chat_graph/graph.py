"""StateGraph wiring for the LangGraph chat engine.

START → policy → prepare → conditional route → {react, research, rag, chat} →
finalize → END, with a ``cancel`` branch for turns aborted after routing but
before mode dispatch. ``host`` supplies the node implementations; see
engine.ChatGraphEngine. The compiled graph is request-scoped: no checkpointer
by default, mirroring the native turn flow's single in-flight turn semantics.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .state import ChatGraphState

MODE_NODES = ("react", "research", "rag", "chat")


def build_chat_graph(host, *, checkpointer=None):
    graph = StateGraph(ChatGraphState)
    graph.add_node("policy", host.policy_node)
    graph.add_node("prepare", host.prepare_node)
    for mode in MODE_NODES:
        graph.add_node(mode, getattr(host, f"{mode}_node"))
    graph.add_node("cancel", host.cancel_node)
    graph.add_node("finalize", host.finalize_node)
    graph.add_edge(START, "policy")
    graph.add_conditional_edges("policy", host.after_policy, ["prepare", END])
    graph.add_conditional_edges(
        "prepare", host.after_prepare, [*MODE_NODES, "cancel", END]
    )
    for mode in MODE_NODES:
        graph.add_edge(mode, "finalize")
    graph.add_edge("cancel", END)
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()
