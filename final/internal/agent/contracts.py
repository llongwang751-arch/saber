"""Stable Agent request/response contracts without runtime or storage imports."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class StepType:
    THOUGHT = "Thought"
    ACTION = "Action"
    OBSERVATION = "Observation"
    FINAL_ANSWER = "Final Answer"


@dataclass
class ReActStep:
    type: str
    content: str
    tool: str = ""
    params: Optional[Dict[str, str]] = None


@dataclass
class ChatOptions:
    use_rag: bool = False
    conversation_id: str = ""


@dataclass(frozen=True)
class RequestExecutionContext:
    """Server-created per-request context; it is not part of the chat wire API.

    Online experiments may only inject a pre-compiled allowlist here.  Never
    mutate ``UnifiedAgent.cfg``: one Agent instance can serve concurrent
    requests and a shared mutation would mix variants.
    """

    runtime_overrides: Dict[str, Any] = field(default_factory=dict)
    experiment_exposure_id: str = ""
    runtime_strategy_checksum: str = ""
    trace_id: str = ""


@dataclass
class Response:
    query: str
    answer: str = ""
    mode: str = "chat"
    # ``mode`` describes the execution path (react/rag); these fields carry
    # the business semantics extracted by deterministic domain tools.
    intent: Optional[str] = None
    slots: Dict[str, Any] = field(default_factory=dict)
    fallback: bool = False
    error: Optional[str] = None
    steps: List[ReActStep] = field(default_factory=list)
    tool_call: Optional[Dict[str, Any]] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    search_results: List[dict] = field(default_factory=list)
    rag_trace: Dict[str, Any] = field(default_factory=dict)
    task: Optional[dict] = None
    extracted_info: str = ""
    short_term_count: int = 0
    long_term_count: int = 0
    preferences: Dict[str, str] = field(default_factory=dict)
    interrupted: bool = False
    trace_id: str = ""
    # The public HTTP response only exposes the opaque exposure id.  The
    # strategy checksum is retained in the tenant-scoped trace for audit.
    experiment_exposure_id: str = ""
    runtime_strategy_checksum: str = ""
