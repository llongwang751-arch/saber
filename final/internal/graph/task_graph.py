from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class NodeType(str, Enum):
    TOOL = "tool"
    SUBAGENT = "sub_agent"
    LLM = "llm"
    THINK = "think"
    AGGREGATE = "aggregate"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass
class Node:
    id: str
    type: NodeType = NodeType.TOOL
    name: str = ""
    tool_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    optional_depends_on: List[str] = field(default_factory=list)
    race_group: str = ""
    status: NodeStatus = NodeStatus.PENDING
    result: str = ""
    error: str = ""
    retry_count: int = 0


class TaskGraph:
    """有向无环任务图，通过拓扑层级决定并行调度顺序。"""

    def __init__(self, nodes: List[Node]):
        self.nodes: Dict[str, Node] = {node.id: node for node in nodes}
        self.adj: Dict[str, List[str]] = {node.id: [] for node in nodes}
        self.indegree: Dict[str, int] = {node.id: 0 for node in nodes}
        for node in nodes:
            for dep in node.depends_on or []:
                if dep in self.adj:
                    self.adj[dep].append(node.id)
                    self.indegree[node.id] = self.indegree.get(node.id, 0) + 1

    def validate(self) -> None:
        for node in self.nodes.values():
            for dep in node.depends_on or []:
                if dep not in self.nodes:
                    raise ValueError(f"missing dependency {dep} for node {node.id}")
            unknown_optional = set(node.optional_depends_on or []) - set(node.depends_on or [])
            if unknown_optional:
                raise ValueError(
                    f"optional dependencies must also appear in depends_on for node {node.id}: "
                    f"{sorted(unknown_optional)}"
                )
        self.topological_levels()

    def topological_levels(self) -> List[List[str]]:
        self._check_missing_dependencies()
        indegree = dict(self.indegree)
        ready = sorted([node_id for node_id, degree in indegree.items() if degree == 0])
        levels: List[List[str]] = []
        visited = 0

        while ready:
            level = ready
            levels.append(level)
            visited += len(level)
            next_ready: List[str] = []
            for node_id in level:
                for child in sorted(self.adj.get(node_id, [])):
                    indegree[child] -= 1
                    if indegree[child] == 0:
                        next_ready.append(child)
            ready = sorted(next_ready)

        if visited != len(self.nodes):
            raise ValueError("cycle detected in task graph")
        return levels

    def ready_nodes(self) -> List[str]:
        failure_terminal = {NodeStatus.FAILED, NodeStatus.CANCELLED, NodeStatus.SKIPPED}
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node.status != NodeStatus.PENDING:
                    continue
                optional = set(node.optional_depends_on or [])
                failed_required = [
                    dep for dep in node.depends_on
                    if dep not in optional and self.nodes[dep].status in failure_terminal
                ]
                if failed_required:
                    node.status = NodeStatus.SKIPPED
                    node.error = "required dependency failed: " + ", ".join(sorted(failed_required))
                    changed = True

        ready: List[str] = []
        terminal = {NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.CANCELLED, NodeStatus.SKIPPED}
        for node_id, node in self.nodes.items():
            if node.status != NodeStatus.PENDING:
                continue
            deps_done = all(self.nodes[dep].status in terminal for dep in node.depends_on)
            required = set(node.depends_on or []) - set(node.optional_depends_on or [])
            required_ok = all(self.nodes[dep].status == NodeStatus.DONE for dep in required)
            if deps_done and required_ok:
                ready.append(node_id)
        return sorted(ready)

    def mark_done(self, node_id: str) -> List[str]:
        self.set_node_status(node_id, NodeStatus.DONE)
        return self.ready_nodes()

    def race_groups(self) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = {}
        for node in self.nodes.values():
            if not node.race_group:
                continue
            groups.setdefault(node.race_group, []).append(node.id)
        return groups

    def set_node_status(self, node_id: str, status: NodeStatus) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].status = status

    def set_node_result(self, node_id: str, result: str) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].result = result
            self.nodes[node_id].status = NodeStatus.DONE

    def set_node_error(self, node_id: str, error: str) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].error = error
            self.nodes[node_id].status = NodeStatus.FAILED

    def set_node_retry_count(self, node_id: str, count: int) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].retry_count = count

    def successful_results(self) -> List[str]:
        return [node.result for node in self.nodes.values() if node.status == NodeStatus.DONE and node.result]

    def add_nodes(self, nodes: List[Node]) -> List[str]:
        """运行期追加重规划节点，并重建依赖索引。冲突 ID 由调用方先处理。"""
        added: List[str] = []
        for node in nodes:
            if not node.id or node.id in self.nodes:
                continue
            self.nodes[node.id] = node
            added.append(node.id)
        self._rebuild_edges()
        return added

    def summary(self) -> str:
        parts = []
        for node in self.nodes.values():
            parts.append(f"{node.id}:{node.tool_name}:{node.status.value}")
        return "\n".join(parts)

    def _check_missing_dependencies(self) -> None:
        for node in self.nodes.values():
            for dep in node.depends_on or []:
                if dep not in self.nodes:
                    raise ValueError(f"missing dependency {dep} for node {node.id}")

    def _rebuild_edges(self) -> None:
        self.adj = {node_id: [] for node_id in self.nodes}
        self.indegree = {node_id: 0 for node_id in self.nodes}
        for node in self.nodes.values():
            for dep in node.depends_on or []:
                if dep in self.adj:
                    self.adj[dep].append(node.id)
                    self.indegree[node.id] += 1
