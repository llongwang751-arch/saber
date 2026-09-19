from __future__ import annotations

import pytest

from internal.experimentation.statistics import (
    analyze_binary_outcome,
    required_sample_size_binary,
    srm_exact,
)


def test_required_sample_size_is_a_deterministic_preregistered_value():
    assert required_sample_size_binary(
        0.20,
        0.05,
        alpha=0.05,
        power=0.80,
    ) == 1094
    assert required_sample_size_binary(
        0.20,
        0.10,
        alpha=0.05,
        power=0.80,
    ) == 294


@pytest.mark.parametrize(
    "kwargs",
    [
        {"baseline_rate": 0.2, "minimum_detectable_effect": 0.0},
        {"baseline_rate": 0.2, "minimum_detectable_effect": -0.1},
        {"baseline_rate": 0.2, "minimum_detectable_effect": 0.8},
        {"baseline_rate": 0.0, "minimum_detectable_effect": 0.1},
        {"baseline_rate": 1.0, "minimum_detectable_effect": 0.1},
        {"baseline_rate": 0.2, "minimum_detectable_effect": 0.1, "alpha": 0.0},
        {"baseline_rate": 0.2, "minimum_detectable_effect": 0.1, "power": 0.5},
    ],
)
def test_preregistered_power_inputs_reject_invalid_boundaries(kwargs):
    with pytest.raises((TypeError, ValueError)):
        required_sample_size_binary(**kwargs)


def test_srm_exact_accepts_expected_ratios_and_flags_large_mismatch():
    balanced = srm_exact(500, 500, candidate_allocation_bps=5000)
    weighted = srm_exact(900, 100, candidate_allocation_bps=1000)
    mismatch = srm_exact(900, 100, candidate_allocation_bps=5000)

    assert balanced["status"] == "pass"
    assert balanced["sufficient_sample"] is True
    assert balanced["p_value"] == pytest.approx(1.0)
    assert balanced["passes"] is True
    assert weighted["status"] == "pass"
    assert weighted["passes"] is True
    assert mismatch["status"] == "mismatch"
    assert mismatch["p_value"] < 0.01
    assert mismatch["is_mismatch"] is True
    assert mismatch["passes"] is False


def test_srm_small_sample_is_explicitly_insufficient_not_green():
    result = srm_exact(5, 5, candidate_allocation_bps=5000)

    assert result["status"] == "insufficient"
    assert result["sufficient_sample"] is False
    assert result["p_value"] is None
    assert result["passes"] is False
    assert result["is_mismatch"] is False


@pytest.mark.parametrize(
    "overrides, blocker",
    [
        ({"provenance": "internal"}, "traffic_provenance_internal"),
        (
            {
                "control_successes": 0,
                "control_total": 0,
                "candidate_successes": 0,
                "candidate_total": 0,
            },
            "no_real_traffic",
        ),
        ({"required_sample_per_arm": 101}, "insufficient_sample_size"),
        ({"experiment_status": "running"}, "experiment_not_completed"),
        ({"minimum_duration_reached": False}, "minimum_duration_not_reached"),
    ],
)
def test_ineligible_analysis_never_emits_inferential_numbers_or_winner(
    overrides,
    blocker,
):
    arguments = {
        "control_successes": 40,
        "control_total": 100,
        "candidate_successes": 70,
        "candidate_total": 100,
        "provenance": "production_authenticated",
        "experiment_status": "completed",
        "required_sample_per_arm": 20,
        "minimum_duration_reached": True,
        "srm": srm_exact(100, 100, candidate_allocation_bps=5000),
    }
    arguments.update(overrides)

    result = analyze_binary_outcome(**arguments)

    assert result["truth"] == "no_claim"
    assert result["can_claim_effect"] is False
    assert blocker in result["claim_blockers"]
    assert result["absolute_effect"] is None
    assert result["confidence_interval"] is None
    assert result["p_value"] is None
    assert result["winner"] is None


def test_only_completed_sufficient_production_experiment_can_claim_effect():
    result = analyze_binary_outcome(
        40,
        100,
        70,
        100,
        provenance="production_authenticated",
        experiment_status="completed",
        required_sample_per_arm=20,
        minimum_duration_reached=True,
        srm=srm_exact(100, 100, candidate_allocation_bps=5000),
    )

    assert result["truth"] == "observed"
    assert result["can_claim_effect"] is True
    assert result["claim_blockers"] == []
    assert result["winner"] == "candidate"
    assert result["absolute_effect"] == pytest.approx(0.30)
    assert result["confidence_interval"][0] > 0.0
    assert result["p_value"] < 0.05
