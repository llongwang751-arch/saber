"""Planning service for an explicitly supplied conversation runtime.

The caller owns mutable state and lifecycle; services do not retain a user or conversation.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from internal.llm.llm import Message
from internal.tools.tools import Tool

from .artifact import prepare_artifact_workspace, produce_artifact
from .graph_runtime import GraphConfig, GraphRuntime
from .planner import (
    llm_plan_graph,
    subagent_pipeline_nodes,
)


from .contracts import StepType, ReActStep, Response
from .serialization import _latest_structured_tool_result, _graph_to_contract, _graph_to_react_steps, _emit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanningDependencies:
    plan_graph: Any = llm_plan_graph
    artifact_writer: Any = produce_artifact


def run_react_with_tools(
    agent,
    query: str,
    tools_map: Dict[str, Tool],
    mem_prefix: str,
    hist_msgs: List[Message],
    token,
    on_event=None,
    allow_subagents: bool = False,
    force_subagent_plan: bool = False,
    *,
    dependencies: PlanningDependencies,
):
    """ReAct 模式入口：与 main 分支 runReAct 行为一致。

    - llm_plan_graph 拿到节点列表；
    - 节点为空 → 直接调 chat LLM 给一句话回答（对应 Go chatLLM 兜底），不再做工具迭代；
    - 节点非空 → 走 GraphRuntime 拓扑分层 + race + 重试；执行结束后用
      _generate_final_answer（对应 Go llmGenerate）合成自然语言回复。
    """
    task = {
        "task_id": f"task-{time.time_ns()}",
        "query": query,
        "status": "running",
        "phase": "planning",
        "steps": [],
    }
    task_lease = None
    agent._cancel_registry.set_task(task)
    try:
        tools_map = tools_map or {}
        if force_subagent_plan:
            plan_nodes = subagent_pipeline_nodes(query)
        else:
            plan_nodes = dependencies.plan_graph(agent, query, tools_map, mem_prefix, allow_subagents)
        if not plan_nodes:
            # 与 Go runReAct: planNodes 空 → chatLLM 一句话答复
            answer = agent._chat_response(mem_prefix, hist_msgs, token, on_event)
            task.update(status="completed", phase="done", result=answer)
            agent.save_snapshot(task)
            return answer, [], task

        if any(node.tool_name == "farm_copilot" for node in plan_nodes):
            try:
                from internal.application.farm_agent import detect_farm_guardrails

                for guardrail in detect_farm_guardrails(query):
                    _emit(on_event, "guardrail", guardrail)
            except Exception:
                pass
        _emit(
            on_event,
            "plan_created",
            {
                "task_id": task["task_id"],
                "nodes": [
                    {
                        "id": node.id,
                        "type": getattr(node.type, "value", str(node.type)),
                        "tool": node.tool_name,
                        "reason": node.name,
                        "arguments": dict(node.params or {}),
                        "depends_on": list(node.depends_on or []),
                    }
                    for node in plan_nodes
                ],
            },
        )

        from internal.graph.task_graph import TaskGraph

        graph = TaskGraph(plan_nodes)
        try:
            graph.validate()
        except Exception:
            # Never remove dependencies to make an invalid plan runnable.
            task.update(status="interrupted", phase="invalid_plan", result="执行计划依赖不合法，请重新规划")
            agent.save_snapshot(task)
            return task["result"], [], task
        task["phase"] = "executing"
        task["steps"] = [
            {
                "id": index,
                "name": node.name,
                "node_id": node.id,
                "tool_name": node.tool_name,
                "params": dict(node.params or {}),
                "status": "pending",
                "retry_count": 0,
            }
            for index, node in enumerate(graph.nodes.values(), start=1)
        ]
        task["current_step"] = 0
        task["graph"] = _graph_to_contract(graph)
        if hasattr(agent, "task_mem"):
            agent.task_mem.reset()
        agent.save_snapshot(task)
        host_workspace = prepare_artifact_workspace(agent, task["task_id"])
        if host_workspace:
            _emit(on_event, "sandbox_ready", {"workspace": host_workspace})
        cfg = GraphConfig(
            max_parallel=getattr(agent.cfg, "graph_max_parallel", 2),
            race_timeout_ms=getattr(agent.cfg, "graph_race_timeout_ms", 30000),
            enable_racing=getattr(agent.cfg, "graph_enable_racing", True),
            replan_enabled=getattr(agent.cfg, "graph_replan_enabled", False),
            max_replan=getattr(agent.cfg, "graph_max_replan", 2),
            replan_on_failed=getattr(agent.cfg, "graph_replan_on_failed", False),
        )
        runtime = GraphRuntime(graph, agent, cfg, tools_map, task, on_event=on_event)
        journal = getattr(agent.inf.repo, "action_journal", None)
        if journal is not None:
            from .recovery import TaskLease

            task_lease = TaskLease(journal, agent.user_id, task["task_id"])
            task_lease.__enter__()
            runtime._lease = task_lease
        runtime.set_replan_context(query, mem_prefix, allow_subagents)
        result = runtime.execute(token)
        steps = _graph_to_react_steps(graph)
        cancelled = bool(token is not None and callable(getattr(token, "is_cancelled", None)) and token.is_cancelled())
        if result.interrupted or cancelled:
            task["phase"] = "interrupted"
            task["status"] = "interrupted"
            summary = "; ".join(
                f"{node.tool_name}:{getattr(node.status, 'value', str(node.status))}" for node in graph.nodes.values()
            )
            return f"[已中断] {result.interrupted_msg}\n当前进度：{summary}", steps, task

        task["phase"] = "generating"
        final_answer = agent._generate_final_answer(query, steps, mem_prefix, token, on_event)
        graph_failed = any(getattr(node.status, "value", str(node.status)) == "failed" for node in graph.nodes.values())
        if task_lease is not None:
            task_lease.refresh()
        from .artifact import ArtifactExecutionBlocked

        try:
            artifact_step = (
                None
                if graph_failed
                else dependencies.artifact_writer(
                    agent,
                    query,
                    final_answer,
                    host_workspace,
                    on_event=on_event,
                    token=token,
                )
            )
        except ArtifactExecutionBlocked as exc:
            task.update(
                status="interrupted", phase="artifact_blocked", result=final_answer + "\n\n[产物生成中断] " + str(exc)
            )
            return task["result"], steps, task
        if artifact_step is not None:
            step = ReActStep(**artifact_step)
            steps.append(step)
            final_answer += "\n\n---\n" + step.content
        steps.append(ReActStep(type=StepType.FINAL_ANSWER, content=final_answer))
        if result.interrupted:
            task["status"] = "interrupted"
            task["phase"] = "interrupted"
        elif graph_failed:
            task["status"] = "failed"
            task["phase"] = "failed"
        else:
            task["status"] = "completed"
            task["phase"] = "done"
        task["result"] = final_answer
        task["graph"] = _graph_to_contract(graph)
        return final_answer, steps, task
    finally:
        # Go 版保留最近任务及快照供 /api/snapshots 和恢复接口读取。
        try:
            if task.get("recovery"):
                if task_lease is not None:
                    task_lease.refresh()
                agent.persist_task_checkpoint(task)
        finally:
            if task_lease is not None and hasattr(task_lease, "worker"):
                task_lease.__exit__(None, None, None)


def generate_final_answer(agent, query: str, steps: List[ReActStep], mem_prefix: str, token=None, on_event=None) -> str:
    if not agent.cfg.is_real_llm():
        structured = _latest_structured_tool_result(steps)
        if structured is not None:
            answer = str(structured.get("answer") or "").strip()
            if answer:
                _emit(on_event, "token", {"content": answer})
                return answer
    steps_str = "\n".join(f"{s.type}: {s.content}" for s in steps)
    prompt = f"""基于以下推理过程，给出最终答案。

    任务: {query}

    记忆上下文:
    {mem_prefix or "（无）"}

    推理过程:
    {steps_str}

    请用自然语言总结最终答案，不要包含 Action/Final 等关键字。
    """
    messages = [Message(role="user", content=prompt)]
    return agent._chat_llm("你是一个总结助手，能够基于推理过程给出简洁的最终答案。", messages, token, on_event)


def apply_graph_tool_calls(resp: Response) -> None:
    task = resp.task if isinstance(resp.task, dict) else {}
    graph = task.get("graph") if isinstance(task, dict) else None
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, dict):
        return
    calls: List[Dict[str, Any]] = []
    for node in nodes.values():
        if not isinstance(node, dict) or not node.get("tool_name"):
            continue
        status = str(node.get("status") or "").lower()
        result = node.get("result") or ""
        success = status == "done"
        try:
            result_payload = json.loads(result) if isinstance(result, str) else result
            if isinstance(result_payload, dict) and result_payload.get("ok") is False:
                success = False
        except json.JSONDecodeError:
            pass
        calls.append(
            {
                "tool_name": str(node.get("tool_name")),
                "params": dict(node.get("params") or {}),
                "tool_result": result,
                "success": success,
                "error": str(node.get("error") or "") or None,
            }
        )
    resp.tool_calls = calls
    if len(calls) == 1:
        resp.tool_call = dict(calls[0])


def apply_structured_tool_output(resp: Response) -> None:
    structured = _latest_structured_tool_result(resp.steps)
    if structured is None:
        return
    intent = str(structured.get("intent") or "").strip()
    evidence = structured.get("evidence")
    slots = evidence.get("slots") if isinstance(evidence, dict) else None
    if intent:
        resp.intent = intent
    if isinstance(slots, dict):
        resp.slots = dict(slots)
    if structured.get("ok") is False:
        resp.fallback = True
        error = structured.get("error")
        if isinstance(error, dict):
            resp.error = str(error.get("message") or error.get("code") or "工具执行失败")
        else:
            resp.error = str(error or "工具执行失败")
