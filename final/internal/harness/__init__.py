"""Harness Runtime 2.0 package.

Inspired by deepseek-harness's modular event-sourcing and plugin-based design.
"""

from internal.harness.events import (
    BaseEventStream,
    EventType,
    HarnessEvent,
    JSONLEventStream,
    MemoryEventStream,
    SqliteEventStream,
)
from internal.harness.loops import (
    BaseLoopPlugin,
    DAGLoopPlugin,
    DirectChatLoopPlugin,
    ReActLoopPlugin,
)
from internal.harness.plugins import (
    AuditLogPlugin,
    DependencyFallbackPlugin,
    HarnessContext,
    HarnessPlugin,
    ResiliencePlugin,
)
from internal.harness.runtime import HarnessResult, HarnessRuntime

__all__ = [
    "EventType",
    "HarnessEvent",
    "BaseEventStream",
    "MemoryEventStream",
    "JSONLEventStream",
    "SqliteEventStream",
    "HarnessPlugin",
    "HarnessContext",
    "ResiliencePlugin",
    "DependencyFallbackPlugin",
    "AuditLogPlugin",
    "BaseLoopPlugin",
    "DirectChatLoopPlugin",
    "ReActLoopPlugin",
    "DAGLoopPlugin",
    "HarnessRuntime",
    "HarnessResult",
]
