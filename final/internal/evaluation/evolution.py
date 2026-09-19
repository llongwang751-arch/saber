"""Deterministic, evidence-bound RAG strategy suggestions.

This module deliberately stops at an *offline experiment hypothesis*.  It does
not edit prompts, models, tools or memory, and it never applies a suggestion to
runtime traffic.  A separate human review and materialisation step is required
before the normal P2/P3 gates can even begin.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .strategy import canonical_manifest_json


EVOLUTION_RULE_VERSION = "rag-badcase-rules-v1"
DEFAULT_TOP_K = 5
DEFAULT_NO_ANSWER_THRESHOLD = 0.5

_RETRIEVAL_METRICS = frozenset(
    {
        "rag_evidence_f1",
        "rag_recall_at_k",
        "rag_mrr",
        "rag_ndcg_at_k",
    }
)


class _RAGSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    top_k: StrictInt | None = Field(default=None, ge=1, le=20)
    no_answer_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("no_answer_threshold", mode="before")
    @classmethod
    def _finite_number(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("no_answer_threshold must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError("no_answer_threshold must be finite")
        return float(value)


class _RuntimeOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rag: _RAGSuggestion


class _SuggestionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    runtime_overrides: _RuntimeOverrides


def canonical_checksum(value: Any) -> str:
    """Return a stable SHA-256 for JSON evidence and audit requests."""

    canonical = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_suggestion_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Accept exactly the two request-scoped, read-only RAG knobs from v1."""

    if not isinstance(manifest, Mapping):
        raise TypeError("suggestion manifest must be an object")
    model = _SuggestionManifest.model_validate(dict(manifest))
    value = model.model_dump(mode="json", exclude_none=True)
    rag = value["runtime_overrides"]["rag"]
    if not rag:
        raise ValueError("suggestion must change at least one safe RAG parameter")
    # Reuse P2's size, secret and canonical-JSON checks as defence in depth.
    return json.loads(canonical_manifest_json(value))


def build_evidence_snapshot(
    *,
    run: Mapping[str, Any],
    dataset_version: Mapping[str, Any],
    strategy_version: Mapping[str, Any] | None = None,
    cases: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    badcases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the immutable evidence envelope referenced by one suggestion."""

    run_snapshot = {
        "id": str(run.get("id") or ""),
        "dataset_version_id": str(run.get("dataset_version_id") or ""),
        "strategy_version_id": run.get("strategy_version_id"),
        "status": str(run.get("status") or ""),
        "config": _json_value(run.get("config") or {}),
        "summary": _json_value(run.get("summary") or {}),
        "metadata": _json_value(run.get("metadata") or {}),
    }
    version_snapshot = {
        "id": str(dataset_version.get("id") or ""),
        "dataset_id": str(dataset_version.get("dataset_id") or ""),
        "version": dataset_version.get("version"),
        "checksum": str(dataset_version.get("checksum") or ""),
        "case_count": int(dataset_version.get("case_count") or len(cases)),
        "metadata": _json_value(dataset_version.get("metadata") or {}),
    }
    strategy_snapshot: dict[str, Any] | None = None
    if strategy_version is not None:
        manifest = _json_value(strategy_version.get("manifest") or {})
        canonical = canonical_manifest_json(manifest)
        checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if (
            canonical != str(strategy_version.get("manifest_canonical_json") or "")
            or checksum != str(strategy_version.get("manifest_checksum") or "")
        ):
            raise ValueError("source strategy manifest checksum mismatch")
        strategy_payload = {
            "id": str(strategy_version.get("id") or ""),
            "version": strategy_version.get("version"),
            "source": str(strategy_version.get("source") or ""),
            "manifest": manifest,
            "manifest_canonical_json": canonical,
            "manifest_checksum": checksum,
        }
        strategy_snapshot = {
            **strategy_payload,
            "checksum": canonical_checksum(strategy_payload),
        }
    case_refs = []
    for case in sorted(cases, key=lambda item: (int(item.get("position") or 0), str(item.get("id") or ""))):
        payload = _json_value(case.get("payload") or {})
        case_refs.append(
            {
                "id": str(case.get("id") or ""),
                "case_id": str(case.get("case_id") or ""),
                "position": int(case.get("position") or 0),
                "metadata": _json_value(
                    payload.get("metadata")
                    if isinstance(payload.get("metadata"), Mapping)
                    else {}
                ),
                "payload_checksum": canonical_checksum(payload),
            }
        )
    result_refs = []
    for result in sorted(results, key=lambda item: (str(item.get("case_id") or ""), str(item.get("id") or ""))):
        snapshot = {
            "id": str(result.get("id") or ""),
            "eval_case_id": str(result.get("eval_case_id") or ""),
            "case_id": str(result.get("case_id") or ""),
            "status": str(result.get("status") or ""),
            "output": _json_value(result.get("output") or {}),
            "metrics": _json_value(result.get("metrics") or {}),
            "error": _json_value(result.get("error") or {}),
        }
        result_refs.append({**snapshot, "checksum": canonical_checksum(snapshot)})
    badcase_refs = []
    for badcase in sorted(badcases, key=lambda item: str(item.get("id") or "")):
        snapshot = {
            "id": str(badcase.get("id") or ""),
            "case_run_id": str(badcase.get("case_run_id") or ""),
            "eval_case_id": str(badcase.get("eval_case_id") or ""),
            "category": str(badcase.get("category") or ""),
            "severity": str(badcase.get("severity") or ""),
            "status": str(badcase.get("status") or ""),
            "details": _json_value(badcase.get("details") or {}),
            "resolution": _json_value(badcase.get("resolution") or {}),
        }
        badcase_refs.append({**snapshot, "checksum": canonical_checksum(snapshot)})
    return {
        "run": {**run_snapshot, "checksum": canonical_checksum(run_snapshot)},
        "dataset_version": version_snapshot,
        "strategy_version": strategy_snapshot,
        "cases": case_refs,
        "case_results": result_refs,
        "badcases": badcase_refs,
    }


def derive_rag_suggestion(
    *,
    evidence: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    current_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive one deterministic, bounded hypothesis from failed RAG signals."""

    base_top_k, base_threshold, base_limitations = _base_parameters(current_manifest)
    cases_by_id = {str(item.get("id") or ""): item for item in cases}
    retrieval_failures = 0
    false_answer_failures = 0
    false_abstain_failures = 0
    no_answer_unclassified = 0
    failed_metric_count = 0

    for result in results:
        failed_names = _failed_metric_names(result)
        failed_metric_count += len(failed_names)
        retrieval_failures += sum(name in _RETRIEVAL_METRICS for name in failed_names)
        if "no_answer_decision" not in failed_names:
            continue
        case = cases_by_id.get(str(result.get("eval_case_id") or ""), {})
        payload = case.get("payload") if isinstance(case, Mapping) else {}
        expected = payload.get("expected") if isinstance(payload, Mapping) else {}
        answerable = expected.get("answerable") if isinstance(expected, Mapping) else None
        output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
        abstained = output.get("abstained")
        if answerable is False and abstained is not True:
            false_answer_failures += 1
        elif answerable is True and abstained is True:
            false_abstain_failures += 1
        else:
            no_answer_unclassified += 1

    badcase_entries = evidence.get("badcases") if isinstance(evidence, Mapping) else []
    retrieval_badcases = sum(
        str(item.get("category") or "").upper() == "RETRIEVAL"
        for item in (badcase_entries if isinstance(badcase_entries, Sequence) else [])
        if isinstance(item, Mapping)
    )
    retrieval_signal = max(retrieval_failures, retrieval_badcases)
    rag_changes: dict[str, Any] = {}
    rationale: list[str] = []
    if retrieval_signal:
        step = min(4, max(1, math.ceil(retrieval_signal / 2)))
        candidate_top_k = min(20, base_top_k + step)
        if candidate_top_k != base_top_k:
            rag_changes["top_k"] = candidate_top_k
            rationale.append(
                f"发现 {retrieval_signal} 个检索失败信号；将 top_k 从 {base_top_k} 提议为 {candidate_top_k}，仅作为待回归验证的召回假设。"
            )

    threshold_direction = false_answer_failures - false_abstain_failures
    if threshold_direction:
        magnitude = min(0.15, 0.05 * abs(threshold_direction))
        candidate_threshold = round(
            min(1.0, max(0.0, base_threshold + (magnitude if threshold_direction > 0 else -magnitude))),
            6,
        )
        if candidate_threshold != base_threshold:
            rag_changes["no_answer_threshold"] = candidate_threshold
            direction_text = "提高拒答门槛" if threshold_direction > 0 else "降低过度拒答"
            rationale.append(
                f"无答案误判净信号为 {threshold_direction:+d}；将阈值从 {base_threshold:.3f} 提议为 {candidate_threshold:.3f}，用于{direction_text}的离线假设。"
            )

    if not rag_changes:
        raise ValueError(
            "completed run has no actionable v1 RAG signal; no safe parameter suggestion was created"
        )

    truth = classify_evidence_truth(evidence=evidence, results=results)
    limitations = [
        *base_limitations,
        "该记录只是确定性实验假设，不证明离线或线上效果提升。",
        "接受后仍需显式物化，再完成同一不可变数据集上的完整 baseline/candidate 评测与 P2 门禁。",
        "进入线上前仍需 P3 双人审批、真实曝光、样本量、SRM 与安全门禁；本步骤不会自动部署。",
    ]
    if no_answer_unclassified:
        limitations.append(
            f"{no_answer_unclassified} 个无答案失败缺少可判定方向，未用于阈值调整。"
        )
    if not truth["evidence_complete"]:
        limitations.append("证据字段不完整，建议只能作为待验证假设。")
    if truth["synthetic"] or truth["replay"]:
        limitations.append("证据包含合成数据或 Replay 输出，不得解释为真实模型或真实用户效果。")

    manifest = validate_suggestion_manifest(
        {"runtime_overrides": {"rag": rag_changes}}
    )
    return {
        "manifest": manifest,
        "rationale": rationale,
        "limitations": list(dict.fromkeys(limitations)),
        "truth": truth,
        "diagnostics": {
            "base": {
                "top_k": base_top_k,
                "no_answer_threshold": base_threshold,
            },
            "retrieval_failures": retrieval_failures,
            "retrieval_badcases": retrieval_badcases,
            "false_answer_failures": false_answer_failures,
            "false_abstain_failures": false_abstain_failures,
            "unclassified_no_answer_failures": no_answer_unclassified,
            "failed_metric_count": failed_metric_count,
        },
    }


def classify_evidence_truth(
    *, evidence: Mapping[str, Any], results: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    run = evidence.get("run") if isinstance(evidence.get("run"), Mapping) else {}
    version = (
        evidence.get("dataset_version")
        if isinstance(evidence.get("dataset_version"), Mapping)
        else {}
    )
    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    adapter = config.get("adapter") if isinstance(config.get("adapter"), Mapping) else {}
    metadata = run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {}
    version_metadata = (
        version.get("metadata") if isinstance(version.get("metadata"), Mapping) else {}
    )
    case_refs = evidence.get("cases") if isinstance(evidence.get("cases"), list) else []
    case_metadata = [
        item.get("metadata")
        for item in case_refs
        if isinstance(item, Mapping) and isinstance(item.get("metadata"), Mapping)
    ]
    adapter_type = str(adapter.get("type") or "").strip().casefold()
    replay = adapter_type == "replay"
    truth_text = json.dumps(
        {"run": metadata, "dataset": version_metadata, "cases": case_metadata},
        ensure_ascii=False,
        sort_keys=True,
    ).casefold()
    synthetic = bool(
        metadata.get("synthetic")
        or version_metadata.get("synthetic")
        or any(bool(item.get("synthetic")) for item in case_metadata)
        or "synthetic" in truth_text
        or "合成" in truth_text
    )
    expected_count = int(version.get("case_count") or 0)
    terminal = {"passed", "failed", "error", "skipped"}
    evidence_complete = bool(
        expected_count > 0
        and len(results) == expected_count
        and all(str(item.get("status") or "") in terminal for item in results)
        and all(isinstance(item.get("metrics"), Mapping) and item.get("metrics") for item in results)
    )
    return {
        "classification": (
            "synthetic_or_replay_hypothesis"
            if synthetic or replay
            else "offline_evaluation_hypothesis"
        ),
        "adapter_type": adapter_type or "unknown",
        "evidence_complete": evidence_complete,
        "synthetic": synthetic,
        "replay": replay,
        "claim": "experiment_hypothesis_only",
        "can_claim_offline_improvement": False,
        "can_claim_online_improvement": False,
        "no_auto_apply": True,
    }


def verify_evidence_checksums(evidence: Mapping[str, Any]) -> None:
    """Reject a persisted evidence envelope whose embedded refs were altered."""

    for section in ("run",):
        item = evidence.get(section)
        if not isinstance(item, Mapping):
            raise ValueError(f"evolution evidence is missing {section}")
        declared = str(item.get("checksum") or "")
        payload = {key: value for key, value in item.items() if key != "checksum"}
        if declared != canonical_checksum(payload):
            raise ValueError(f"evolution evidence {section} checksum mismatch")
    run = evidence["run"]
    source_strategy_id = run.get("strategy_version_id")
    strategy = evidence.get("strategy_version")
    if source_strategy_id:
        if not isinstance(strategy, Mapping):
            raise ValueError("evolution evidence is missing source strategy")
        declared = str(strategy.get("checksum") or "")
        payload = {key: value for key, value in strategy.items() if key != "checksum"}
        if declared != canonical_checksum(payload):
            raise ValueError("evolution evidence strategy_version checksum mismatch")
        if str(strategy.get("id") or "") != str(source_strategy_id):
            raise ValueError("evolution evidence source strategy id mismatch")
        manifest = strategy.get("manifest")
        if not isinstance(manifest, Mapping):
            raise ValueError("evolution evidence source strategy manifest is invalid")
        canonical = canonical_manifest_json(manifest)
        checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if (
            canonical != str(strategy.get("manifest_canonical_json") or "")
            or checksum != str(strategy.get("manifest_checksum") or "")
        ):
            raise ValueError("evolution evidence source strategy checksum mismatch")
    elif strategy is not None:
        raise ValueError("evolution evidence contains an unrelated source strategy")
    for section in ("case_results", "badcases"):
        entries = evidence.get(section)
        if not isinstance(entries, list):
            raise ValueError(f"evolution evidence is missing {section}")
        for item in entries:
            if not isinstance(item, Mapping):
                raise ValueError(f"evolution evidence {section} entry is invalid")
            declared = str(item.get("checksum") or "")
            payload = {key: value for key, value in item.items() if key != "checksum"}
            if declared != canonical_checksum(payload):
                raise ValueError(f"evolution evidence {section} checksum mismatch")


def _base_parameters(
    manifest: Mapping[str, Any] | None,
) -> tuple[int, float, list[str]]:
    top_k = DEFAULT_TOP_K
    threshold = DEFAULT_NO_ANSWER_THRESHOLD
    limitations: list[str] = []
    if not isinstance(manifest, Mapping):
        limitations.append("源运行未绑定可解析的策略版本，基线参数采用保守默认值。")
        return top_k, threshold, limitations
    runtime = manifest.get("runtime_overrides")
    if not isinstance(runtime, Mapping):
        limitations.append("源策略没有 runtime_overrides.rag，基线参数采用保守默认值。")
        return top_k, threshold, limitations
    rag = runtime.get("rag")
    if not isinstance(rag, Mapping):
        limitations.append("源策略没有可解析的 RAG 参数，基线参数采用保守默认值。")
        return top_k, threshold, limitations
    raw_top_k = rag.get("top_k")
    if isinstance(raw_top_k, int) and not isinstance(raw_top_k, bool) and 1 <= raw_top_k <= 20:
        top_k = raw_top_k
    raw_threshold = rag.get("no_answer_threshold")
    if (
        isinstance(raw_threshold, (int, float))
        and not isinstance(raw_threshold, bool)
        and math.isfinite(float(raw_threshold))
        and 0.0 <= float(raw_threshold) <= 1.0
    ):
        threshold = float(raw_threshold)
    return top_k, threshold, limitations


def _failed_metric_names(result: Mapping[str, Any]) -> list[str]:
    metrics = result.get("metrics")
    entries = metrics.get("metrics") if isinstance(metrics, Mapping) else None
    if not isinstance(entries, list):
        return []
    return [
        str(item.get("name") or "").strip()
        for item in entries
        if isinstance(item, Mapping)
        and item.get("passed") is False
        and str(item.get("name") or "").strip()
    ]


def _json_value(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    )


__all__ = [
    "EVOLUTION_RULE_VERSION",
    "build_evidence_snapshot",
    "canonical_checksum",
    "classify_evidence_truth",
    "derive_rag_suggestion",
    "validate_suggestion_manifest",
    "verify_evidence_checksums",
]
