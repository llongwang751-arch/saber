from __future__ import annotations

import pytest

from internal.evaluation.strategy import mcnemar_exact, paired_bootstrap_ci


def test_mcnemar_exact_detects_a_strong_paired_candidate_improvement():
    baseline = [False] * 20 + [True] * 20
    candidate = [True] * 40

    result = mcnemar_exact(baseline, candidate)

    assert result["method"] == "mcnemar_exact"
    assert result["status"] == "ok"
    assert result["reason"] == ""
    assert result["n_pairs"] == 40
    assert result["fixed"] == 20
    assert result["regressions"] == 0
    assert result["discordant_pairs"] == 20
    assert result["p_value"] == pytest.approx(2 / (2**20))
    assert result["alpha"] == pytest.approx(0.05)
    assert result["significant"] is True
    assert result["direction"] == "candidate_better"
    assert result["passed"] is True


def test_mcnemar_exact_does_not_call_symmetric_changes_an_improvement():
    baseline = [False] * 10 + [True] * 10 + [True] * 20
    candidate = [True] * 10 + [False] * 10 + [True] * 20

    result = mcnemar_exact(baseline, candidate)

    assert result["status"] == "ok"
    assert result["fixed"] == 10
    assert result["regressions"] == 10
    assert result["p_value"] == pytest.approx(1.0)
    assert result["significant"] is False
    assert result["direction"] == "no_difference"
    assert result["passed"] is False


@pytest.mark.parametrize(
    ("baseline", "candidate", "reason_fragment"),
    [
        ([False] * 19, [True] * 19, "pair"),
        ([True] * 20, [True] * 20, "discord"),
    ],
)
def test_mcnemar_exact_marks_small_or_non_informative_samples_inconclusive(
    baseline,
    candidate,
    reason_fragment,
):
    result = mcnemar_exact(baseline, candidate)

    assert result["status"] == "inconclusive"
    assert reason_fragment in result["reason"].casefold()
    assert result["significant"] is False
    assert result["passed"] is False


def test_mcnemar_exact_rejects_unpaired_inputs():
    with pytest.raises(ValueError, match="(?i)length|pair|长度|配对"):
        mcnemar_exact([True, False], [True])


def test_paired_bootstrap_is_seeded_reproducible_and_reports_a_ci():
    baseline = [0.10 + index / 1000 for index in range(40)]
    candidate = [value + 0.08 for value in baseline]

    first = paired_bootstrap_ci(
        baseline,
        candidate,
        iterations=2000,
        seed=20260914,
    )
    second = paired_bootstrap_ci(
        baseline,
        candidate,
        iterations=2000,
        seed=20260914,
    )

    assert first == second
    assert first["method"] == "paired_bootstrap_95ci"
    assert first["status"] == "ok"
    assert first["reason"] == ""
    assert first["n_pairs"] == 40
    assert first["mean_delta"] == pytest.approx(0.08)
    assert first["ci_low"] == pytest.approx(0.08)
    assert first["ci_high"] == pytest.approx(0.08)
    assert first["confidence"] == pytest.approx(0.95)
    assert first["iterations"] == 2000
    assert first["seed"] == 20260914
    assert first["direction"] == "candidate_better"
    assert first["passed"] is True


def test_paired_bootstrap_marks_too_few_pairs_inconclusive():
    result = paired_bootstrap_ci(
        [0.1] * 19,
        [0.2] * 19,
        iterations=500,
        seed=7,
    )

    assert result["status"] == "inconclusive"
    assert "pair" in result["reason"].casefold()
    assert result["n_pairs"] == 19
    assert result["passed"] is False


def test_paired_bootstrap_rejects_invalid_inputs_and_parameters():
    with pytest.raises(ValueError, match="(?i)length|pair|长度|配对"):
        paired_bootstrap_ci([0.1, 0.2], [0.3])
    with pytest.raises(ValueError):
        paired_bootstrap_ci([0.1] * 20, [0.2] * 20, iterations=0)
    with pytest.raises(ValueError):
        paired_bootstrap_ci([0.1] * 20, [0.2] * 20, confidence=1.0)
