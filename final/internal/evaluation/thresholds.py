"""No-answer threshold calibration for RAG reranker scores.

The positive class is deliberately defined as ``should reject`` (an
unanswerable query).  Keeping that convention explicit prevents the common
mistake of reporting a high "recall" without saying whether it measures
answer retrieval or no-answer detection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ThresholdSample:
    """One labelled validation sample.

    ``score`` is the calibrated 0..1 reranker score for the best candidate.
    ``answerable`` is a human-owned label, not an LLM-generated judgement.
    """

    score: float
    answerable: bool
    sample_id: str = ""

    def __post_init__(self) -> None:
        score = float(self.score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("score must be a finite number in [0, 1]")
        object.__setattr__(self, "score", score)


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    true_reject: int
    false_reject: int
    unsupported_answer: int
    correct_answer: int
    precision: float
    recall: float
    f1: float
    accuracy: float
    false_refusal_rate: float
    unsupported_answer_rate: float
    weighted_cost: float


@dataclass(frozen=True)
class ThresholdCalibration:
    recommended: ThresholdMetrics
    rows: tuple[ThresholdMetrics, ...]
    sample_count: int
    answerable_count: int
    unanswerable_count: int
    false_answer_cost: float
    false_refusal_cost: float

    def to_dict(self) -> dict:
        return {
            "recommended": asdict(self.recommended),
            "sample_count": self.sample_count,
            "answerable_count": self.answerable_count,
            "unanswerable_count": self.unanswerable_count,
            "false_answer_cost": self.false_answer_cost,
            "false_refusal_cost": self.false_refusal_cost,
            "rows": [asdict(row) for row in self.rows],
        }


def evaluate_threshold(
    samples: Sequence[ThresholdSample],
    threshold: float,
    *,
    false_answer_cost: float = 5.0,
    false_refusal_cost: float = 1.0,
) -> ThresholdMetrics:
    """Evaluate one threshold with no-answer detection as the positive class."""

    _validate_costs(false_answer_cost, false_refusal_cost)
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be a finite number in [0, 1]")

    true_reject = false_reject = unsupported_answer = correct_answer = 0
    for sample in samples:
        predicted_reject = sample.score < threshold
        if predicted_reject and not sample.answerable:
            true_reject += 1
        elif predicted_reject and sample.answerable:
            false_reject += 1
        elif not predicted_reject and not sample.answerable:
            unsupported_answer += 1
        else:
            correct_answer += 1

    precision = _safe_div(true_reject, true_reject + false_reject)
    recall = _safe_div(true_reject, true_reject + unsupported_answer)
    f1 = _safe_div(2.0 * precision * recall, precision + recall)
    total = len(samples)
    answerable = false_reject + correct_answer
    unanswerable = true_reject + unsupported_answer
    return ThresholdMetrics(
        threshold=threshold,
        true_reject=true_reject,
        false_reject=false_reject,
        unsupported_answer=unsupported_answer,
        correct_answer=correct_answer,
        precision=precision,
        recall=recall,
        f1=f1,
        accuracy=_safe_div(true_reject + correct_answer, total),
        false_refusal_rate=_safe_div(false_reject, answerable),
        unsupported_answer_rate=_safe_div(unsupported_answer, unanswerable),
        weighted_cost=(
            unsupported_answer * float(false_answer_cost)
            + false_reject * float(false_refusal_cost)
        ),
    )


def calibrate_no_answer_threshold(
    samples: Iterable[ThresholdSample],
    *,
    thresholds: Iterable[float] | None = None,
    false_answer_cost: float = 5.0,
    false_refusal_cost: float = 1.0,
) -> ThresholdCalibration:
    """Traverse thresholds and select the lowest business-cost operating point.

    Tie-breaking prefers higher F1, then higher no-answer recall, then the
    lower threshold to avoid unnecessary refusals when all other evidence is
    equal.
    """

    sample_list = tuple(samples)
    if not sample_list:
        raise ValueError("at least one labelled sample is required")
    if not any(sample.answerable for sample in sample_list):
        raise ValueError("validation set must include answerable samples")
    if not any(not sample.answerable for sample in sample_list):
        raise ValueError("validation set must include unanswerable samples")
    _validate_costs(false_answer_cost, false_refusal_cost)

    threshold_values = _normalise_thresholds(thresholds)
    rows = tuple(
        evaluate_threshold(
            sample_list,
            threshold,
            false_answer_cost=false_answer_cost,
            false_refusal_cost=false_refusal_cost,
        )
        for threshold in threshold_values
    )
    recommended = min(
        rows,
        key=lambda row: (
            row.weighted_cost,
            -row.f1,
            -row.recall,
            row.threshold,
        ),
    )
    return ThresholdCalibration(
        recommended=recommended,
        rows=rows,
        sample_count=len(sample_list),
        answerable_count=sum(1 for sample in sample_list if sample.answerable),
        unanswerable_count=sum(1 for sample in sample_list if not sample.answerable),
        false_answer_cost=float(false_answer_cost),
        false_refusal_cost=float(false_refusal_cost),
    )


def render_threshold_report(calibration: ThresholdCalibration) -> str:
    """Render a compact, interview-auditable Markdown calibration report."""

    best = calibration.recommended
    nearest = sorted(
        calibration.rows,
        key=lambda row: (abs(row.threshold - best.threshold), row.threshold),
    )[:7]
    nearest.sort(key=lambda row: row.threshold)
    lines = [
        "# RAG 无答案阈值校准报告",
        "",
        "## 口径",
        "",
        "- 正类：应该拒答的无答案问题。",
        "- 预测拒答：最高 Rerank 分数低于阈值。",
        "- Unsupported Answer：无答案问题被系统继续回答，通常比错误拒答风险更高。",
        "",
        "## 数据与业务成本",
        "",
        f"- 样本数：{calibration.sample_count}",
        f"- 可回答：{calibration.answerable_count}",
        f"- 不可回答：{calibration.unanswerable_count}",
        f"- 错误回答成本：{calibration.false_answer_cost:g}",
        f"- 错误拒答成本：{calibration.false_refusal_cost:g}",
        "",
        "## 推荐结果",
        "",
        f"- 推荐阈值：**{best.threshold:.2f}**",
        f"- Precision / Recall / F1：{best.precision:.3f} / {best.recall:.3f} / {best.f1:.3f}",
        f"- 错误拒答：{best.false_reject}",
        f"- 无依据回答：{best.unsupported_answer}",
        f"- 加权业务成本：{best.weighted_cost:g}",
        "",
        "## 推荐阈值附近的结果",
        "",
        "| 阈值 | Precision | Recall | F1 | 错误拒答 | 无依据回答 | 加权成本 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in nearest:
        lines.append(
            f"| {row.threshold:.2f} | {row.precision:.3f} | {row.recall:.3f} | "
            f"{row.f1:.3f} | {row.false_reject} | {row.unsupported_answer} | "
            f"{row.weighted_cost:g} |"
        )
    lines.extend([
        "",
        "> 推荐阈值只对当前数据、Rerank 模型、Prompt 和知识库版本有效；任一版本变化后应重新校准。",
        "",
    ])
    return "\n".join(lines)


def _normalise_thresholds(thresholds: Iterable[float] | None) -> tuple[float, ...]:
    raw_values = thresholds if thresholds is not None else (i / 100.0 for i in range(101))
    values: set[float] = set()
    for raw in raw_values:
        threshold = float(raw)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("all thresholds must be finite numbers in [0, 1]")
        values.add(threshold)
    if not values:
        raise ValueError("at least one threshold is required")
    return tuple(sorted(values))


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _validate_costs(false_answer_cost: float, false_refusal_cost: float) -> None:
    for name, value in (
        ("false_answer_cost", false_answer_cost),
        ("false_refusal_cost", false_refusal_cost),
    ):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{name} must be a finite non-negative number")
