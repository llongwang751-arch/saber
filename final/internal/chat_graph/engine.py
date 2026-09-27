"""LangGraph execution backend for a chat turn.

The graph orchestrates; the existing agent services execute. Every node
delegates to the very same agent methods the native turn service called
(``_prepare`` / ``_run_react_with_tools`` / ``_run_rag_query_with_trace`` /
``_chat_response`` / ``_finalize``), so memory writes, the two-level intent
funnel, the forced subagent research plan, budget charging, harness policy and
the SSE event vocabulary/order stay identical to the native backend. This class
owns only orchestration: the policy gate, routing and the cancel checkpoints.

``dispatch`` is the single engine entry shared with the native backend:
``chat.engine: langgraph`` (default) runs this graph, ``native`` runs
:mod:`internal.chat_graph.native` verbatim.
"""

from __future__ import annotations

import uuid
from typing import Optional

from langgraph.graph import END

from internal.agent.contracts import ChatOptions, RequestExecutionContext, Response, StepType
from internal.agent.serialization import _emit, _to_jsonable

from . import native
from .graph import build_chat_graph

ENGINES = ("native", "langgraph")
_DEFAULT_ENGINE = "langgraph"

# Native ``dispatch_mode`` mode → graph node name ("rag_agent" is the research
# branch: the same forced subagent plan → TaskGraph machinery, unweakened).
MODE_NODE = {"react": "react", "rag_agent": "research", "rag": "rag"}


def selected_engine(agent) -> str:
    """``chat.engine`` of the agent's config; langgraph when unset (default)."""
    return str(getattr(getattr(agent, "cfg", None), "chat_engine", "") or _DEFAULT_ENGINE)


def dispatch(agent, query, opts, token, on_event=None, execution_context=None):
    """One chat turn through the configured engine behind one interface."""
    if selected_engine(agent) == "native":
        return native.dispatch(agent, query, opts, token, on_event, execution_context)
    return ChatGraphEngine(agent).dispatch(query, opts, token, on_event, execution_context)


class ChatGraphEngine:
    """Same ``dispatch`` surface as the native turn service; LangGraph inside."""

    def __init__(self, agent, *, checkpointer=None):
        self.agent = agent
        self._checkpointer = checkpointer
        self._graph = build_chat_graph(self, checkpointer=checkpointer)
        self._opts: Optional[ChatOptions] = None
        self._token = None
        self._on_event = None
        self._execution_context: Optional[RequestExecutionContext] = None
        self._pr: Optional[dict] = None
        self._resp: Optional[Response] = None

    # ------------------------------------------------------------------ runs

    def dispatch(self, query, opts, token, on_event=None, execution_context=None) -> Response:
        """Turn lock + budget wrapping, identical to the native turn service."""
        from internal.agent.conversations import ConversationBusy
        from internal.resilience.budget import request_budget

        agent = self.agent
        lock = getattr(agent, "_turn_lock", None)
        if lock is not None and not lock.acquire(blocking=False):
            raise ConversationBusy("当前会话仍在执行")
        try:
            with request_budget(
                getattr(agent.cfg, "max_llm_calls_per_turn", 24),
                getattr(agent.cfg, "max_tool_calls_per_turn", 32),
            ):
                return self._invoke(query, opts, token, on_event, execution_context)
        finally:
            if lock is not None:
                lock.release()

    def _invoke(self, query, opts, token, on_event=None, execution_context=None) -> Response:
        self._opts = opts
        self._token = token
        self._on_event = on_event
        self._execution_context = execution_context or RequestExecutionContext()
        self._pr = None
        self._resp = None
        self._graph.invoke({"query": str(query)})
        return self._resp

    # ----------------------------------------------------------------- nodes

    def policy_node(self, state):
        """Harness execution-policy gate, identical to the native pre-prepare check."""
        from internal.harness.plugins import HarnessContext
        from internal.harness.execution import start_session

        agent, query = self.agent, state["query"]
        policy = HarnessContext(
            session_id=getattr(agent, "conversation_id", "") or "default",
            user_id=getattr(agent, "user_id", "default_user"),
            query=query,
            plugins=getattr(agent, "execution_plugins", []),
        )
        start_session(policy)
        if policy.interrupted:
            denied = Response(
                query=query,
                answer=policy.state.get("final_answer", "请求被执行策略拒绝"),
                interrupted=True,
                error=policy.interrupted_reason,
            )
            _emit(self._on_event, "done", _to_jsonable(denied))
            self._resp = denied
            return {"denied": True}
        return {}

    def after_policy(self, state):
        return END if state.get("denied") else "prepare"

    def prepare_node(self, state):
        """STM write + memory extraction + route decision + context assembly,
        then the ``memory``/``route`` events in the native dispatch_once order."""
        query = state["query"]
        pr = self.agent._prepare(query, self._opts, self._execution_context)
        self._pr = pr
        context = self._execution_context
        resp = Response(
            query=query,
            mode=pr["mode"],
            trace_id=str(context.trace_id or uuid.uuid4()),
            experiment_exposure_id=str(context.experiment_exposure_id or ""),
            runtime_strategy_checksum=str(context.runtime_strategy_checksum or ""),
        )
        resp.extracted_info = pr["extracted"]
        if resp.extracted_info:
            _emit(self._on_event, "memory", {"extracted_info": resp.extracted_info})
        _emit(self._on_event, "route", {"mode": resp.mode})
        self._resp = resp
        return {"mode": pr["mode"]}

    def after_prepare(self, state):
        """The native cancel checkpoint sits between the route event and mode
        dispatch, so on the graph it is a routing decision; unknown modes fall
        through to the direct-chat branch exactly like native ``dispatch_mode``."""
        if self._token.is_cancelled():
            return "cancel"
        return MODE_NODE.get(state["mode"], "chat")

    def cancel_node(self, state):
        resp = self._resp
        resp.interrupted = True
        resp.answer = "[已中断] 请求在开始前被取消"
        _emit(self._on_event, "done", _to_jsonable(resp))
        return {}

    def react_node(self, state):
        pr, resp = self._pr, self._resp
        resp.answer, resp.steps, resp.task = self.agent._run_react_with_tools(
            pr["query"],
            pr["route_tools"],
            pr["mem_prefix"],
            pr["hist_msgs"],
            self._token,
            self._on_event,
            allow_subagents=False,
        )
        self.agent._apply_graph_tool_calls(resp)
        self.agent._apply_structured_tool_output(resp)
        if resp.intent is None and not any(step.type == StepType.ACTION for step in resp.steps):
            resp.intent = "direct_chat"
        return {}

    def research_node(self, state):
        """``rag_agent``: the forced subagent plan → TaskGraph research pipeline."""
        pr, resp = self._pr, self._resp
        resp.answer, resp.steps, resp.task = self.agent._run_react_with_tools(
            pr["query"],
            pr["route_tools"] or {},
            pr["mem_prefix"],
            pr["hist_msgs"],
            self._token,
            self._on_event,
            allow_subagents=True,
            force_subagent_plan=True,
        )
        self.agent._apply_graph_tool_calls(resp)
        return {}

    def rag_node(self, state):
        """Lightweight RAG query with its structured trace, in native order."""
        pr, resp = self._pr, self._resp
        resp.answer, resp.search_results, resp.rag_trace = self.agent._run_rag_query_with_trace(
            pr["query"],
            runtime_overrides=pr.get("runtime_overrides") or {},
        )
        resp.rag_trace.setdefault("trace_id", resp.trace_id)
        if resp.runtime_strategy_checksum:
            resp.rag_trace.setdefault(
                "runtime_strategy_checksum",
                resp.runtime_strategy_checksum,
            )
        _emit(self._on_event, "rag_trace", resp.rag_trace)
        _emit(self._on_event, "rag_result", {"search_results": resp.search_results})
        _emit(self._on_event, "token", {"content": resp.answer})
        return {}

    def chat_node(self, state):
        pr, resp = self._pr, self._resp
        resp.answer = self.agent._chat_response(pr["mem_prefix"], pr["hist_msgs"], self._token, self._on_event)
        return {}

    def finalize_node(self, state):
        """Native post-mode policy: task/cancel interruption flags, finalize, done."""
        resp = self._resp
        if isinstance(resp.task, dict) and resp.task.get("status") == "interrupted":
            resp.interrupted = True
        if self._token.is_cancelled():
            resp.interrupted = True
        self.agent._finalize(state["query"], resp)
        _emit(self._on_event, "done", _to_jsonable(resp))
        return {}
