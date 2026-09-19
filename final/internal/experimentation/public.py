"""Public-view redaction for traces produced during online experiments.

The server keeps assignment and runtime evidence for audit, but an ordinary
chat participant must not receive enough metadata to infer their arm.  This
module is deliberately independent from FastAPI so sync responses, SSE and
persisted trace reads share one policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_MARKERS = frozenset({
    "experiment_exposure_id",
    "runtime_strategy_checksum",
    "runtime_overrides_applied",
    "request_fingerprint",
    "audience_attestation",
})

_EXPERIMENT_ONLY_FIELDS = frozenset({
    "experiment_exposure_id",
    "runtime_strategy_checksum",
    "runtime_overrides",
    "runtime_overrides_applied",
    "compiled_checksum",
    "source_manifest_checksum",
    "subject_digest",
    "assignment_id",
    "deployment_id",
    "experiment_generation",
    "request_fingerprint",
    "audience_policy_version",
    "audience_provenance",
    "audience_eligible",
    "audience_account_created_at",
    "audience_attestation",
    "audience_exclusion_reasons",
    "enrollment_bucket",
    "variant_bucket",
    "allocation_bps",
    "candidate_allocation_bps",
    "arm",
    # These two values are themselves the v1 treatment.  Showing either one
    # would reveal the arm even if the arm label were removed.
    "top_k",
    "no_answer_threshold",
})


def has_online_experiment_context(value: Any) -> bool:
    """Return whether a nested trace contains a non-empty experiment marker."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in _MARKERS and item not in (None, "", {}, []):
                return True
            if has_online_experiment_context(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(has_online_experiment_context(item) for item in value)
    return False


def redact_public_experiment_trace(
    value: Any,
    *,
    experiment_active: bool | None = None,
) -> Any:
    """Copy a trace while removing online-assignment side channels.

    Non-experiment traces retain normal RAG teaching fields.  For experiment
    traces the request-level treatment parameters are removed recursively.
    The caller may pass ``experiment_active`` when it already owns the opaque
    exposure handle; otherwise markers are detected before redaction.
    """

    active = (
        has_online_experiment_context(value)
        if experiment_active is None
        else bool(experiment_active)
    )
    return _copy_redacted(value, active)


def _copy_redacted(value: Any, active: bool) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if active and text_key in _EXPERIMENT_ONLY_FIELDS:
                continue
            result[text_key] = _copy_redacted(item, active)
        return result
    if isinstance(value, list):
        return [_copy_redacted(item, active) for item in value]
    if isinstance(value, tuple):
        return [_copy_redacted(item, active) for item in value]
    return value


__all__ = [
    "has_online_experiment_context",
    "redact_public_experiment_trace",
]
