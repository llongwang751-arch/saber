"""Typed contracts for deterministic Agent evaluation.

The models in this module deliberately describe workflow expectations instead
of medical truth.  Clinical labels and policies must come from qualified,
versioned sources before they are used in a real evaluation dataset.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RiskSeverity(str, Enum):
    """Severity used by release gates.  S0 is the most severe level."""

    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"


class ReleaseGate(_StrictModel):
    """Validated thresholds for one offline evaluation run.

    Keeping this contract typed prevents malformed or unknown thresholds from
    silently turning a release recommendation green.  It is deliberately an
    offline quality gate; it does not represent online experiment traffic.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
    )

    minimum_pass_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    maximum_error_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    maximum_p95_latency_ms: float = Field(default=5000.0, gt=0.0, le=3_600_000.0)

    @field_validator(
        "minimum_pass_rate",
        "maximum_error_rate",
        "maximum_p95_latency_ms",
        mode="before",
    )
    @classmethod
    def _reject_boolean_thresholds(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("release gate thresholds must be numeric")
        return value


class StrategyVersion(_StrictModel):
    """Immutable, content-addressed offline strategy snapshot."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    manifest: dict[str, Any] = Field(min_length=1)
    manifest_canonical_json: str = Field(min_length=2)
    manifest_checksum: str = Field(min_length=64, max_length=64)
    creator: str = Field(min_length=1, max_length=200)
    source: Literal["offline_eval"] = "offline_eval"
    created_at: datetime


class PromotionProposal(_StrictModel):
    """Human-reviewed recommendation produced only from paired offline runs."""

    id: str = Field(min_length=1)
    baseline_run_id: str = Field(min_length=1)
    candidate_run_id: str = Field(min_length=1)
    candidate_strategy_version_id: str = Field(min_length=1)
    status: Literal["proposed", "blocked", "approved", "rejected", "activated"]
    comparison: dict[str, Any] = Field(default_factory=dict)
    statistics: dict[str, Any] = Field(default_factory=dict)
    release_gate: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)
    blocked_reasons: list[str] = Field(default_factory=list)
    created_by: str = Field(min_length=1, max_length=200)
    reviewed_by: str = ""
    review_note: str = ""
    reviewed_at: datetime | None = None
    activated_by: str = ""
    activation_note: str = ""
    activated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    scope: Literal["offline_evaluation"] = "offline_evaluation"
    auto_activate: Literal[False] = False


class TraceEventType(str, Enum):
    USER_MESSAGE = "user_message"
    INTENT_PREDICTED = "intent_predicted"
    SLOT_EXTRACTED = "slot_extracted"
    RETRIEVAL = "retrieval"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    GUARDRAIL = "guardrail"
    FINAL_RESPONSE = "final_response"
    FALLBACK = "fallback"
    ERROR = "error"


class MetricDimension(str, Enum):
    INTENT = "intent"
    INFORMATION = "information_collection"
    TOOL = "tool_call"
    RESPONSE = "response"
    RAG = "rag"
    FALLBACK = "fallback"
    SAFETY = "safety"
    TRACE = "trace"
    MEMORY = "memory"
    HARNESS = "harness"


class ConversationTurn(_StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolExpectation(_StrictModel):
    """Expected tool and the deterministic constraints on its arguments.

    ``required_arguments`` only checks presence. ``expected_arguments`` checks
    both presence and exact value (strings are compared case-insensitively after
    trimming).  The latter is suitable for synthetic, explicitly labelled data.
    """

    name: str = Field(min_length=1)
    required_arguments: list[str] = Field(default_factory=list)
    expected_arguments: dict[str, Any] = Field(default_factory=dict)
    expected_status: Literal["success", "error", "timeout", "skipped"] | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("required_arguments")
    @classmethod
    def _normalise_argument_names(cls, values: list[str]) -> list[str]:
        return _unique_non_empty(values, "required argument")


class ToolCall(_StrictModel):
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "error", "timeout", "skipped"] = "success"
    result: Any | None = None
    error: str | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return value.strip()


class ForbiddenContent(_StrictModel):
    """A dataset-owned boundary rule; it is not a clinical judgement."""

    rule_id: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    severity: RiskSeverity = RiskSeverity.S1
    is_regex: bool = False
    description: str = ""

    @field_validator("rule_id", "pattern")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        return value.strip()


class Expected(_StrictModel):
    """Deterministic oracle attached to one evaluation case."""

    intents: list[str] = Field(default_factory=list)
    required_slots: list[str] = Field(default_factory=list)
    tool_calls: list[ToolExpectation] = Field(default_factory=list)
    required_content: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    require_fallback: bool = False
    privacy_values: list[str] = Field(default_factory=list)
    forbidden_content: list[ForbiddenContent] = Field(default_factory=list)
    required_trace_events: list[str] = Field(default_factory=list)
    allow_extra_tools: bool = False
    allow_extra_evidence: bool = False
    retrieval_k: int = Field(default=5, ge=1, le=100)
    min_recall_at_k: float = Field(default=1.0, ge=0.0, le=1.0)
    min_mrr: float = Field(default=0.0, ge=0.0, le=1.0)
    min_ndcg_at_k: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_relevance: dict[str, int] = Field(default_factory=dict)
    answerable: bool | None = None
    required_trace_order: list[str] = Field(default_factory=list)
    required_memory_reads: list[str] = Field(default_factory=list)
    required_memory_writes: list[str] = Field(default_factory=list)
    forbidden_memory_reads: list[str] = Field(default_factory=list)
    forbidden_memory_writes: list[str] = Field(default_factory=list)

    @field_validator(
        "intents",
        "required_slots",
        "required_content",
        "evidence_ids",
        "privacy_values",
        "required_trace_events",
        "required_trace_order",
        "required_memory_reads",
        "required_memory_writes",
        "forbidden_memory_reads",
        "forbidden_memory_writes",
    )
    @classmethod
    def _normalise_string_lists(cls, values: list[str]) -> list[str]:
        return _unique_non_empty(values, "expectation")

    @field_validator("required_trace_events", "required_trace_order")
    @classmethod
    def _normalise_trace_event_names(cls, values: list[str]) -> list[str]:
        return [value.casefold() for value in values]

    @field_validator("evidence_relevance")
    @classmethod
    def _validate_relevance(cls, values: dict[str, int]) -> dict[str, int]:
        result: dict[str, int] = {}
        for raw_key, raw_grade in values.items():
            key = str(raw_key).strip()
            grade = int(raw_grade)
            if not key:
                raise ValueError("evidence relevance id must not be empty")
            if grade < 0:
                raise ValueError("evidence relevance grade must be non-negative")
            result[key] = grade
        return result


class EvalCase(_StrictModel):
    case_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    turns: list[ConversationTurn] = Field(default_factory=list)
    expected: Expected
    risk_tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("case_id", "scenario")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("risk_tags")
    @classmethod
    def _normalise_risk_tags(cls, values: list[str]) -> list[str]:
        return _unique_non_empty(values, "risk tag")


class TraceEvent(_StrictModel):
    sequence: int = Field(ge=0)
    event_type: str = Field(min_length=1)
    name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["ok", "error", "timeout", "skipped"] = "ok"
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    timestamp: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)

    @field_validator("event_type")
    @classmethod
    def _normalise_event_type(cls, value: str) -> str:
        return value.strip().casefold()

    @field_validator("name")
    @classmethod
    def _normalise_optional_name(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None


class AgentOutput(_StrictModel):
    intent: str | None = None
    slots: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    content: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    fallback: bool = False
    abstained: bool | None = None
    error: str | None = None
    trace: list[TraceEvent] = Field(default_factory=list)
    memory_reads: list[str] = Field(default_factory=list)
    memory_writes: list[str] = Field(default_factory=list)

    @field_validator("intent")
    @classmethod
    def _normalise_optional_intent(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("evidence_ids", "memory_reads", "memory_writes")
    @classmethod
    def _normalise_evidence_ids(cls, values: list[str]) -> list[str]:
        return _unique_non_empty(values, "evidence id")


class MetricResult(_StrictModel):
    name: str = Field(min_length=1)
    dimension: MetricDimension
    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    applicable: bool = True
    severity: RiskSeverity | None = None
    hard_gate: bool = False
    evidence: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class EvaluationReport(_StrictModel):
    case_id: str
    passed: bool
    hard_gate_failed: bool
    overall_score: float = Field(ge=0.0, le=1.0)
    metrics: list[MetricResult]
    failed_metrics: list[str] = Field(default_factory=list)


def _unique_non_empty(values: list[str], label: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError(f"{label} must not be empty")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
