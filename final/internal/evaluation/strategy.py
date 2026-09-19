"""Statistics and canonical manifests for offline strategy review.

This module intentionally models paired *offline* evaluation evidence.  It
does not assign traffic, record exposures, estimate online outcomes, or claim
to implement an online A/B test.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_BOOTSTRAP_SEED = 20260914
DEFAULT_BOOTSTRAP_ITERATIONS = 10_000
DEFAULT_MIN_PAIRS = 20
MAX_STRATEGY_MANIFEST_BYTES = 256 * 1024
_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "privatekey",
)
_REFERENCE_SUFFIXES = ("ref", "reference", "env", "envvar", "secretname")


def canonical_manifest_json(manifest: Mapping[str, Any]) -> str:
    """Return the one canonical JSON representation used for content hashes."""

    value = validate_strategy_manifest(manifest)
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return canonical


def validate_strategy_manifest(
    manifest: Mapping[str, Any],
    *,
    max_bytes: int = MAX_STRATEGY_MANIFEST_BYTES,
) -> dict[str, Any]:
    """Validate a secret-free, bounded JSON strategy manifest.

    Credential values are never acceptable in an immutable evaluation
    snapshot.  Callers must store a reference such as ``api_key_env`` or
    ``secret_ref`` and resolve it only at runtime.
    """

    if not isinstance(manifest, Mapping) or not manifest:
        raise ValueError("strategy manifest must be a non-empty JSON object")
    max_bytes = _bounded_int(max_bytes, "max_bytes", minimum=1, maximum=10 * 1024 * 1024)
    value = _strict_json_value(dict(manifest), path="manifest", depth=0)
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    size = len(canonical.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(
            f"strategy manifest exceeds {max_bytes} bytes; store compact version references"
        )
    return value


def manifest_sha256(manifest: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(canonical_json, sha256)`` for an immutable manifest."""

    canonical = canonical_manifest_json(manifest)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mcnemar_exact(
    baseline: Sequence[bool | int],
    candidate: Sequence[bool | int],
    *,
    alpha: float = 0.05,
    min_pairs: int = DEFAULT_MIN_PAIRS,
    min_discordant: int = 1,
) -> dict[str, Any]:
    """Two-sided exact McNemar test over paired pass/fail outcomes.

    ``fixed`` means baseline failed and candidate passed. ``regressions`` is
    the reverse.  Samples below the declared evidence floor, or with no
    discordant pairs, are explicitly inconclusive rather than silently green.
    """

    before = _boolean_pairs(baseline, "baseline")
    after = _boolean_pairs(candidate, "candidate")
    _validate_pair_lengths(before, after)
    alpha = _probability(alpha, "alpha", inclusive=False)
    min_pairs = _positive_int(min_pairs, "min_pairs")
    min_discordant = _positive_int(min_discordant, "min_discordant")

    fixed = sum(not left and right for left, right in zip(before, after))
    regressions = sum(left and not right for left, right in zip(before, after))
    discordant = fixed + regressions
    p_value = _two_sided_binomial_p_value(fixed, regressions)
    direction = (
        "candidate_better" if fixed > regressions
        else "baseline_better" if regressions > fixed
        else "no_difference"
    )
    reasons: list[str] = []
    if len(before) < min_pairs:
        reasons.append(f"requires at least {min_pairs} paired cases")
    if discordant < min_discordant:
        reasons.append(f"requires at least {min_discordant} discordant pair")
    status = "inconclusive" if reasons else "ok"
    significant = status == "ok" and p_value <= alpha
    return {
        "method": "mcnemar_exact",
        "status": status,
        "reason": "; ".join(reasons),
        "n_pairs": len(before),
        "fixed": fixed,
        "regressions": regressions,
        "discordant_pairs": discordant,
        "p_value": p_value,
        "alpha": alpha,
        "significant": significant,
        "direction": direction,
        "passed": bool(significant and direction == "candidate_better"),
    }


def paired_bootstrap_ci(
    baseline_scores: Sequence[float | int],
    candidate_scores: Sequence[float | int],
    *,
    confidence: float = 0.95,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    min_pairs: int = DEFAULT_MIN_PAIRS,
) -> dict[str, Any]:
    """Fixed-seed percentile bootstrap CI for paired candidate score deltas."""

    before = _finite_scores(baseline_scores, "baseline_scores")
    after = _finite_scores(candidate_scores, "candidate_scores")
    _validate_pair_lengths(before, after)
    confidence = _probability(confidence, "confidence", inclusive=False)
    iterations = _bounded_int(iterations, "iterations", minimum=100, maximum=1_000_000)
    min_pairs = _positive_int(min_pairs, "min_pairs")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")

    deltas = [right - left for left, right in zip(before, after)]
    mean_delta = sum(deltas) / len(deltas) if deltas else None
    ci_low: float | None = None
    ci_high: float | None = None
    reasons: list[str] = []
    if len(deltas) < min_pairs:
        reasons.append(f"requires at least {min_pairs} paired scores")
    if deltas:
        rng = random.Random(seed)
        n = len(deltas)
        samples = [
            sum(deltas[rng.randrange(n)] for _ in range(n)) / n
            for _ in range(iterations)
        ]
        samples.sort()
        tail = (1.0 - confidence) / 2.0
        ci_low = _quantile(samples, tail)
        ci_high = _quantile(samples, 1.0 - tail)

    status = "inconclusive" if reasons else "ok"
    if ci_low is not None and ci_low > 0:
        direction = "candidate_better"
    elif ci_high is not None and ci_high < 0:
        direction = "baseline_better"
    else:
        direction = "no_difference"
    return {
        "method": "paired_bootstrap_95ci",
        "status": status,
        "reason": "; ".join(reasons),
        "n_pairs": len(deltas),
        "mean_delta": mean_delta,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "confidence": confidence,
        "iterations": iterations,
        "seed": seed,
        "direction": direction,
        "passed": bool(status == "ok" and direction == "candidate_better"),
    }


def paired_bootstrap_delta(
    baseline_scores: Sequence[float | int],
    candidate_scores: Sequence[float | int],
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility alias with an explicit function signature boundary."""

    return paired_bootstrap_ci(baseline_scores, candidate_scores, **kwargs)


def evaluate_paired_statistics(
    baseline_passed: Sequence[bool | int],
    candidate_passed: Sequence[bool | int],
    baseline_scores: Sequence[float | int],
    candidate_scores: Sequence[float | int],
) -> dict[str, Any]:
    """Combine both required offline tests into one conservative decision."""

    pair_counts = {
        len(baseline_passed),
        len(candidate_passed),
        len(baseline_scores),
        len(candidate_scores),
    }
    if len(pair_counts) != 1:
        raise ValueError("all paired status and score inputs must have the same length")

    mcnemar = mcnemar_exact(baseline_passed, candidate_passed)
    bootstrap = paired_bootstrap_ci(baseline_scores, candidate_scores)
    passed = bool(mcnemar["passed"] and bootstrap["passed"])
    if passed:
        status = "passed"
    elif "inconclusive" in {mcnemar["status"], bootstrap["status"]}:
        status = "inconclusive"
    else:
        status = "blocked"
    reasons = [
        value
        for value in (
            f"McNemar: {mcnemar['reason']}" if mcnemar["status"] == "inconclusive" else "",
            f"Bootstrap: {bootstrap['reason']}" if bootstrap["status"] == "inconclusive" else "",
            "McNemar did not show significant candidate improvement"
            if mcnemar["status"] == "ok" and not mcnemar["passed"] else "",
            "Bootstrap 95% CI did not stay above zero"
            if bootstrap["status"] == "ok" and not bootstrap["passed"] else "",
        )
        if value
    ]
    return {
        "scope": "offline_evaluation",
        "n_pairs": len(baseline_passed),
        "status": status,
        "passed": passed,
        "reasons": reasons,
        "mcnemar": mcnemar,
        "paired_bootstrap": bootstrap,
    }


def _two_sided_binomial_p_value(left_only: int, right_only: int) -> float:
    discordant = left_only + right_only
    if discordant == 0:
        return 1.0
    tail_count = min(left_only, right_only)
    numerator = sum(math.comb(discordant, index) for index in range(tail_count + 1))
    return min(1.0, 2.0 * (numerator / (2 ** discordant)))


def _boolean_pairs(values: Sequence[bool | int], field: str) -> list[bool]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{field} must be a sequence")
    result: list[bool] = []
    for value in values:
        if isinstance(value, bool):
            result.append(value)
        elif isinstance(value, int) and value in {0, 1}:
            result.append(bool(value))
        else:
            raise ValueError(f"{field} values must be boolean or 0/1")
    return result


def _finite_scores(values: Sequence[float | int], field: str) -> list[float]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{field} must be a sequence")
    result: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError(f"{field} values must be finite numbers")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} values must be finite numbers") from exc
        if not math.isfinite(number):
            raise ValueError(f"{field} values must be finite numbers")
        result.append(number)
    return result


def _validate_pair_lengths(left: Sequence[Any], right: Sequence[Any]) -> None:
    if len(left) != len(right):
        raise ValueError("paired samples must have the same length")


def _probability(value: float, field: str, *, inclusive: bool) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    valid = 0.0 <= number <= 1.0 if inclusive else 0.0 < number < 1.0
    if not math.isfinite(number) or not valid:
        brackets = "[0, 1]" if inclusive else "(0, 1)"
        raise ValueError(f"{field} must be in {brackets}")
    return number


def _positive_int(value: int, field: str) -> int:
    return _bounded_int(value, field, minimum=1, maximum=1_000_000)


def _bounded_int(value: int, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _quantile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(values[lower])
    weight = position - lower
    return float(values[lower] * (1.0 - weight) + values[upper] * weight)


def _strict_json_value(value: Any, *, path: str, depth: int) -> Any:
    if depth > 32:
        raise ValueError(f"{path} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and _looks_like_secret_value(value):
            raise ValueError(
                f"{path} contains a credential value; store a secret reference instead"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [
            _strict_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} object keys must be non-empty strings")
            normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if (
                any(marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS)
                and not normalized_key.endswith(_REFERENCE_SUFFIXES)
            ):
                raise ValueError(
                    f"{path}.{key} is a sensitive field; store a secret reference instead"
                )
            result[key] = _strict_json_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
            )
        return result
    raise ValueError(f"{path} contains unsupported JSON value {type(value).__name__}")


def _looks_like_secret_value(value: str) -> bool:
    text = value.strip()
    lowered = text.casefold()
    if not text:
        return False
    if lowered.startswith(("bearer ", "basic ")):
        return True
    if "-----begin " in lowered and "private key-----" in lowered:
        return True
    if re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", text):
        return True
    if re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text):
        return True
    if re.search(r"(?:session(?:id)?|cookie)=[^;\s]{8,}(?:;|$)", text, re.IGNORECASE):
        return True
    return False


__all__ = [
    "DEFAULT_BOOTSTRAP_ITERATIONS",
    "DEFAULT_BOOTSTRAP_SEED",
    "DEFAULT_MIN_PAIRS",
    "MAX_STRATEGY_MANIFEST_BYTES",
    "canonical_manifest_json",
    "evaluate_paired_statistics",
    "manifest_sha256",
    "mcnemar_exact",
    "paired_bootstrap_ci",
    "paired_bootstrap_delta",
    "validate_strategy_manifest",
]
