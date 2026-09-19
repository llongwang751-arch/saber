from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

from internal.experimentation import assignment as assignment_module
from internal.experimentation.assignment import (
    canonical_bucket_input,
    select_arm,
    stable_bucket,
    stable_buckets,
)
from internal.experimentation.runtime import (
    RAGRuntimeOverrides,
    compile_runtime_overrides,
)


def test_hmac_bucket_matches_cross_language_golden_vector():
    # HMAC-SHA256("golden-test-secret", b"agi-exp-v1\0tenant-alpha\0"
    #             b"exp-001\0user\0user-42")[:8] % 10_000.
    assert stable_bucket(
        b"golden-test-secret",
        "tenant-alpha",
        "exp-001",
        "user-42",
    ) == 7961

    assert canonical_bucket_input("tenant-a", "exp-1", "user-42") == (
        b"agi-exp-v1\0tenant-a\0exp-1\0user\0user-42"
    )
    assert stable_buckets("secret", "tenant-a", "exp-1", "user-42") == {
        "enrollment_bucket": 5860,
        "variant_bucket": 6630,
    }


def test_hmac_assignment_is_restart_stable_and_domain_separated():
    arguments = (b"restart-stable-secret", "tenant-a", "experiment-a", "user-a")
    before = stable_bucket(*arguments)
    reloaded = importlib.reload(assignment_module)

    assert reloaded.stable_bucket(*arguments) == before
    assert len(
        {
            before,
            stable_bucket(
                b"restart-stable-secret", "tenant-b", "experiment-a", "user-a"
            ),
            stable_bucket(
                b"restart-stable-secret", "tenant-a", "experiment-b", "user-a"
            ),
        }
    ) == 3


@pytest.mark.parametrize(
    ("bucket", "candidate_allocation_bps", "expected"),
    [
        (0, 0, "control"),
        (9999, 0, "control"),
        (0, 1, "candidate"),
        (1, 1, "control"),
        (4999, 5000, "candidate"),
        (5000, 5000, "control"),
        (0, 10000, "candidate"),
        (9999, 10000, "candidate"),
    ],
)
def test_candidate_weight_uses_exact_basis_point_boundaries(
    bucket,
    candidate_allocation_bps,
    expected,
):
    assert select_arm(bucket, candidate_allocation_bps) == expected


@pytest.mark.parametrize(
    ("bucket", "candidate_allocation_bps"),
    [
        (-1, 5000),
        (10000, 5000),
        (0, -1),
        (0, 10001),
        (True, 5000),
        (0, True),
    ],
)
def test_candidate_weight_and_bucket_reject_invalid_values(
    bucket,
    candidate_allocation_bps,
):
    with pytest.raises((TypeError, ValueError)):
        select_arm(bucket, candidate_allocation_bps)


@pytest.mark.parametrize(
    "payload",
    [
        {"top_k": 1, "no_answer_threshold": 0.0},
        {"top_k": 20, "no_answer_threshold": 1.0},
        {},
    ],
)
def test_rag_runtime_override_accepts_only_declared_boundaries(payload):
    parsed = RAGRuntimeOverrides.model_validate(payload)
    assert parsed.model_dump(exclude_none=True) == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"top_k": 0},
        {"top_k": 21},
        {"no_answer_threshold": -0.001},
        {"no_answer_threshold": 1.001},
        {"temperature": 0.2},
        {"api_key": "literal-secret"},
    ],
)
def test_rag_runtime_override_rejects_out_of_range_extra_and_sensitive_fields(payload):
    with pytest.raises((TypeError, ValueError, ValidationError)):
        RAGRuntimeOverrides.model_validate(payload)


def test_runtime_compiler_returns_only_whitelisted_candidate_knobs():
    assert compile_runtime_overrides(
        {
            "runtime_overrides": {
                "rag": {"top_k": 7, "no_answer_threshold": 0.42}
            }
        }
    ) == {"rag": {"top_k": 7, "no_answer_threshold": 0.42}}


@pytest.mark.parametrize(
    "manifest",
    [
        {"runtime_overrides": {"rag": {"unknown_knob": True}}},
        {"runtime_overrides": {"control": {"rag": {"top_k": 5}}}},
        {"runtime_overrides": {"rag": {"authorization": "Bearer fake-value"}}},
        {"runtime_overrides": {"rag": {"notes": "Bearer abcdefghijklmnop"}}},
    ],
)
def test_runtime_compiler_rejects_unknown_control_and_secret_material(manifest):
    with pytest.raises((TypeError, ValueError, ValidationError)):
        compile_runtime_overrides(manifest)
