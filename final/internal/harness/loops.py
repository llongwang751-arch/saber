"""Pluggable Agent Loops for Harness Runtime 2.0.

Inspired by deepseek-harness's loop-as-a-plugin architecture,
this module decouples the reasoning & execution strategy from the core engine.
Developers can seamlessly hot-swap between ReAct, DAG scheduling, and Direct Chat.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from internal.graph.task_graph import NodeStatus, TaskGraph
from internal.harness.events import EventType
from internal.harness.plugins import HarnessContext, HarnessPlugin

from .execution import execute_tool, ExecutionInterrupted

logger = logging.getLogger(__name__)


class BaseLoopPlugin(HarnessPlugin):
    """Abstract interface for pluggable Agent decision loops."""

    name = "base_loop"
    priority = 100

    def execute_loop(
        self,
        ctx: HarnessContext,
        tools: Dict[str, Any],
        llm_fn: Callable[[str], str],
        resume_from_step: int = 0,
    ) -> Any:
        raise NotImplementedError


class DirectChatLoopPlugin(BaseLoopPlugin):
    """Direct single-turn response loop without multi-step planning."""

    name = "direct_chat_loop"

    def execute_loop(
        self,
        ctx: HarnessContext,
        tools: Dict[str, Any],
        llm_fn: Callable[[str], str],
        resume_from_step: int = 0,
    ) -> str:
        ctx.emit(EventType.USER_INPUT, {"query": ctx.query}, step_index=0)
        if ctx.interrupted:
            return ctx.state.get("final_answer", "")
        reply = llm_fn(ctx.query)
        ctx.emit(EventType.REASONING, {"response": reply}, step_index=1)
        return reply


class ReActLoopPlugin(BaseLoopPlugin):
    """ReAct (Thought-Action-Observation) Loop Plugin with step-level checkpoints."""

    name = "react_loop"

    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations

    def execute_loop(
        self,
        ctx: HarnessContext,
        tools: Dict[str, Any],
        llm_fn: Callable[[str], str],
        resume_from_step: int = 0,
    ) -> Dict[str, Any]:
        ctx.emit(EventType.USER_INPUT, {"query": ctx.query}, step_index=0)

        history_observations: List[str] = list(ctx.state.get("observations", []))
        step_idx = resume_from_step

        while step_idx < self.max_iterations:
            if ctx.interrupted:
                return {"status": "interrupted", "observations": history_observations, "reason": ctx.interrupted_reason}
            step_idx += 1
            ctx.state["step_index"] = step_idx
            ctx.state["invocation_id"] = str(step_idx)
            ctx.state["durable_actions"] = True

            # 1. LLM Reasoning
            prompt = f"Query: {ctx.query}\nPast Observations: {history_observations}\nNext step (Thought/Action/Final Answer):"
            pending = ctx.state.pop("pending_action", None)
            thought = ("Action: " + pending["tool"] + "(" + json.dumps(pending["params"]) + ")") if pending else llm_fn(prompt)

            ctx.emit(EventType.REASONING, {"step": step_idx, "thought": thought}, step_index=step_idx)

            # Check if Final Answer reached
            if "Final Answer:" in thought:
                answer = thought.split("Final Answer:", 1)[-1].strip()
                ctx.state["final_answer"] = answer
                ctx.emit(EventType.CHECKPOINT, {
                    "step_index": step_idx,
                    "status": "completed",
                    "final_answer": answer,
                }, step_index=step_idx)
                return {"status": "success", "answer": answer, "steps": step_idx}

            # 2. Parse Tool Action (e.g. Action: tool_name(param=value))
            tool_name, tool_params = self._parse_action(thought)
            if not tool_name or tool_name not in tools:
                # No valid tool called; treat as direct response
                ctx.state["final_answer"] = thought
                return {"status": "success", "answer": thought, "steps": step_idx}

            ctx.emit(EventType.TOOL_CALL, {
                "tool": tool_name,
                "params": tool_params,
            }, step_index=step_idx)

            def invoke(params):
                # Persist uncertainty only after policy/approval checks pass,
                # immediately before entering the external tool.
                ctx.emit(EventType.CHECKPOINT, {
                    "step_index": step_idx - 1, "status": "dispatching",
                    "observations": list(history_observations),
                    "pending_action": {"tool": tool_name, "params": params},
                }, step_index=step_idx)
                tool_obj = tools[tool_name]
                return tool_obj(params) if callable(tool_obj) else tool_obj.func(params)
            try:
                result_val = execute_tool(ctx, tool_name, tool_params, invoke)
            except ExecutionInterrupted:
                ctx.emit(EventType.CHECKPOINT, {
                    "step_index": step_idx - 1,
                    "status": "interrupted",
                    "observations": list(history_observations),
                    "pending_action": {"tool": tool_name, "params": tool_params},
                }, step_index=step_idx)
                return {"status": "interrupted", "reason": ctx.interrupted_reason, "steps": step_idx, "observations": history_observations}
            except Exception as exc:
                result_val = f"Tool error: {exc}"
                ctx.emit(EventType.ERROR, {"tool": tool_name, "error": str(exc)}, step_index=step_idx)

            obs = str(result_val)
            history_observations.append(f"Step {step_idx} [{tool_name}]: {obs}")
            ctx.state["observations"] = history_observations

            ctx.emit(EventType.TOOL_RESULT, {
                "tool": tool_name,
                "result": obs,
            }, step_index=step_idx)

            # 4. Save checkpoint after each action for resume capability
            ctx.emit(EventType.CHECKPOINT, {
                "step_index": step_idx,
                "observations": list(history_observations),
                "status": "running",
            }, step_index=step_idx)

        ctx.interrupted = True
        ctx.interrupted_reason = "Max iterations reached"
        return {"status": "max_iterations", "observations": history_observations, "steps": step_idx}

    def _parse_action(self, text: str) -> tuple[str, dict]:
        """Simple action parser supporting 'Action: tool_name(params)' or JSON."""
        if "Action:" in text:
            action_line = text.split("Action:", 1)[1].strip().split("\n")[0]
            if "(" in action_line and action_line.endswith(")"):
                tool = action_line.split("(", 1)[0].strip()
                arg_str = action_line.split("(", 1)[1][:-1].strip()
                try:
                    params = json.loads(arg_str) if arg_str.startswith("{") else {"arg": arg_str}
                except Exception:
                    params = {"query": arg_str}
                return tool, params
        return "", {}


class DAGLoopPlugin(BaseLoopPlugin):
    """TaskGraph DAG Parallel Scheduler Loop Plugin."""

    name = "dag_loop"

    def __init__(self, max_parallel: int = 2):
        self.max_parallel = max_parallel

    def execute_loop(
        self,
        ctx: HarnessContext,
        tools: Dict[str, Any],
        llm_fn: Callable[[str], str],
        resume_from_step: int = 0,
    ) -> Dict[str, Any]:
        graph: Optional[TaskGraph] = ctx.state.get("task_graph")
        if not graph:
            return {"status": "error", "message": "No TaskGraph provided in state"}

        ctx.emit(EventType.USER_INPUT, {"query": ctx.query}, step_index=0)
        ctx.emit(EventType.STATE_CHANGE, {
            "action": "dag_schedule_start",
            "nodes": list(graph.nodes.keys()),
        }, step_index=0)

        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import asdict, replace
        import threading
        graph.validate()
        results: Dict[str, Any] = {n.id: n.result for n in graph.nodes.values() if n.status == NodeStatus.DONE}
        checkpoint_lock = threading.RLock()
        def checkpoint(status="running"):
            ctx.emit(EventType.CHECKPOINT, {
                "dag_schema": 1, "status": status, "query": ctx.query, "user_id": ctx.user_id,
                "max_parallel": self.max_parallel,
                "graph": [asdict(node) for node in graph.nodes.values()],
                "completed_nodes": [n.id for n in graph.nodes.values() if n.status == NodeStatus.DONE],
                "output": {"status": "success", "results": dict(results)} if status == "completed" else None,
            })
        # An in-flight node could already have changed the outside world.
        if any(n.status == NodeStatus.RUNNING for n in graph.nodes.values()):
            ctx.interrupted = True
            ctx.interrupted_reason = "DAG has an uncertain in-flight action; automatic replay refused"
            return {"status": "interrupted", "results": results, "reason": ctx.interrupted_reason}
        checkpoint()
        def run_node(node_id):
            node = graph.nodes[node_id]
            if node.status == NodeStatus.DONE:
                return node_id, node.result
            if ctx.interrupted or not all(graph.nodes[dep].status == NodeStatus.DONE for dep in node.depends_on):
                node.status = NodeStatus.SKIPPED
                return node_id, None
            if node.status != NodeStatus.PENDING:
                return node_id, None
            local = replace(ctx, state={**ctx.state, "invocation_id": node_id, "durable_actions": True})
            with checkpoint_lock:
                node.status = NodeStatus.RUNNING
                checkpoint()
            try:
                tool = tools.get(node.tool_name)
                if tool is None:
                    raise ValueError("Unregistered tool: " + node.tool_name)
                value = execute_tool(local, node.tool_name, node.params or {}, lambda p: tool(p) if callable(tool) else tool.func(p))
                with checkpoint_lock:
                    node.status = NodeStatus.DONE
                    node.result = str(value)
                    results[node_id] = node.result
                    checkpoint()
                return node_id, value
            except ExecutionInterrupted:
                node.status = NodeStatus.RUNNING if local.state.get("dispatch_uncertain") else NodeStatus.PENDING
                ctx.interrupted = True
                ctx.interrupted_reason = local.interrupted_reason
            except Exception as exc:
                node.status = NodeStatus.FAILED
                ctx.emit(EventType.ERROR, {"node_id": node_id, "error": str(exc)})
            return node_id, None
        for level_index, level in enumerate(graph.topological_levels()):
            if ctx.interrupted:
                break
            with ThreadPoolExecutor(max_workers=max(1, self.max_parallel)) as pool:
                for node_id, value in pool.map(run_node, level):
                    if value is not None:
                        results[node_id] = value
            with checkpoint_lock:
                checkpoint("interrupted" if ctx.interrupted else "running")
        status = "interrupted" if ctx.interrupted else "failed" if any(n.status != NodeStatus.DONE for n in graph.nodes.values()) else "success"
        if status == "success":
            checkpoint("completed")
        return {"status": status, "results": results}
