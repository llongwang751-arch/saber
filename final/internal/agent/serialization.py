"""Pure Agent event, graph and tool-result serialization."""

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from .contracts import ReActStep, StepType


def _param_string(params: Dict[str, Any], key: str) -> str:
    if not isinstance(params, dict):
        return ""
    value = params.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _param_string_default(params: Dict[str, Any], key: str, fallback: str) -> str:
    value = _param_string(params, key)
    return value if value else fallback


def _param_bool(params: Dict[str, Any], key: str) -> bool:
    if not isinstance(params, dict):
        return False
    value = params.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "on"}
    return bool(value)


def _json_string(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, indent=2)


def _latest_structured_tool_result(steps: List[ReActStep]) -> Optional[Dict[str, Any]]:
    for step in reversed(steps or []):
        if step.type != StepType.OBSERVATION:
            continue
        try:
            payload = json.loads(step.content or "")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        evidence = payload.get("evidence")
        if (
            isinstance(evidence, dict)
            and payload.get("intent")
            and "answer" in payload
        ):
            return payload
    return None


def _graph_to_contract(graph) -> Dict[str, Any]:
    nodes: Dict[str, Any] = {}
    for node_id, node in graph.nodes.items():
        is_subagent = getattr(node.type, "value", node.type) == "sub_agent"
        item = {
            "id": node.id,
            "type": getattr(node.type, "value", str(node.type)),
            "name": node.name,
            "params": dict(node.params or {}),
            "depends_on": list(node.depends_on or []),
            "optional_depends_on": list(node.optional_depends_on or []),
            "status": getattr(node.status, "value", str(node.status)),
            "retry_count": node.retry_count,
        }
        if is_subagent:
            item["agent_name"] = node.tool_name
            item["goal"] = str((node.params or {}).get("goal") or "")
        else:
            item["tool_name"] = node.tool_name
        if node.race_group:
            item["race_group"] = node.race_group
        if node.result:
            item["result"] = node.result
        if node.error:
            item["error"] = node.error
        nodes[node_id] = item
    return {
        "nodes": nodes,
        "adj_list": {key: list(value) for key, value in graph.adj.items()},
        "in_degree": dict(graph.indegree),
    }


def _graph_to_react_steps(graph) -> List[ReActStep]:
    steps: List[ReActStep] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        steps.append(ReActStep(type=StepType.THOUGHT, content=node.name))
        steps.append(
            ReActStep(
                type=StepType.ACTION,
                content=f"调用 {node.tool_name}",
                tool=node.tool_name,
                params=dict(node.params or {}),
            )
        )
        status = getattr(node.status, "value", str(node.status))
        if status == "done":
            content = node.result
        elif status == "failed":
            # A semantic-failure envelope contains the safe user-facing answer
            # and evidence.  Keep it available to the response/adapter layer.
            content = node.result or f"执行失败: {node.error}"
        elif status == "skipped":
            content = "[竞速跳过] 其他节点已胜出"
        elif status == "cancelled":
            content = "[已中断]"
        else:
            continue
        steps.append(ReActStep(type=StepType.OBSERVATION, content=content))
    return steps


def _emit(on_event, event_type: str, data: Any) -> None:
    if on_event is None:
        return
    on_event({"type": event_type, "data": _to_jsonable(data)})


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return _to_jsonable(asdict(value))
    return value
