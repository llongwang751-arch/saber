"""Strict public contracts for the v1 online experimentation surface.

The first version is intentionally small.  It can vary only the two RAG
runtime knobs that are safe to apply per request; it cannot mutate shared
configuration, credentials, prompts, models, tools, or write-side behaviour.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class _StrictModel(BaseModel):
    """Base for request and value objects: unknown input is always an error."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
    )


class TrafficProvenance(str, Enum):
    """Where an exposure came from, and therefore what may be claimed from it."""

    DISABLED = "disabled"
    INTERNAL = "internal"
    PRODUCTION_AUTHENTICATED = "production_authenticated"


class RAGRuntimeOverrides(_StrictModel):
    """The complete v1 request-scoped override allowlist.

    An empty object is valid.  In particular, a control arm may intentionally
    describe the existing production behaviour without spelling out either
    setting.  Cross-arm and deployment checks belong to the service layer.
    """

    top_k: StrictInt | None = Field(default=None, ge=1, le=20)
    no_answer_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("no_answer_threshold", mode="before")
    @classmethod
    def _validate_threshold_number(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("no_answer_threshold must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError("no_answer_threshold must be finite")
        return float(value)

    def runtime_payload(self) -> dict[str, dict[str, int | float]]:
        """Return the shape consumed by ``ChatOptions.runtime_overrides``."""

        values = self.model_dump(exclude_none=True)
        return {"rag": values} if values else {}


class DeploymentCreate(_StrictModel):
    """Request to materialise an approved offline proposal as a deployment."""

    proposal_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("proposal_id", "idempotency_key")
    @classmethod
    def _strip_required_identifiers(cls, value: str) -> str:
        return _required_text(value)


class ExperimentCreate(_StrictModel):
    """Pre-registered two-arm binary experiment.

    Candidate overrides are deliberately absent: the candidate is an immutable
    deployment.  The service resolves and validates that deployment before an
    experiment can start.  The control may be ``{}``, meaning current runtime
    defaults.
    """

    name: str = Field(min_length=1, max_length=200)
    hypothesis: str = Field(default="", max_length=2_000)
    candidate_deployment_id: str = Field(min_length=1, max_length=200)
    surface: Literal["rag_chat"] = "rag_chat"
    primary_metric: Literal["positive_feedback"] = "positive_feedback"
    baseline_rate: float = Field(gt=0.0, lt=1.0)
    minimum_detectable_effect: float = Field(gt=0.0, lt=1.0)
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    power: float = Field(default=0.8, gt=0.5, lt=1.0)
    enrollment_bps: StrictInt = Field(default=1_000, ge=1, le=10_000)
    candidate_allocation_bps: StrictInt = Field(default=5_000, ge=1, le=9_999)
    control_overrides: RAGRuntimeOverrides = Field(default_factory=RAGRuntimeOverrides)
    min_duration_hours: StrictInt = Field(default=168, ge=1)
    max_duration_hours: StrictInt = Field(default=672, ge=1)
    attribution_window_hours: StrictInt = Field(default=168, ge=1, le=720)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator(
        "baseline_rate",
        "minimum_detectable_effect",
        "alpha",
        "power",
        mode="before",
    )
    @classmethod
    def _reject_boolean_numbers(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("statistical parameters must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError("statistical parameters must be finite")
        return float(value)

    @field_validator("name", "candidate_deployment_id", "idempotency_key")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("hypothesis")
    @classmethod
    def _strip_hypothesis(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_preregistration(self) -> "ExperimentCreate":
        if self.baseline_rate + self.minimum_detectable_effect >= 1.0:
            raise ValueError(
                "baseline_rate + minimum_detectable_effect must be below 1"
            )
        if self.max_duration_hours < self.min_duration_hours:
            raise ValueError("max_duration_hours must be >= min_duration_hours")
        return self


class ExperimentMutation(_StrictModel):
    """Optimistically locked, idempotent mutation request."""

    expected_generation: StrictInt = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2_000)

    @field_validator("idempotency_key")
    @classmethod
    def _strip_idempotency_key(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        return value.strip()


class ExperimentDecision(_StrictModel):
    """Human approval/rejection of a completed experiment recommendation."""

    decision: Literal["approve", "reject"]
    expected_generation: StrictInt = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2_000)

    @field_validator("idempotency_key")
    @classmethod
    def _strip_decision_idempotency_key(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("reason")
    @classmethod
    def _strip_decision_reason(cls, value: str) -> str:
        return value.strip()


class ExperimentStart(ExperimentMutation):
    """Start an approved experiment at the explicit first traffic stage."""

    target_status: Literal["canary", "running"] = "canary"


class ExperimentResume(ExperimentMutation):
    """Resume a manual or independently approved safety pause."""

    target_status: Literal["canary", "running"] | None = None


class RampRequest(_StrictModel):
    """Optimistically locked request to change experiment enrollment."""

    target_enrollment_bps: StrictInt = Field(ge=1, le=10_000)
    expected_generation: StrictInt = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2_000)

    @field_validator("idempotency_key")
    @classmethod
    def _strip_ramp_idempotency_key(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("reason")
    @classmethod
    def _strip_ramp_reason(cls, value: str) -> str:
        return value.strip()


class FeedbackCreate(_StrictModel):
    """Idempotent binary feedback attached to one exposure."""

    exposure_id: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    rating: Literal[-1, 1]

    @field_validator("rating", mode="before")
    @classmethod
    def _require_integer_rating(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rating must be the integer -1 or 1")
        return value

    @field_validator("exposure_id", "event_id")
    @classmethod
    def _strip_feedback_identifiers(cls, value: str) -> str:
        return _required_text(value)


class FeedbackEvent(_StrictModel):
    """Path-addressed feedback request; the arm is never client supplied."""

    event_id: str = Field(min_length=1, max_length=200)
    rating: Literal[-1, 1]

    @field_validator("rating", mode="before")
    @classmethod
    def _require_integer_rating(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rating must be the integer -1 or 1")
        return value

    @field_validator("event_id")
    @classmethod
    def _strip_event_id(cls, value: str) -> str:
        return _required_text(value)


class Allocation(_StrictModel):
    """Deterministic result of assigning one user to one of two arms."""

    bucket: StrictInt = Field(ge=0, le=9_999)
    arm: Literal["control", "candidate"]
    candidate_allocation_bps: StrictInt = Field(ge=0, le=10_000)


class ExposureContext(_StrictModel):
    """Request-local experiment context; safe to pass across execution layers."""

    tenant_id: str = Field(min_length=1, max_length=500)
    experiment_id: str = Field(min_length=1, max_length=500)
    user_id: str = Field(min_length=1, max_length=500)
    exposure_id: str = Field(default="", max_length=200)
    allocation: Allocation
    provenance: TrafficProvenance
    surface: Literal["rag_chat"] = "rag_chat"
    runtime_overrides: RAGRuntimeOverrides = Field(default_factory=RAGRuntimeOverrides)
    runtime_strategy_checksum: str = Field(default="", max_length=128)

    @field_validator("tenant_id", "experiment_id", "user_id")
    @classmethod
    def _strip_context_identifiers(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("exposure_id", "runtime_strategy_checksum")
    @classmethod
    def _strip_optional_context_text(cls, value: str) -> str:
        return value.strip()


def _required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("value must not be blank")
    return value


__all__ = [
    "Allocation",
    "DeploymentCreate",
    "ExperimentCreate",
    "ExperimentDecision",
    "ExperimentMutation",
    "ExperimentResume",
    "ExperimentStart",
    "ExposureContext",
    "FeedbackCreate",
    "FeedbackEvent",
    "RAGRuntimeOverrides",
    "RampRequest",
    "TrafficProvenance",
]
