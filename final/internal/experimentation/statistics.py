"""Dependency-free statistics for a pre-registered two-arm binary test.

The module separates descriptive calculations from permission to make an
effect claim.  Synthetic/internal traffic is useful for exercising the
pipeline, but it can never produce a winner.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from statistics import NormalDist
from typing import Any, Literal

from .schemas import TrafficProvenance


_NORMAL = NormalDist()


def required_sample_size_binary(
    baseline_rate: float,
    minimum_detectable_effect: float | None = None,
    *,
    mde: float | None = None,
    alpha: float = 0.05,
    power: float = 0.8,
    alternative: Literal["two-sided", "one-sided"] = "two-sided",
) -> int:
    """Return the pre-registered required observations *per arm*.

    This is the standard normal-approximation formula for two independent
    proportions with equal arm sizes.  ``minimum_detectable_effect`` is an
    absolute uplift (for example, ``0.02`` means two percentage points), not a
    relative percentage.  ``mde`` is accepted as a concise keyword alias.
    """

    baseline = _probability(baseline_rate, "baseline_rate", endpoints=False)
    effect = _resolve_effect(minimum_detectable_effect, mde)
    candidate_rate = baseline + effect
    if candidate_rate >= 1.0:
        raise ValueError(
            "baseline_rate + minimum_detectable_effect must be below 1"
        )
    alpha_value = _open_probability(alpha, "alpha")
    power_value = _open_probability(power, "power")
    if power_value <= 0.5:
        raise ValueError("power must be greater than 0.5")
    if alternative not in {"two-sided", "one-sided"}:
        raise ValueError("alternative must be 'two-sided' or 'one-sided'")

    alpha_tail = alpha_value / 2.0 if alternative == "two-sided" else alpha_value
    z_alpha = _NORMAL.inv_cdf(1.0 - alpha_tail)
    z_power = _NORMAL.inv_cdf(power_value)
    pooled = (baseline + candidate_rate) / 2.0
    numerator = (
        z_alpha * math.sqrt(2.0 * pooled * (1.0 - pooled))
        + z_power
        * math.sqrt(
            baseline * (1.0 - baseline)
            + candidate_rate * (1.0 - candidate_rate)
        )
    ) ** 2
    return max(1, math.ceil(numerator / (effect * effect)))


def minimum_detectable_effect_binary(
    sample_size_per_arm: int,
    baseline_rate: float,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
    alternative: Literal["two-sided", "one-sided"] = "two-sided",
    tolerance: float = 1e-8,
) -> float:
    """Invert :func:`required_sample_size_binary` for an absolute binary MDE."""

    sample_size = _non_negative_int(
        sample_size_per_arm, "sample_size_per_arm", positive=True
    )
    baseline = _probability(baseline_rate, "baseline_rate", endpoints=False)
    _open_probability(alpha, "alpha")
    power_value = _open_probability(power, "power")
    if power_value <= 0.5:
        raise ValueError("power must be greater than 0.5")
    if alternative not in {"two-sided", "one-sided"}:
        raise ValueError("alternative must be 'two-sided' or 'one-sided'")
    tolerance_value = _finite_number(tolerance, "tolerance")
    if tolerance_value <= 0.0:
        raise ValueError("tolerance must be positive")

    # Stay just inside the open probability boundary used by the planning
    # formula.  If even that very large effect is underpowered, no admissible
    # uplift can meet the requested plan.
    lower = 0.0
    upper = math.nextafter(1.0, 0.0) - baseline
    if required_sample_size_binary(
        baseline,
        upper,
        alpha=alpha,
        power=power,
        alternative=alternative,
    ) > sample_size:
        raise ValueError("sample_size_per_arm is too small for this baseline")

    for _ in range(100):
        midpoint = (lower + upper) / 2.0
        if midpoint <= 0.0:
            midpoint = math.nextafter(0.0, 1.0)
        required = required_sample_size_binary(
            baseline,
            midpoint,
            alpha=alpha,
            power=power,
            alternative=alternative,
        )
        if required <= sample_size:
            upper = midpoint
        else:
            lower = midpoint
        if upper - lower <= tolerance_value:
            break
    return upper


def srm_exact(
    control_count: int,
    candidate_count: int,
    *,
    expected_candidate_share: float | None = None,
    candidate_allocation_bps: int | None = None,
    alpha: float = 0.01,
    minimum_total: int = 20,
) -> dict[str, Any]:
    """Run an exact two-sided binomial sample-ratio-mismatch check.

    The exact p-value follows the probability-ordering definition used by a
    two-sided binomial test: it sums every outcome whose null probability is no
    greater than that of the observed outcome.
    """

    control = _non_negative_int(control_count, "control_count")
    candidate = _non_negative_int(candidate_count, "candidate_count")
    share = _expected_share(expected_candidate_share, candidate_allocation_bps)
    alpha_value = _open_probability(alpha, "alpha")
    minimum = _non_negative_int(minimum_total, "minimum_total")
    total = control + candidate
    sufficient_sample = total >= minimum
    p_value = (
        _binomial_two_sided(candidate, total, share)
        if sufficient_sample and total
        else None
    )
    mismatch = bool(p_value is not None and p_value < alpha_value)
    status = (
        "insufficient"
        if not sufficient_sample
        else ("mismatch" if mismatch else "pass")
    )
    return {
        "method": "exact_binomial_two_sided",
        "control_count": control,
        "candidate_count": candidate,
        "total": total,
        "expected_candidate_share": share,
        "expected_control_count": total * (1.0 - share),
        "expected_candidate_count": total * share,
        "p_value": p_value,
        "alpha": alpha_value,
        "minimum_total": minimum,
        "sufficient_sample": sufficient_sample,
        "status": status,
        "mismatch": mismatch,
        "is_mismatch": mismatch,
        "passes": sufficient_sample and not mismatch,
    }


def analyze_binary_outcome(
    control_successes: int,
    control_total: int,
    candidate_successes: int,
    candidate_total: int,
    *,
    provenance: TrafficProvenance | str,
    experiment_status: str,
    required_sample_per_arm: int,
    minimum_duration_reached: bool,
    srm: Mapping[str, Any] | bool | None = None,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Describe and, only when justified, infer a two-arm binary outcome.

    Operational claim gates are deliberately fail-closed.  Non-production
    provenance, zero real traffic, an incomplete run, unmet duration/sample
    requirements, or SRM all force ``truth='no_claim'`` and ``winner=None``.
    Descriptive rates remain available so internal end-to-end tests are useful
    without being presented as production evidence.
    """

    control_n = _non_negative_int(control_total, "control_total")
    candidate_n = _non_negative_int(candidate_total, "candidate_total")
    control_y = _success_count(
        control_successes, control_n, "control_successes"
    )
    candidate_y = _success_count(
        candidate_successes, candidate_n, "candidate_successes"
    )
    required_n = _non_negative_int(
        required_sample_per_arm, "required_sample_per_arm", positive=True
    )
    if not isinstance(minimum_duration_reached, bool):
        raise TypeError("minimum_duration_reached must be a boolean")
    alpha_value = _open_probability(alpha, "alpha")
    provenance_value = _provenance_value(provenance)
    status_value = _status_value(experiment_status)
    srm_result, has_srm = _normalise_srm(srm, control_n, candidate_n)

    total = control_n + candidate_n
    has_real_traffic = (
        provenance_value == TrafficProvenance.PRODUCTION_AUTHENTICATED.value
        and total > 0
    )
    eligibility_blockers: list[str] = []
    if provenance_value == TrafficProvenance.DISABLED.value:
        eligibility_blockers.append("traffic_provenance_disabled")
    elif provenance_value == TrafficProvenance.INTERNAL.value:
        eligibility_blockers.append("traffic_provenance_internal")
    if not has_real_traffic:
        eligibility_blockers.append("no_real_traffic")
    if status_value != "completed":
        eligibility_blockers.append("experiment_not_completed")
    if not minimum_duration_reached:
        eligibility_blockers.append("minimum_duration_not_reached")
    if control_n < required_n or candidate_n < required_n:
        eligibility_blockers.append("insufficient_sample_size")
    if not has_srm:
        eligibility_blockers.append("srm_not_checked")
    elif (
        srm_result.get("status") == "insufficient"
        or srm_result.get("sufficient_sample") is False
    ):
        eligibility_blockers.append("srm_insufficient_sample")
    elif bool(srm_result.get("is_mismatch", srm_result.get("mismatch", False))):
        eligibility_blockers.append("sample_ratio_mismatch")

    control_rate = control_y / control_n if control_n else None
    candidate_rate = candidate_y / candidate_n if candidate_n else None
    descriptive_difference = (
        candidate_rate - control_rate
        if control_rate is not None and candidate_rate is not None
        else None
    )
    descriptive_relative_lift = (
        descriptive_difference / control_rate
        if descriptive_difference is not None
        and control_rate is not None
        and control_rate > 0.0
        else None
    )

    if not has_real_traffic:
        analysis_status = "no_real_traffic"
    elif (
        control_n < required_n
        or candidate_n < required_n
        or "srm_insufficient_sample" in eligibility_blockers
    ):
        analysis_status = "insufficient_data"
    elif eligibility_blockers:
        analysis_status = "blocked"
    else:
        analysis_status = "confirmatory"

    eligible = analysis_status == "confirmatory"
    effect: float | None = descriptive_difference if eligible else None
    relative_lift: float | None = descriptive_relative_lift if eligible else None
    z_score: float | None = None
    p_value: float | None = None
    confidence_interval: list[float] | None = None
    if eligible and control_rate is not None and candidate_rate is not None:
        z_score, p_value = _two_proportion_z(
            control_y,
            control_n,
            candidate_y,
            candidate_n,
        )
        confidence_interval = list(
            _difference_confidence_interval(
                control_rate,
                control_n,
                candidate_rate,
                candidate_n,
                alpha_value,
            )
        )

    statistically_significant: bool | None = None
    if eligible:
        statistically_significant = bool(
            p_value is not None
            and p_value <= alpha_value
            and effect is not None
            and effect != 0.0
        )
    interval_excludes_zero = bool(
        confidence_interval is not None
        and (confidence_interval[0] > 0.0 or confidence_interval[1] < 0.0)
    )
    can_claim_effect = bool(statistically_significant and interval_excludes_zero)
    claim_blockers = list(eligibility_blockers)
    if eligible and not can_claim_effect:
        claim_blockers.append("no_statistically_significant_effect")

    winner: Literal["control", "candidate"] | None = None
    if can_claim_effect and effect is not None:
        winner = "candidate" if effect > 0.0 else "control"

    return {
        "truth": "observed" if eligible else "no_claim",
        "analysis_status": analysis_status,
        "has_real_traffic": has_real_traffic,
        "can_claim_effect": can_claim_effect,
        "claim_blockers": claim_blockers,
        "winner": winner,
        "provenance": provenance_value,
        "experiment_status": status_value,
        "minimum_duration_reached": minimum_duration_reached,
        "required_sample_per_arm": required_n,
        "alpha": alpha_value,
        "control": {
            "successes": control_y,
            "total": control_n,
            "rate": control_rate,
        },
        "candidate": {
            "successes": candidate_y,
            "total": candidate_n,
            "rate": candidate_rate,
        },
        "descriptive": {
            "absolute_difference": descriptive_difference,
            "relative_lift": descriptive_relative_lift,
        },
        "absolute_effect": effect,
        "relative_lift": relative_lift,
        "z_score": z_score,
        "p_value": p_value,
        "confidence_interval": confidence_interval,
        "statistically_significant": statistically_significant,
        "interval_excludes_zero": interval_excludes_zero if eligible else None,
        "srm": srm_result,
    }


def _resolve_effect(primary: float | None, alias: float | None) -> float:
    if primary is None and alias is None:
        raise TypeError("minimum_detectable_effect is required")
    if primary is not None and alias is not None:
        left = _finite_number(primary, "minimum_detectable_effect")
        right = _finite_number(alias, "mde")
        if left != right:
            raise ValueError("minimum_detectable_effect and mde disagree")
        value = left
    else:
        value = _finite_number(
            primary if primary is not None else alias,
            "minimum_detectable_effect",
        )
    if not 0.0 < value < 1.0:
        raise ValueError("minimum_detectable_effect must be between 0 and 1")
    return value


def _expected_share(
    expected_candidate_share: float | None,
    candidate_allocation_bps: int | None,
) -> float:
    if candidate_allocation_bps is not None:
        bps = _non_negative_int(
            candidate_allocation_bps,
            "candidate_allocation_bps",
        )
        if not 1 <= bps <= 9_999:
            raise ValueError("candidate_allocation_bps must be between 1 and 9999")
        from_bps = bps / 10_000.0
        if expected_candidate_share is not None:
            explicit = _probability(
                expected_candidate_share,
                "expected_candidate_share",
                endpoints=False,
            )
            if not math.isclose(explicit, from_bps, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(
                    "expected_candidate_share and candidate_allocation_bps disagree"
                )
        return from_bps
    if expected_candidate_share is None:
        return 0.5
    return _probability(
        expected_candidate_share,
        "expected_candidate_share",
        endpoints=False,
    )


def _binomial_two_sided(successes: int, total: int, probability: float) -> float:
    if total == 0:
        return 1.0
    observed_log_probability = _binomial_log_pmf(successes, total, probability)
    # Match the conventional exact probability-ordering test while allowing a
    # tiny relative tolerance for outcomes that are mathematically tied.
    cutoff = observed_log_probability + math.log1p(1e-12)
    log_sum = -math.inf
    for outcome in range(total + 1):
        log_probability = _binomial_log_pmf(outcome, total, probability)
        if log_probability <= cutoff:
            log_sum = _log_add(log_sum, log_probability)
    result = min(1.0, max(0.0, math.exp(log_sum)))
    if math.isclose(result, 1.0, rel_tol=0.0, abs_tol=1e-10):
        return 1.0
    return result


def _binomial_log_pmf(successes: int, total: int, probability: float) -> float:
    failures = total - successes
    return (
        math.lgamma(total + 1)
        - math.lgamma(successes + 1)
        - math.lgamma(failures + 1)
        + successes * math.log(probability)
        + failures * math.log1p(-probability)
    )


def _log_add(left: float, right: float) -> float:
    if left == -math.inf:
        return right
    if right > left:
        left, right = right, left
    return left + math.log1p(math.exp(right - left))


def _normalise_srm(
    value: Mapping[str, Any] | bool | None,
    control_total: int,
    candidate_total: int,
) -> tuple[dict[str, Any], bool]:
    if value is None:
        # A direct library caller gets a safe exact default.  Services that
        # track exposure counts should pass their exposure-based SRM result.
        return srm_exact(control_total, candidate_total), True
    if isinstance(value, bool):
        return {
            "mismatch": value,
            "is_mismatch": value,
            "passes": not value,
        }, True
    if not isinstance(value, Mapping):
        raise TypeError("srm must be a mapping, boolean, or None")
    result = dict(value)
    if "is_mismatch" in result:
        mismatch = _strict_bool(result["is_mismatch"], "srm.is_mismatch")
    elif "mismatch" in result:
        mismatch = _strict_bool(result["mismatch"], "srm.mismatch")
    elif "passes" in result:
        mismatch = not _strict_bool(result["passes"], "srm.passes")
    elif "passed" in result:
        mismatch = not _strict_bool(result["passed"], "srm.passed")
    else:
        return result, False
    result["mismatch"] = mismatch
    result["is_mismatch"] = mismatch
    result["passes"] = not mismatch
    return result, True


def _two_proportion_z(
    control_successes: int,
    control_total: int,
    candidate_successes: int,
    candidate_total: int,
) -> tuple[float | None, float]:
    control_rate = control_successes / control_total
    candidate_rate = candidate_successes / candidate_total
    pooled = (control_successes + candidate_successes) / (
        control_total + candidate_total
    )
    variance = pooled * (1.0 - pooled) * (
        1.0 / control_total + 1.0 / candidate_total
    )
    if variance <= 0.0:
        return None, 1.0 if candidate_rate == control_rate else 0.0
    z_score = (candidate_rate - control_rate) / math.sqrt(variance)
    return z_score, math.erfc(abs(z_score) / math.sqrt(2.0))


def _difference_confidence_interval(
    control_rate: float,
    control_total: int,
    candidate_rate: float,
    candidate_total: int,
    alpha: float,
) -> tuple[float, float]:
    effect = candidate_rate - control_rate
    standard_error = math.sqrt(
        control_rate * (1.0 - control_rate) / control_total
        + candidate_rate * (1.0 - candidate_rate) / candidate_total
    )
    critical = _NORMAL.inv_cdf(1.0 - alpha / 2.0)
    return (
        max(-1.0, effect - critical * standard_error),
        min(1.0, effect + critical * standard_error),
    )


def _provenance_value(value: TrafficProvenance | str) -> str:
    if isinstance(value, TrafficProvenance):
        return value.value
    if not isinstance(value, str):
        raise TypeError("provenance must be a TrafficProvenance or string")
    try:
        return TrafficProvenance(value).value
    except ValueError as exc:
        allowed = ", ".join(member.value for member in TrafficProvenance)
        raise ValueError(f"provenance must be one of: {allowed}") from exc


def _status_value(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    if not isinstance(value, str) or not value:
        raise TypeError("experiment_status must be a non-empty string")
    return value


def _success_count(value: int, total: int, name: str) -> int:
    successes = _non_negative_int(value, name)
    if successes > total:
        raise ValueError(f"{name} must not exceed its arm total")
    return successes


def _non_negative_int(value: int, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    lower = 1 if positive else 0
    if value < lower:
        comparator = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be {comparator}")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _open_probability(value: Any, name: str) -> float:
    number = _finite_number(value, name)
    if not 0.0 < number < 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


def _probability(value: Any, name: str, *, endpoints: bool) -> float:
    number = _finite_number(value, name)
    valid = 0.0 <= number <= 1.0 if endpoints else 0.0 < number < 1.0
    if not valid:
        boundary = "in [0, 1]" if endpoints else "between 0 and 1"
        raise ValueError(f"{name} must be {boundary}")
    return number


__all__ = [
    "analyze_binary_outcome",
    "minimum_detectable_effect_binary",
    "required_sample_size_binary",
    "srm_exact",
]
