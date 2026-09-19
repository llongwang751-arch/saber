"""Safe v1 contracts, assignment, and statistics for online experiments."""

from .assignment import (
    allocate,
    assign_variant,
    canonical_bucket_input,
    select_arm,
    stable_bucket,
    stable_buckets,
)
from .schemas import (
    Allocation,
    DeploymentCreate,
    ExperimentCreate,
    ExperimentDecision,
    ExperimentMutation,
    ExperimentResume,
    ExperimentStart,
    ExposureContext,
    FeedbackCreate,
    FeedbackEvent,
    RAGRuntimeOverrides,
    RampRequest,
    TrafficProvenance,
)
from .statistics import (
    analyze_binary_outcome,
    minimum_detectable_effect_binary,
    required_sample_size_binary,
    srm_exact,
)
from .runtime import RUNTIME_COMPILER_VERSION, compile_runtime_overrides
from .service import ExperimentService
from .store import (
    ExperimentConflictError,
    ExperimentStore,
    GenerationConflictError,
    IdempotencyConflictError,
)

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
    "ExperimentConflictError",
    "ExperimentService",
    "ExperimentStore",
    "GenerationConflictError",
    "IdempotencyConflictError",
    "RUNTIME_COMPILER_VERSION",
    "allocate",
    "analyze_binary_outcome",
    "assign_variant",
    "canonical_bucket_input",
    "compile_runtime_overrides",
    "minimum_detectable_effect_binary",
    "required_sample_size_binary",
    "select_arm",
    "srm_exact",
    "stable_bucket",
    "stable_buckets",
]
