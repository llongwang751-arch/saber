"""Turn service for an explicitly supplied conversation runtime.

The caller owns mutable state and lifecycle; services do not retain a user or conversation.
"""

import logging
import os
import uuid
from typing import Any, Dict, Optional


from .planner import (
    needs_subagent_plan,
    refine_intent_with_llm,
)


from .contracts import StepType, ChatOptions, RequestExecutionContext, Response
from .serialization import _emit, _to_jsonable

logger = logging.getLogger(__name__)


def dispatch(agent, query, opts, token, on_event=None, execution_context=None):
    from .conversations import ConversationBusy

    # The legacy/default conversation also has a single in-flight turn.
    lock = getattr(agent, "_turn_lock", None)
    if lock is not None and not lock.acquire(blocking=False):
        raise ConversationBusy("当前会话仍在执行")
    try:
        from internal.resilience.budget import request_budget

        with request_budget(
            getattr(agent.cfg, "max_llm_calls_per_turn", 24), getattr(agent.cfg, "max_tool_calls_per_turn", 32)
        ):
            return agent._dispatch_once(query, opts, token, on_event, execution_context)
    finally:
        if lock is not None:
            lock.release()


def dispatch_once(
    agent,
    query: str,
    opts: ChatOptions,
    token,
    on_event=None,
    execution_context: Optional[RequestExecutionContext] = None,
) -> Response:
    """三段式编排：prepare → dispatch → finalize（与 main runOnce 对齐）。"""
    from internal.harness.plugins import HarnessContext
    from internal.harness.execution import start_session

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
        _emit(on_event, "done", _to_jsonable(denied))
        return denied
    context = execution_context or RequestExecutionContext()
    pr = agent._prepare(query, opts, context)
    resp = Response(
        query=query,
        mode=pr["mode"],
        trace_id=str(context.trace_id or uuid.uuid4()),
        experiment_exposure_id=str(context.experiment_exposure_id or ""),
        runtime_strategy_checksum=str(context.runtime_strategy_checksum or ""),
    )
    resp.extracted_info = pr["extracted"]
    if resp.extracted_info:
        _emit(on_event, "memory", {"extracted_info": resp.extracted_info})
    _emit(on_event, "route", {"mode": resp.mode})

    if token.is_cancelled():
        resp.interrupted = True
        resp.answer = "[已中断] 请求在开始前被取消"
        _emit(on_event, "done", _to_jsonable(resp))
        return resp

    agent._dispatch_mode(pr, resp, token, on_event)

    if isinstance(resp.task, dict) and resp.task.get("status") == "interrupted":
        resp.interrupted = True

    if token.is_cancelled():
        resp.interrupted = True

    agent._finalize(query, resp)
    _emit(on_event, "done", _to_jsonable(resp))
    return resp


def prepare(
    agent, query: str, opts: ChatOptions, execution_context: Optional[RequestExecutionContext] = None, *, memory_update
) -> Dict[str, Any]:
    """STM 写入 + 偏好提取 + 路由决策 + 上下文装配 + 历史构建。"""
    agent.stm.add("user", query)
    agent._save_chat_history("user", query)

    # 偏好/记忆抽取（同步规则即时回显 + 异步 LLM 扩展）
    # 注：async_update_memory 的写入路径直接修改 resp.extracted_info，
    # 这里复用一个 Response 占位以承接同步部分的输出。
    ph = Response(query=query)
    memory_update(agent, query, ph)

    # 知识库请求走只读 RAG；其余请求进入统一工具执行链。
    mode, route_tools = agent._route_decide(query, opts)

    mem_prefix = agent._build_context_prefix(query, mode)
    hist_msgs = agent._build_history_messages(query)

    return {
        "query": query,
        "mode": mode,
        "route_tools": route_tools,
        "mem_prefix": mem_prefix,
        "hist_msgs": hist_msgs,
        "extracted": ph.extracted_info,
        "runtime_overrides": dict((execution_context or RequestExecutionContext()).runtime_overrides or {}),
    }


def route_decide(agent, query: str, opts: ChatOptions):
    """Choose Agentic RAG, lightweight RAG, or ordinary ReAct."""
    rag_loaded = bool(agent.rag and getattr(agent.rag, "loaded", False))
    if opts.use_rag and rag_loaded:
        if agent._report_intent(query):
            return "rag_agent", None
        if agent._report_intent_refined(query):
            return "rag_agent", None
        return "rag", None
    executor = getattr(agent, "tool_executor", None)
    return "react", executor.snapshot() if executor is not None else {}


def report_intent(query: str) -> bool:
    return needs_subagent_plan(query)


def report_intent_refined(agent, query: str) -> bool:
    """可选的 LLM 意图复核（默认关闭）：兜住关键词漏网的报告类任务。

    两级漏斗的第二级——关键词命中时 `_route_decide` 已短路返回，只有
    未命中的灰区消息才会走到这里。开关关闭、复核超时或输出非法时一律
    维持关键词结果（rag），路由行为与未启用时完全一致。
    """
    raw = os.getenv("AGI_INTENT_LLM_ENABLED", "").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return False
    try:
        return refine_intent_with_llm(agent, query)
    except Exception:
        return False


def dispatch_mode(agent, pr: Dict[str, Any], resp: Response, token, on_event=None) -> None:
    """按 mode 分发到对应 handler，把结果填回 resp。"""
    mode = pr["mode"]
    query = pr["query"]
    mem_prefix = pr["mem_prefix"]
    hist_msgs = pr["hist_msgs"]
    route_tools = pr["route_tools"]
    runtime_overrides = pr.get("runtime_overrides") or {}
    resp.extracted_info = pr["extracted"]

    if mode == "react":
        resp.answer, resp.steps, resp.task = agent._run_react_with_tools(
            query,
            route_tools,
            mem_prefix,
            hist_msgs,
            token,
            on_event,
            allow_subagents=False,
        )
        agent._apply_graph_tool_calls(resp)
        agent._apply_structured_tool_output(resp)
        if resp.intent is None and not any(step.type == StepType.ACTION for step in resp.steps):
            resp.intent = "direct_chat"
    elif mode == "rag_agent":
        resp.answer, resp.steps, resp.task = agent._run_react_with_tools(
            query,
            route_tools or {},
            mem_prefix,
            hist_msgs,
            token,
            on_event,
            allow_subagents=True,
            force_subagent_plan=True,
        )
        agent._apply_graph_tool_calls(resp)
    elif mode == "rag":
        resp.answer, resp.search_results, resp.rag_trace = agent._run_rag_query_with_trace(
            query,
            runtime_overrides=runtime_overrides,
        )
        resp.rag_trace.setdefault("trace_id", resp.trace_id)
        if resp.runtime_strategy_checksum:
            resp.rag_trace.setdefault(
                "runtime_strategy_checksum",
                resp.runtime_strategy_checksum,
            )
        _emit(on_event, "rag_trace", resp.rag_trace)
        _emit(on_event, "rag_result", {"search_results": resp.search_results})
        _emit(on_event, "token", {"content": resp.answer})
    else:
        resp.answer = agent._chat_response(mem_prefix, hist_msgs, token, on_event)
