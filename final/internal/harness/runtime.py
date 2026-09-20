"""Harness Runtime 2.0 Core Engine.

Inspired by deepseek-harness's modular orchestrator, this engine coordinates
hot-swappable plugins, pluggable loops, and immutable event streams.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from internal.harness.events import (
    BaseEventStream,
    EventType,
    HarnessEvent,
    JSONLEventStream,
    MemoryEventStream,
    SqliteEventStream,
)
from internal.harness.loops import BaseLoopPlugin, ReActLoopPlugin
from internal.harness.plugins import (
    AuditLogPlugin,
    DependencyFallbackPlugin,
    HarnessContext,
    HarnessPlugin,
    ResiliencePlugin,
)

from .execution import start_session, finish_session

logger = logging.getLogger(__name__)


@dataclass
class HarnessResult:
    session_id: str
    status: str
    output: Any
    steps: int = 0
    events: List[HarnessEvent] = field(default_factory=list)
    interrupted: bool = False
    interrupted_reason: str = ""


class HarnessRuntime:
    """Universal Harness Runtime supporting lightweight and enterprise dual-modes."""

    def __init__(
        self,
        event_stream: Optional[BaseEventStream] = None,
        llm_fn: Optional[Callable[[str], str]] = None,
    ):
        self.event_stream = event_stream or MemoryEventStream()
        self.llm_fn = llm_fn or (lambda q: f"Echo: {q}")
        self._plugins: List[HarnessPlugin] = []
        self._tools: Dict[str, Any] = {}
        self._loop_plugin: BaseLoopPlugin = ReActLoopPlugin()

    def register_plugin(self, plugin: HarnessPlugin) -> None:
        self._plugins.append(plugin)
        # Sort by priority
        self._plugins.sort(key=lambda p: getattr(p, "priority", 100))

    def set_loop(self, loop_plugin: BaseLoopPlugin) -> None:
        self._loop_plugin = loop_plugin

    def register_tool(self, name: str, tool_callable: Any) -> None:
        self._tools[name] = tool_callable

    def run(
        self,
        query: str,
        session_id: Optional[str] = None,
        user_id: str = "default_user",
        initial_state: Optional[Dict[str, Any]] = None,
    ) -> HarnessResult:
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        ctx = HarnessContext(
            session_id=sid,
            user_id=user_id,
            query=query,
            event_stream=self.event_stream,
            state=initial_state or {},
            plugins=list(self._plugins),
        )

        start_session(ctx)
        output = None
        status = "success"
        try:
            if ctx.interrupted:
                output = {"status": "interrupted", "answer": ctx.state.get("final_answer", "")}
            else:
                output = self._loop_plugin.execute_loop(ctx=ctx, tools=self._tools, llm_fn=self.llm_fn, resume_from_step=0)
            output = finish_session(ctx, output)
            if ctx.interrupted:
                status = "interrupted"
            elif isinstance(output, dict) and output.get("status") in {"error", "failed"}:
                status = "failed"
        except Exception as exc:
            status = "failed"
            ctx.interrupted = True
            ctx.interrupted_reason = str(exc)
            ctx.emit(EventType.ERROR, {"error": str(exc), "stage": "loop_execution"})

        events = self.event_stream.get_events(sid)
        return HarnessResult(
            session_id=sid,
            status=status,
            output=output,
            steps=len([e for e in events if e.type in (EventType.TOOL_CALL, EventType.REASONING)]),
            events=events,
            interrupted=ctx.interrupted,
            interrupted_reason=ctx.interrupted_reason,
        )

    def resume(self, session_id: str) -> HarnessResult:
        """Resume execution from the last recorded checkpoint (Crash Resilience)."""
        last_ckpt = self.event_stream.get_last_checkpoint(session_id)
        if not last_ckpt:
            raise ValueError(f"No checkpoint found for session {session_id} to resume from.")

        if last_ckpt.payload.get("status") == "dispatching":
            return HarnessResult(session_id=session_id, status="interrupted", output=None,
                interrupted=True, interrupted_reason="Prior dispatch is uncertain; automatic replay refused",
                events=self.event_stream.get_events(session_id))

        if last_ckpt.payload.get("status") == "completed":
            return HarnessResult(session_id=session_id, status="resumed_success",
                output=last_ckpt.payload.get("output") or {"status": "success", "answer": last_ckpt.payload.get("final_answer", "")},
                events=self.event_stream.get_events(session_id))
        if "completed_nodes" in last_ckpt.payload and last_ckpt.payload.get("dag_schema") != 1:
            raise ValueError("DAG checkpoint lacks a durable graph snapshot; unsafe automatic resume refused")

        step_to_resume = last_ckpt.payload.get("step_index", 0)
        saved_observations = last_ckpt.payload.get("observations", [])

        events = self.event_stream.get_events(session_id)
        # Find original user query
        query = ""
        user_id = "default_user"
        for ev in events:
            if ev.type == EventType.USER_INPUT:
                query = ev.payload.get("query", "")
            if ev.type == EventType.SESSION_START:
                user_id = ev.payload.get("user_id", "default_user")

        ctx = HarnessContext(
            session_id=session_id,
            user_id=user_id,
            query=query,
            event_stream=self.event_stream,
            state={"observations": saved_observations, "pending_action": last_ckpt.payload.get("pending_action")},
            plugins=list(self._plugins),
        )
        loop = self._loop_plugin
        if last_ckpt.payload.get("dag_schema") == 1:
            from internal.graph.task_graph import Node, NodeStatus, NodeType, TaskGraph
            from internal.harness.loops import DAGLoopPlugin
            nodes = [Node(**{**row, "status": NodeStatus(row["status"]), "type": NodeType(row["type"])})
                     for row in last_ckpt.payload["graph"]]
            graph = TaskGraph(nodes)
            graph.validate()
            ctx.state["task_graph"] = graph
            ctx.query = last_ckpt.payload["query"]
            ctx.user_id = last_ckpt.payload["user_id"]
            loop = DAGLoopPlugin(max_parallel=last_ckpt.payload.get("max_parallel", 2))
        ctx.emit(EventType.STATE_CHANGE, {
            "action": "session_resumed",
            "from_step": step_to_resume,
        }, step_index=step_to_resume)

        start_session(ctx)
        if ctx.interrupted:
            return HarnessResult(session_id=session_id, status="interrupted", output=None, interrupted=True, interrupted_reason=ctx.interrupted_reason)
        output = loop.execute_loop(
            ctx=ctx,
            tools=self._tools,
            llm_fn=self.llm_fn,
            resume_from_step=step_to_resume,
        )

        output = finish_session(ctx, output)
        all_events = self.event_stream.get_events(session_id)
        return HarnessResult(
            session_id=session_id,
            status="interrupted" if ctx.interrupted else "failed" if isinstance(output, dict) and output.get("status") in {"error", "failed"} else "resumed_success",
            output=output,
            steps=len(all_events),
            events=all_events,
            interrupted=ctx.interrupted,
            interrupted_reason=ctx.interrupted_reason,
        )

    def replay(self, session_id: str, stop_at_event_id: Optional[str] = None) -> List[HarnessEvent]:
        """Replay history up to a specific event."""
        return self.event_stream.replay(session_id, stop_at_event_id=stop_at_event_id)

    def fork(self, source_session_id: str, fork_at_event_id: str, new_session_id: Optional[str] = None) -> str:
        """Fork a new parallel session from history."""
        target_sid = new_session_id or f"fork_{uuid.uuid4().hex[:12]}"
        self.event_stream.fork(source_session_id, fork_at_event_id, target_sid)
        return target_sid

    @classmethod
    def create_lightweight(
        cls,
        llm_fn: Optional[Callable[[str], str]] = None,
        storage_dir: Optional[str | Path] = None,
    ) -> HarnessRuntime:
        """Factory: Lightweight runtime with zero external dependencies."""
        stream = JSONLEventStream(storage_dir) if storage_dir else MemoryEventStream()
        runtime = cls(event_stream=stream, llm_fn=llm_fn)
        runtime.register_plugin(ResiliencePlugin(max_retries=2, retry_delay_ms=50))
        runtime.register_plugin(DependencyFallbackPlugin())
        runtime.register_plugin(AuditLogPlugin())
        return runtime

    @classmethod
    def create_enterprise(
        cls,
        db_path: str = "harness_events.db",
        llm_fn: Optional[Callable[[str], str]] = None,
    ) -> HarnessRuntime:
        """Factory: Enterprise runtime backed by SQLite/relational persistence."""
        stream = SqliteEventStream(db_path)
        runtime = cls(event_stream=stream, llm_fn=llm_fn)
        runtime.register_plugin(ResiliencePlugin(max_retries=3, retry_delay_ms=200, failure_threshold=5))
        runtime.register_plugin(DependencyFallbackPlugin())
        runtime.register_plugin(AuditLogPlugin())
        return runtime
