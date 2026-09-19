import json
import queue
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

from internal.resilience.budget import inherit_context
from internal.agent.subagents import SubAgentTask
from internal.agent.tool_execution import guarded_tool_attempt
from internal.graph.task_graph import NodeStatus, NodeType, TaskGraph
from internal.promptctx import StepObservation, ToolCallTrace
from internal.tools.tools import (
    ToolCallContext,
    ToolError,
    ToolResult,
    classify_tool_exception,
)


@dataclass
class GraphConfig:
    max_parallel: int = 2
    race_timeout_ms: int = 30000
    enable_racing: bool = True
    replan_enabled: bool = False
    max_replan: int = 2
    replan_on_failed: bool = False


@dataclass
class NodeResult:
    status: NodeStatus
    result: str = ""
    error: str = ""
    payload_json: Optional[Dict[str, Any]] = None
    error_code: str = ""
    retryable: Optional[bool] = None
    duration: float = 0.0
    metadata: Dict[str, str] = field(default_factory=dict)
    attempts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class GraphResult:
    observations: List[str] = field(default_factory=list)
    node_results: Dict[str, NodeResult] = field(default_factory=dict)
    interrupted: bool = False
    interrupted_at: str = ""
    interrupted_msg: str = ""


class GraphRuntime:
    """按拓扑层级并行执行 TaskGraph，支持 race group 和取消。"""

    def __init__(
        self,
        graph: TaskGraph,
        agent,
        cfg: GraphConfig,
        tools: Dict[str, Any],
        task: Optional[dict] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        if cfg.max_parallel <= 0:
            cfg.max_parallel = 2
        if cfg.race_timeout_ms <= 0:
            cfg.race_timeout_ms = 30000
        self.graph = graph
        self.agent = agent
        self.cfg = cfg
        self.tools = tools
        self.task = task if task is not None else {}
        self.on_event = on_event
        self._sem = threading.Semaphore(cfg.max_parallel)
        self._lock = threading.RLock()
        self._results: Dict[str, str] = {n.id: n.result for n in graph.nodes.values() if n.status == NodeStatus.DONE}
        self._lease = None
        self._checkpoint_error = None
        self._dispatches = dict(self.task.get('recovery', {}).get('dispatches', {}))
        self._errors: Dict[str, str] = {}
        self._tool_results: Dict[str, ToolResult] = {}
        self._tool_attempts: Dict[str, List[ToolResult]] = {}
        self.query = ""
        self.mem_prefix = ""
        self.allow_subagents = False
        self.replan_used = 0

    def set_replan_context(self, query: str, mem_prefix: str, allow_subagents: bool = False) -> None:
        self.query = query
        self.mem_prefix = mem_prefix
        self.allow_subagents = allow_subagents

    def execute(self, token) -> GraphResult:
        from .recovery import TaskLease
        journal = getattr(getattr(getattr(self.agent, 'inf', None), 'repo', None), 'action_journal', None)
        try:
            if self._lease is None and journal is not None and self.task.get('task_id'):
                with TaskLease(journal, self.agent.user_id, self.task['task_id']) as lease:
                    self._lease = lease
                    return self._execute(token)
            return self._execute(token)
        except Exception as exc:
            return self._build_interrupted('执行检查点或租约失败：' + type(exc).__name__)

    def _execute(self, token) -> GraphResult:
        try:
            self.graph.validate()
        except Exception as e:
            return GraphResult(interrupted=True, interrupted_msg=f"图校验失败: {e}")

        for node in self.graph.nodes.values():
            if node.status == NodeStatus.RUNNING:
                return self._build_interrupted('动作结果不确定，禁止自动重放：' + node.id)
            tool = self.tools.get(node.tool_name)
            if node.race_group and (getattr(tool, "side_effecting", False)
                                    or node.tool_name in {"exec_command", "write_document", "ingest_document", "doc_agent"}):
                return GraphResult(interrupted=True, interrupted_msg="副作用工具不能参加竞速执行，请重新规划")

        self._save_snapshot()
        self._emit_event("graph_ready", {
            "levels": self.graph.topological_levels(),
            "nodes": {key: _node_dict(node) for key, node in self.graph.nodes.items()},
        })
        idx = 0
        while True:
            level = self.graph.ready_nodes()
            if not level:
                break
            if _is_cancelled(token):
                return self._build_interrupted(f"在第 {idx} 层执行前被中断")

            groups = self._group_by_race(level)
            threads = []
            for group_name, node_ids in groups:
                target = self._race_group if group_name and self.cfg.enable_racing else self._execute_group
                t = threading.Thread(target=inherit_context(target), args=(token, group_name, node_ids), daemon=True)
                threads.append(t)
                t.start()
            for t in threads:
                t.join()

            if self._checkpoint_error:
                return self._build_interrupted('检查点持久化失败，已停止后续执行')

            if _is_cancelled(token):
                return self._build_interrupted(f"在第 {idx} 层执行后被中断")
            self._save_snapshot()

            blocked = next((value.error.message for value in self._tool_results.values()
                            if value.error and (value.error.code in {"approval_required", "execution_uncertain"}
                                                or value.metadata.get("dispatch_state") == "uncertain")), None)
            if blocked:
                return self._build_interrupted(blocked)

            if self.cfg.replan_enabled and self.replan_used < self.cfg.max_replan:
                failed = next(
                    (self.graph.nodes[node_id] for node_id in level
                     if self.graph.nodes[node_id].status == NodeStatus.FAILED),
                    None,
                )
                if failed is not None and self.cfg.replan_on_failed:
                    self._try_replan("node_failed", failed)
                elif self.replan_used < self.cfg.max_replan:
                    self._try_replan("layer_done", None)
            idx += 1

        if any(node.status == NodeStatus.RUNNING for node in self.graph.nodes.values()):
            return self._build_interrupted('仍有结果不确定的执行节点，不能标记完成')
        return self._build_result()

    def _try_replan(self, reason: str, failed) -> List[str]:
        self.replan_used += 1
        try:
            from .planner import llm_replan

            nodes = llm_replan(
                self.agent, self.query, reason, self.graph, self.tools,
                self.mem_prefix, self.allow_subagents, failed,
            )
        except Exception:
            nodes = []
        if not nodes:
            return []
        added = self.graph.add_nodes(nodes)
        try:
            self.graph.validate()
        except Exception:
            for node_id in added:
                self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
            return []
        if added:
            self._emit_event("replan", {
                "reason": reason,
                "added": [
                    {"id": node_id, "executor": self.graph.nodes[node_id].tool_name,
                     "reason": self.graph.nodes[node_id].name}
                    for node_id in added
                ],
                "used_count": self.replan_used,
                "max_count": self.cfg.max_replan,
            })
        return added

    def _execute_group(self, token, _group_name: str, node_ids: List[str]) -> None:
        threads = []
        for node_id in node_ids:
            t = threading.Thread(target=inherit_context(self._execute_node), args=(token, node_id), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

    def _race_group(self, token, group_name: str, node_ids: List[str]) -> None:
        done = threading.Event()
        results = []
        results_lock = threading.Lock()

        def runner(node_id: str) -> None:
            if done.is_set() or _is_cancelled(token):
                self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                return
            result, error = self._execute_single_node(token, node_id, record=False)
            with results_lock:
                results.append((node_id, result, error))
                if error is None and not done.is_set():
                    done.set()
                    self.graph.set_node_result(node_id, result)
                    self._record_success(node_id, result)

        threads = []
        for node_id in node_ids:
            t = threading.Thread(target=inherit_context(runner), args=(node_id,), daemon=True)
            threads.append(t)
            t.start()

        timeout = self.cfg.race_timeout_ms / 1000.0
        done.wait(timeout)
        for t in threads:
            t.join(0.01)

        winner = None
        with results_lock:
            for node_id, _result, error in results:
                if error is None:
                    winner = node_id
                    break
        if winner is not None:
            for node_id in node_ids:
                if node_id != winner and self.graph.nodes[node_id].status != NodeStatus.DONE:
                    self.graph.set_node_status(node_id, NodeStatus.SKIPPED)
            return

        last_error = "竞速组无成功结果"
        with results_lock:
            for _node_id, _result, error in results:
                if error:
                    last_error = error
        for node_id in node_ids:
            self.graph.set_node_error(node_id, last_error)
            self._record_failure(node_id, last_error)

    def _execute_node(self, token, node_id: str) -> None:
        result, error = self._execute_single_node(token, node_id, record=True)
        if error is not None:
            with self._lock:
                self._errors[node_id] = error
        else:
            with self._lock:
                self._results[node_id] = result

    def _execute_single_node(self, token, node_id: str, record: bool = True):
        with self._sem:
            node = self.graph.nodes[node_id]
            if self._checkpoint_error:
                return '', '检查点持久化失败'
            if self._lease is not None:
                self._lease.refresh()
            if _is_cancelled(token):
                self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                return "", "被用户中断"

            self.graph.set_node_status(node_id, NodeStatus.RUNNING)
            self._save_snapshot()
            node = self.graph.nodes[node_id]
            self._sync_task_step(node_id, status="running")
            self._emit_event("node_start", {"id": node_id, "tool": node.tool_name})
            self._emit_step("Thought", node.name)
            self._emit_step(
                "Action",
                f"调用 {node.tool_name}",
                dict(node.params or {}),
            )
            if node.type == NodeType.SUBAGENT:
                return self._execute_subagent_node(token, node_id, record)

            tool = self.tools.get(node.tool_name)
            if tool is None:
                error = f"工具 {node.tool_name} 不在允许列表中"
                self.graph.set_node_error(node_id, error)
                if record:
                    self._record_failure(node_id, error)
                return "", error

            is_llm_heavy = node.tool_name.startswith("skill_")
            max_retries = 1 if is_llm_heavy else max(1, int(getattr(self.agent.cfg, "max_retries", 1)))
            if getattr(tool, "side_effecting", False) or node.tool_name in {"write_document", "ingest_document", "exec_command"}:
                max_retries = 1  # A timeout may leave the original write running.
            retry_delay = float(getattr(self.agent.cfg, "retry_delay_ms", 0)) / 1000.0
            timeout = float(getattr(self.agent.cfg, "step_timeout_ms", 0) or 0) / 1000.0
            if is_llm_heavy and 0 < timeout < 300.0:
                timeout = 300.0
            params = self._enrich_params_with_upstream(node, dict(node.params or {}), tool)
            from internal.harness.journal import fingerprint
            self._dispatches[node_id] = {'fingerprint': fingerprint({'tool': node.tool_name, 'params': params})}
            self._save_snapshot()
            last_error = ""
            for attempt in range(max_retries):
                if _is_cancelled(token):
                    self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                    return "", "被用户中断"
                try:
                    self._emit_event("tool_call", {
                        "id": node_id,
                        "tool": node.tool_name,
                        "arguments": params,
                        "attempt": attempt + 1,
                    })
                    structured = guarded_tool_attempt(self.agent, tool, node.tool_name, params, token, timeout,
                        invocation_id=f"{self.task.get('task_id', 'graph')}:{node_id}")
                    self._tool_results[node_id] = structured
                    self._dispatches[node_id].update(
                        error_code=structured.error.code if structured.error else '',
                        uncertain=structured.metadata.get('dispatch_state') == 'uncertain')
                    self._tool_attempts.setdefault(node_id, []).append(structured)
                    result = structured.payload
                    if not structured.success:
                        error = structured.error or ToolError(
                            "internal", "工具返回失败", retryable=False
                        )
                        last_error = _display_tool_error(tool, error)
                        if result:
                            self.graph.nodes[node_id].result = result
                        self.graph.set_node_retry_count(node_id, attempt + 1)
                        if error.code == "cancelled" or _is_cancelled(token):
                            self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                            self._record_cancelled(node_id, last_error, record=record)
                            return result, last_error
                        if not error.retryable:
                            break
                        if attempt < max_retries - 1:
                            retry_event = {
                                "id": node_id,
                                "tool": node.tool_name,
                                "attempt": attempt + 1,
                                "next_attempt": attempt + 2,
                                "result": _event_result(result) if result else None,
                            }
                            _attach_structured_event(retry_event, structured)
                            self._emit_event("tool_retry", retry_event)
                            if retry_delay > 0 and not _wait_retry_delay(token, retry_delay):
                                last_error = "cancelled: 工具重试等待被用户中断"
                                self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                                self._record_cancelled(node_id, last_error, record=record)
                                return result, last_error
                        continue
                    semantic_error = _semantic_tool_error(result)
                    if semantic_error:
                        # Preserve the structured payload for the deterministic
                        # final answer while marking the graph/tool call failed.
                        structured.success = False
                        structured.error = ToolError(
                            "tool_failure", semantic_error, retryable=False
                        )
                        self._tool_results[node_id] = structured
                        self.graph.nodes[node_id].result = result
                        self.graph.set_node_error(node_id, semantic_error)
                        if record:
                            self._record_failure(node_id, semantic_error)
                        return result, semantic_error
                    self.graph.set_node_result(node_id, result)
                    if record:
                        self._record_success(node_id, result)
                    return result, None
                except Exception as exc:
                    # 防御性兜底；正常工具异常应由 _call_tool_attempt 结构化。
                    error = classify_tool_exception(exc, default_retryable=False)
                    last_error = str(error)
                    self.graph.set_node_retry_count(node_id, attempt + 1)
                    failed_result = ToolResult(
                        success=False, error=error, metadata={"backend": "tool"}
                    )
                    self._tool_results[node_id] = failed_result
                    self._tool_attempts.setdefault(node_id, []).append(failed_result)
                    break

            self.graph.set_node_error(node_id, last_error)
            if record:
                self._record_failure(node_id, last_error)
            return self.graph.nodes[node_id].result, last_error

    def _execute_subagent_node(self, token, node_id: str, record: bool = True):
        node = self.graph.nodes[node_id]
        registry = getattr(self.agent, "subagents", None)
        subagent = registry.get(node.tool_name) if registry is not None else None
        if subagent is None:
            error = f"子代理 {node.tool_name} 未注册"
            self.graph.set_node_error(node_id, error)
            if record:
                self._record_failure(node_id, error)
            return "", error

        upstream = {
            f"{dep}:{self.graph.nodes[dep].tool_name}": self.graph.nodes[dep].result
            for dep in node.depends_on or []
            if dep in self.graph.nodes and self.graph.nodes[dep].result
        }
        task = SubAgentTask(
            id=node_id,
            goal=str((node.params or {}).get("goal") or node.name or ""),
            query=str(self.task.get("query") or self.task.get("message") or ""),
            upstream=upstream,
        )
        try:
            setattr(self.agent, "last_subagent_task", task)
        except Exception:
            pass

        # 子 Agent 内部可能串行调用多个 LLM；与 Go 一样只执行一次并放宽到 5 分钟。
        max_retries = 1
        retry_delay = float(getattr(self.agent.cfg, "retry_delay_ms", 0)) / 1000.0
        timeout = max(300.0, float(getattr(self.agent.cfg, "step_timeout_ms", 0) or 0) / 1000.0)
        last_error = ""
        for attempt in range(max_retries):
            if _is_cancelled(token):
                self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                return "", "被用户中断"
            try:
                from internal.tools.tools import Tool
                wrapped = Tool(node.tool_name, "subagent", [], lambda _: str(subagent.run(task)),
                               side_effecting=node.tool_name == "doc_agent")
                from internal.harness.journal import fingerprint
                self._dispatches[node_id] = {'fingerprint': fingerprint({'tool': node.tool_name, 'params': dict(node.params or {})})}
                self._save_snapshot()
                structured = guarded_tool_attempt(self.agent, wrapped, node.tool_name,
                    dict(node.params or {}), token, timeout,
                    invocation_id=f"{self.task.get('task_id', 'graph')}:{node_id}")
                self._tool_results[node_id] = structured
                self._dispatches[node_id].update(error_code=structured.error.code if structured.error else '',
                    uncertain=structured.metadata.get('dispatch_state') == 'uncertain')
                if not structured.success:
                    raise structured.error or ToolError("subagent_failed", "子代理执行失败")
                result = structured.payload
                self.graph.set_node_result(node_id, result)
                if record:
                    self._record_success(node_id, result)
                return result, None
            except Exception as exc:
                error = classify_tool_exception(exc, default_retryable=True)
                last_error = str(error)
                self.graph.set_node_retry_count(node_id, attempt + 1)
                if error.code == "cancelled":
                    self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                    self._record_cancelled(node_id, last_error, record=record)
                    return "", last_error
                if attempt < max_retries - 1 and retry_delay > 0:
                    if not _wait_retry_delay(token, retry_delay):
                        self.graph.set_node_status(node_id, NodeStatus.CANCELLED)
                        return "", "被用户中断"

        self.graph.set_node_error(node_id, last_error)
        if record:
            self._record_failure(node_id, last_error)
        return "", last_error

    def _group_by_race(self, level: List[str]) -> List[tuple]:
        group_map: Dict[str, List[str]] = {}
        no_group: List[str] = []
        for node_id in level:
            group = self.graph.nodes[node_id].race_group
            if group:
                group_map.setdefault(group, []).append(node_id)
            else:
                no_group.append(node_id)
        groups = [(name, ids) for name, ids in sorted(group_map.items())]
        groups.extend(("", [node_id]) for node_id in no_group)
        return groups

    def _record_success(self, node_id: str, result: str) -> None:
        self._save_snapshot()
        node = self.graph.nodes[node_id]
        self._sync_task_step(node_id, status="done", result=result)
        event = {
            "id": node_id,
            "tool": node.tool_name,
            "result": _event_result(result),
            "status": "success",
        }
        _attach_structured_event(event, self._tool_results.get(node_id))
        self._emit_event("tool_result", event)
        self._emit_event("node_done", {"id": node_id, "tool": node.tool_name, "status": "done"})
        self._emit_step("Observation", result)
        self._push_task_mem(StepObservation(
            step_id=_node_step_id(node_id), tool_name=node.tool_name, result=result, success=True,
        ))
        self._record_tool_call(ToolCallTrace(tool_name=node.tool_name, success=True, summary=result))

    def _record_failure(self, node_id: str, error: str) -> None:
        self._save_snapshot()
        node = self.graph.nodes[node_id]
        self._sync_task_step(node_id, status="failed", result=node.result, error=error)
        event = {
            "id": node_id,
            "tool": node.tool_name,
            "result": _event_result(node.result) if node.result else None,
            "error": error,
            "status": "error",
        }
        _attach_structured_event(event, self._tool_results.get(node_id))
        self._emit_event("tool_result", event)
        self._emit_step("Observation", f"{node.tool_name} 失败：{error}")
        self._push_task_mem(StepObservation(
            step_id=_node_step_id(node_id), tool_name=node.tool_name, error=error, success=False,
        ))
        self._record_tool_call(ToolCallTrace(tool_name=node.tool_name, success=False, summary=error))

    def _record_cancelled(self, node_id: str, error: str, *, record: bool) -> None:
        if not record:
            return
        node = self.graph.nodes[node_id]
        self._sync_task_step(node_id, status="cancelled", result=node.result, error=error)
        event = {
            "id": node_id,
            "tool": node.tool_name,
            "result": _event_result(node.result) if node.result else None,
            "error": error,
            "status": "cancelled",
        }
        _attach_structured_event(event, self._tool_results.get(node_id))
        self._emit_event("tool_result", event)
        self._emit_step("Observation", f"{node.tool_name} 已取消：{error}")
        self._push_task_mem(StepObservation(
            step_id=_node_step_id(node_id), tool_name=node.tool_name,
            error=error, success=False,
        ))
        self._record_tool_call(
            ToolCallTrace(tool_name=node.tool_name, success=False, summary=error)
        )

    def _enrich_params_with_upstream(self, node, params: Dict[str, Any], tool) -> Dict[str, Any]:
        upstream = {
            dep: self.graph.nodes[dep].result
            for dep in node.depends_on or []
            if dep in self.graph.nodes and self.graph.nodes[dep].result
        }
        text = "\n\n".join(f"## {key}\n\n{upstream[key]}" for key in sorted(upstream)).strip()
        if not text:
            return params
        placeholders = {"", "{{input}}", "${input}", "<input>", "上游结果", "upstream"}
        for key, value in list(params.items()):
            if isinstance(value, str) and value.strip() in placeholders:
                params[key] = text
        for param in getattr(tool, "params", []) or []:
            name = str(param.get("name") or "")
            if param.get("required") and name and name not in params:
                params[name] = text
        return params

    def _push_task_mem(self, obs: StepObservation) -> None:
        fn = getattr(self.agent, "push_task_mem", None)
        if callable(fn):
            fn(obs)

    def _record_tool_call(self, trace: ToolCallTrace) -> None:
        fn = getattr(self.agent, "record_tool_call", None)
        if callable(fn):
            fn(trace)

    def _emit_step(self, step_type: str, content: str, params: Optional[dict] = None) -> None:
        self._emit_event("step", {
            "type": step_type,
            "content": content,
            "params": params or None,
        })

    def _emit_event(self, event_type: str, data: dict) -> None:
        if not callable(self.on_event):
            return
        self.on_event({
            "type": event_type,
            "data": data,
        })

    def _save_snapshot(self) -> None:
        with self._lock:
            if self._lease is not None:
                self._lease.refresh()
            self.task['recovery'] = {
                'schema': 1, 'user_id': getattr(self.agent, 'user_id', ''),
                'conversation_id': getattr(self.agent, 'conversation_id', '') or 'default',
                'nodes': [asdict(node) for node in self.graph.nodes.values()],
                'dispatches': self._dispatches,
            }
            fn = getattr(self.agent, 'persist_task_checkpoint', None) or getattr(self.agent, 'save_snapshot', None)
            if callable(fn):
                try:
                    fn(self.task)
                except Exception as exc:
                    self._checkpoint_error = type(exc).__name__
                    raise

    def _sync_task_step(
        self, node_id: str, *, status: str, result: str = "", error: str = ""
    ) -> None:
        """Mirror graph state into the task contract shown by the harness UI."""
        steps = self.task.get("steps") if isinstance(self.task, dict) else None
        if not isinstance(steps, list):
            return
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if step.get("node_id") == node_id:
                step["status"] = status
                step["retry_count"] = self.graph.nodes[node_id].retry_count
                if result:
                    step["result"] = result
                if error:
                    step["error"] = error
                self.task["current_step"] = index + 1
                return

    def _build_result(self) -> GraphResult:
        node_results: Dict[str, NodeResult] = {}
        for node_id, node in self.graph.nodes.items():
            structured = self._tool_results.get(node_id)
            node_results[node_id] = NodeResult(
                status=node.status,
                result=node.result,
                error=node.error,
                payload_json=(structured.payload_json if structured is not None else None),
                error_code=(
                    structured.error.code
                    if structured is not None and structured.error is not None else ""
                ),
                retryable=(
                    structured.error.retryable
                    if structured is not None and structured.error is not None else None
                ),
                duration=(structured.duration if structured is not None else 0.0),
                metadata=(dict(structured.metadata) if structured is not None else {}),
                attempts=[
                    attempt.to_dict()
                    for attempt in self._tool_attempts.get(node_id, [])
                ],
            )
        return GraphResult(observations=self.graph.successful_results(), node_results=node_results)

    def _build_interrupted(self, msg: str) -> GraphResult:
        for node in self.graph.nodes.values():
            if node.status in {NodeStatus.PENDING, NodeStatus.RUNNING}:
                node.status = NodeStatus.CANCELLED
        result = self._build_result()
        result.interrupted = True
        result.interrupted_msg = msg
        return result


def _preview_result(tool_name: str, result: str, limit: int = 320) -> str:
    """为 SSE 进度生成可读摘要，避免把完整结果重复塞进页面。"""
    text = " ".join(str(result or "").split())
    if len(text) > limit:
        text = text[:limit] + "…"
    label = f"{tool_name} 执行完成"
    return f"{label}：{text}" if text else label


def _event_result(result: str) -> Any:
    """Keep structured tool output structured in streaming/evaluation traces."""
    try:
        return json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return str(result)


def _semantic_tool_error(result: str) -> str:
    """Recognise tools that explicitly return an auditable failure envelope."""
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "工具返回失败")
    return str(error or payload.get("answer") or "工具返回失败")


def _call_tool_attempt(
    tool: Any,
    params: Dict[str, Any],
    token: Any,
    timeout: float,
) -> ToolResult:
    """执行一次工具调用，并保留结构化错误与观测字段。"""
    started = time.perf_counter()
    context = ToolCallContext(token=token, timeout_seconds=timeout)
    early_failure = context.failure()
    if early_failure is not None:
        return ToolResult(
            success=False, error=early_failure,
            duration=time.perf_counter() - started, metadata={"backend": "tool"},
        )

    structured_fn = getattr(tool, "execute_structured", None)
    if callable(structured_fn):
        try:
            result = _call_with_timeout(
                lambda: structured_fn(context, params), timeout, token
            )
        except Exception as exc:
            error = classify_tool_exception(exc, default_retryable=False)
            return ToolResult(
                success=False, error=error,
                duration=time.perf_counter() - started, metadata={"backend": "tool"},
            )
        if not isinstance(result, ToolResult):
            error = ToolError(
                "internal", "execute_structured 必须返回 ToolResult", retryable=False
            )
            return ToolResult(
                success=False, error=error,
                duration=time.perf_counter() - started, metadata={"backend": "tool"},
            )
        if result.duration <= 0:
            result.duration = time.perf_counter() - started
        context_failure = context.failure()
        if result.success and context_failure is not None:
            result.success = False
            result.error = context_failure
        return result

    execute_ctx = getattr(tool, "execute_ctx", None)
    try:
        if callable(execute_ctx):
            payload = _call_with_timeout(
                lambda: execute_ctx(context, params), timeout, token
            )
        else:
            payload = _call_with_timeout(lambda: _call_tool(tool, params), timeout, token)
    except Exception as exc:
        # Go 的旧 Execute/ExecuteCtx 没有结构化分类，历史上默认允许重试；
        # 但可识别的参数、HTTP 4xx 与取消仍必须立即停止。
        error = classify_tool_exception(exc, default_retryable=True)
        return ToolResult(
            success=False, error=error,
            duration=time.perf_counter() - started, metadata={"backend": "tool"},
        )
    if str(payload) == "[已中断]":
        error = context.failure() or ToolError(
            "cancelled", "工具返回中断状态", retryable=False
        )
        return ToolResult(
            success=False, payload=str(payload), error=error,
            duration=time.perf_counter() - started, metadata={"backend": "tool"},
        )
    return ToolResult(
        success=True, payload=str(payload),
        duration=time.perf_counter() - started, metadata={"backend": "tool"},
    )


def _display_tool_error(tool: Any, error: ToolError) -> str:
    # 旧 Execute 的普通异常在 Go 中保持原始文本；结构化接口保留 code 前缀。
    if not callable(getattr(tool, "execute_structured", None)) and error.code == "internal":
        return error.message
    return str(error)


def _call_tool(tool, params: Dict[str, Any]) -> str:
    if hasattr(tool, "func"):
        return str(tool.func(params))
    execute = getattr(tool, "execute", None)
    if callable(execute):
        return str(execute(params))
    raise RuntimeError("tool has no callable func/execute")


def _call_with_timeout(fn, timeout: float, token: Any = None) -> Any:
    """可取消地等待调用；不会声称能强杀已经运行的 Python 阻塞线程。"""
    if _is_cancelled(token):
        raise ToolError("cancelled", "工具调用被用户中断", retryable=False)
    if timeout <= 0 and token is None:
        return fn()

    outcome: queue.Queue = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            outcome.put((True, fn()))
        except BaseException as exc:
            outcome.put((False, exc))

    # daemon 线程让调度器能停止等待；不支持 context 的底层阻塞调用仍可能继续。
    threading.Thread(target=inherit_context(runner), name="graph-step", daemon=True).start()
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        if _is_cancelled(token):
            raise ToolError("cancelled", "工具调用被用户中断", retryable=False)
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise ToolError(
                "timeout", f"步骤执行超过 {int(timeout * 1000)}ms", retryable=True
            )
        wait_for = 0.01 if remaining is None else min(0.01, remaining)
        try:
            succeeded, value = outcome.get(timeout=max(0.001, wait_for))
        except queue.Empty:
            continue
        if succeeded:
            return value
        raise value


def _wait_retry_delay(token: Any, delay: float) -> bool:
    deadline = time.monotonic() + max(0.0, delay)
    while time.monotonic() < deadline:
        if _is_cancelled(token):
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    return not _is_cancelled(token)


def _attach_structured_event(event: Dict[str, Any], result: Optional[ToolResult]) -> None:
    if result is None:
        return
    event["duration_ms"] = round(result.duration * 1000.0, 3)
    event["metadata"] = dict(result.metadata)
    if result.payload_json is not None:
        event["payload_json"] = result.payload_json
    if result.error is not None:
        event["error_detail"] = result.error.to_dict()


def _is_cancelled(token) -> bool:
    return bool(token is not None and callable(getattr(token, "is_cancelled", None)) and token.is_cancelled())


def _node_step_id(node_id: str) -> int:
    if node_id.startswith("n"):
        try:
            return int(node_id[1:])
        except ValueError:
            return 0
    return 0


def _node_dict(node) -> dict:
    is_subagent = getattr(node.type, "value", node.type) == "sub_agent"
    item = {
        "id": node.id, "type": getattr(node.type, "value", str(node.type)), "name": node.name,
        "params": dict(node.params or {}),
        "depends_on": list(node.depends_on or []), "race_group": node.race_group,
        "optional_depends_on": list(node.optional_depends_on or []),
        "status": getattr(node.status, "value", str(node.status)), "result": node.result, "error": node.error,
        "retry_count": node.retry_count,
    }
    if is_subagent:
        item["agent_name"] = node.tool_name
        item["goal"] = str((node.params or {}).get("goal") or "")
    else:
        item["tool_name"] = node.tool_name
    return item
