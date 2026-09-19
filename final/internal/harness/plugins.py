"""Microkernel and Plugin Architecture for Harness Runtime 2.0.

Inspired by deepseek-harness's 'Everything is a Plugin' philosophy,
this module implements lifecycle hooks, resilient tool interception,
circuit breaking, and dependency fallback as hot-swappable plugins.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from internal.harness.events import BaseEventStream, EventType, HarnessEvent
from internal.resilience.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


@dataclass
class HarnessContext:
    session_id: str
    user_id: str = "default_user"
    query: str = ""
    event_stream: Optional[BaseEventStream] = None
    state: Dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    interrupted_reason: str = ""
    plugins: List[Any] = field(default_factory=list)

    def emit(self, event_type: EventType, payload: Dict[str, Any], step_index: int = 0) -> None:
        if self.event_stream:
            import uuid
            ev = HarnessEvent(
                session_id=self.session_id,
                event_id=f"ev_{uuid.uuid4().hex[:10]}",
                type=event_type,
                payload=payload,
                step_index=step_index,
                timestamp=time.time(),
            )
            self.event_stream.append(ev)


class HarnessPlugin:
    """Base class for all Harness plugins with standard lifecycle hooks."""

    name: str = "base_plugin"
    priority: int = 100  # Lower number executes first

    def on_session_start(self, ctx: HarnessContext) -> None:
        pass

    def on_before_step(self, ctx: HarnessContext, step_index: int) -> None:
        pass

    def on_tool_execute(self, ctx: HarnessContext, tool_name: str, params: Dict[str, Any]) -> Tuple[bool, Optional[Any]]:
        """Return (should_intercept, intercepted_result). If true, tool execution is skipped."""
        return False, None

    def on_tool_result(self, ctx: HarnessContext, tool_name: str, result: Any) -> Any:
        """Post-process tool execution results."""
        return result

    def on_error(self, ctx: HarnessContext, error: Exception, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle execution errors. If returns a dict, error is treated as recovered/fallback."""
        return None

    def on_session_end(self, ctx: HarnessContext, final_result: Any) -> None:
        pass


class ResiliencePlugin(HarnessPlugin):
    """Resilience plugin: Circuit Breaker, classified retries, and exponential backoff."""

    name = "resilience"
    priority = 10

    def __init__(
        self,
        max_retries: int = 2,
        retry_delay_ms: int = 100,
        failure_threshold: int = 3,
        cooldown_seconds: float = 10.0,
    ):
        self.max_retries = max_retries
        self.retry_delay_ms = retry_delay_ms
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
        )

    def is_retryable(self, error: Exception) -> bool:
        """Classify errors: transient network / 5xx / timeout are retryable; 4xx / validation are not."""
        err_msg = str(error).lower()
        if "timeout" in err_msg or "connection" in err_msg or "503" in err_msg or "500" in err_msg:
            return True
        if "invalid" in err_msg or "validation" in err_msg or "permission" in err_msg or "cancel" in err_msg:
            return False
        # Default retry for standard runtime transient errors
        return isinstance(error, (TimeoutError, ConnectionError, IOError))

    def on_tool_execute(self, ctx: HarnessContext, tool_name: str, params: Dict[str, Any]) -> Tuple[bool, Optional[Any]]:
        # Check circuit breaker state
        if not self.circuit_breaker.allow_request():
            ctx.emit(EventType.ERROR, {
                "tool": tool_name,
                "error": "Circuit breaker is OPEN; execution rejected for cooling down",
            })
            return True, {"error": "circuit_breaker_open", "status": "tripped"}
        return False, None

    def record_success(self) -> None:
        self.circuit_breaker.record_success()

    def record_failure(self) -> None:
        self.circuit_breaker.record_failure()


class DependencyFallbackPlugin(HarnessPlugin):
    """Enforces soft vs hard failure semantics for task dependencies."""

    name = "dependency_fallback"
    priority = 20

    def on_error(self, ctx: HarnessContext, error: Exception, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        is_optional = context.get("is_optional", False)
        node_id = context.get("node_id", "unknown")
        if is_optional:
            logger.warning(f"Optional dependency '{node_id}' failed, activating soft fallback: {error}")
            ctx.emit(EventType.STATE_CHANGE, {
                "node_id": node_id,
                "status": "degraded_fallback",
                "error": str(error),
            })
            return {"status": "degraded", "fallback": True, "error": str(error)}
        return None


class AuditLogPlugin(HarnessPlugin):
    """Audit and trajectory logging plugin."""

    name = "audit_log"
    priority = 50

    def on_session_start(self, ctx: HarnessContext) -> None:
        ctx.emit(EventType.SESSION_START, {
            "query": ctx.query,
            "user_id": ctx.user_id,
        })

    def on_session_end(self, ctx: HarnessContext, final_result: Any) -> None:
        ctx.emit(EventType.SESSION_END, {
            "result_summary": str(final_result)[:200] if final_result else "",
            "interrupted": ctx.interrupted,
            "interrupted_reason": ctx.interrupted_reason,
        })
