import pytest

from internal.evaluation.thresholds import (
    ThresholdSample,
    calibrate_no_answer_threshold,
    evaluate_threshold,
    render_threshold_report,
)


def test_threshold_metrics_use_no_answer_as_positive_class():
    samples = [
        ThresholdSample(0.9, True),
        ThresholdSample(0.4, True),
        ThresholdSample(0.2, False),
        ThresholdSample(0.7, False),
    ]

    metrics = evaluate_threshold(samples, 0.5)

    assert metrics.true_reject == 1
    assert metrics.false_reject == 1
    assert metrics.unsupported_answer == 1
    assert metrics.correct_answer == 1
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == pytest.approx(0.5)
    assert metrics.f1 == pytest.approx(0.5)
    assert metrics.weighted_cost == 6.0


def test_calibration_selects_perfect_operating_point_and_renders_report():
    samples = [
        ThresholdSample(0.91, True, "a1"),
        ThresholdSample(0.72, True, "a2"),
        ThresholdSample(0.48, True, "a3"),
        ThresholdSample(0.06, False, "u1"),
        ThresholdSample(0.18, False, "u2"),
        ThresholdSample(0.27, False, "u3"),
    ]

    calibration = calibrate_no_answer_threshold(samples)

    assert calibration.recommended.threshold == pytest.approx(0.28)
    assert calibration.recommended.f1 == pytest.approx(1.0)
    assert calibration.recommended.weighted_cost == 0
    report = render_threshold_report(calibration)
    assert "推荐阈值：**0.28**" in report
    assert "应该拒答的无答案问题" in report


def test_higher_false_answer_cost_prefers_safety_over_false_refusal():
    samples = [
        ThresholdSample(0.55, True),
        ThresholdSample(0.60, False),
    ]

    safety_first = calibrate_no_answer_threshold(
        samples,
        thresholds=[0.0, 0.56, 0.61],
        false_answer_cost=10,
        false_refusal_cost=1,
    )
    availability_first = calibrate_no_answer_threshold(
        samples,
        thresholds=[0.0, 0.56, 0.61],
        false_answer_cost=1,
        false_refusal_cost=10,
    )

    assert safety_first.recommended.threshold == pytest.approx(0.61)
    assert safety_first.recommended.unsupported_answer == 0
    assert availability_first.recommended.threshold == pytest.approx(0.0)
    assert availability_first.recommended.false_reject == 0


def test_calibration_rejects_invalid_or_one_class_data():
    with pytest.raises(ValueError, match="finite number"):
        ThresholdSample(float("nan"), True)
    with pytest.raises(ValueError, match="unanswerable"):
        calibrate_no_answer_threshold([ThresholdSample(0.8, True)])
