"""Application service for reproducible Agent evaluation runs."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time
from typing import Any, Iterable, Mapping

from .adapters import build_adapter, redact_adapter_config
from .evaluators import evaluate
from .evolution import (
    EVOLUTION_RULE_VERSION,
    build_evidence_snapshot,
    canonical_checksum as evolution_checksum,
    derive_rag_suggestion,
)
from .schemas import EvalCase, EvaluationReport, MetricDimension, ReleaseGate
from .store import EvaluationStore, promotion_audit_integrity_key
from .strategy import evaluate_paired_statistics
from . import reporting


BADCASE_CATEGORY_BY_DIMENSION = {
    MetricDimension.INTENT.value: "INTENT",
    MetricDimension.INFORMATION.value: "SLOT_OR_STATE",
    MetricDimension.TOOL.value: "TOOL_ARGUMENT",
    MetricDimension.RESPONSE.value: "GENERATION",
    MetricDimension.RAG.value: "RETRIEVAL",
    MetricDimension.FALLBACK.value: "TOOL_RUNTIME",
    MetricDimension.SAFETY.value: "SAFETY",
    MetricDimension.TRACE.value: "INFRA",
    MetricDimension.MEMORY.value: "SLOT_OR_STATE",
    MetricDimension.HARNESS.value: "TOOL_RUNTIME",
}

METRIC_DISPLAY_NAMES = {
    "intent_accuracy": "意图识别准确率",
    "required_slot_recall": "必要信息召回率",
    "tool_selection_f1": "工具选择 F1",
    "tool_argument_accuracy": "工具参数准确率",
    "tool_outcome_accuracy": "工具结果状态准确率",
    "required_content_recall": "必要回复内容召回率",
    "rag_evidence_f1": "RAG 证据 F1",
    "rag_recall_at_k": "RAG 召回率",
    "rag_mrr": "RAG 首条命中排序",
    "rag_ndcg_at_k": "RAG 分级排序质量",
    "no_answer_decision": "无答案判断准确率",
    "memory_behavior": "记忆读写正确性",
    "fallback_recovery": "异常兜底恢复率",
    "privacy_non_disclosure": "隐私防泄露",
    "boundary_safety": "安全边界",
    "trace_completeness": "执行轨迹完整性",
}

BADCASE_CATEGORY_DISPLAY_NAMES = {
    "INTENT": "意图识别 (INTENT)",
    "SLOT_OR_STATE": "信息与记忆 (SLOT_OR_STATE)",
    "TOOL_ARGUMENT": "工具参数 (TOOL_ARGUMENT)",
    "GENERATION": "回复生成 (GENERATION)",
    "RETRIEVAL": "知识检索 (RETRIEVAL)",
    "TOOL_RUNTIME": "工具运行 (TOOL_RUNTIME)",
    "SAFETY": "安全问题 (SAFETY)",
    "INFRA": "基础设施与轨迹 (INFRA)",
}


class EvaluationService:
    """Coordinates immutable datasets, adapters, evaluators and Badcases."""

    def __init__(self, store: EvaluationStore | None = None, *, local_agent: Any | None = None):
        self.store = store or EvaluationStore()
        self.local_agent = local_agent
        self._workers: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def close(self) -> None:
        self.store.close()

    def create_dataset(
        self,
        name: str,
        *,
        description: str = "",
        domain: str = "agent-quality",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged_metadata = {"domain": domain, **dict(metadata or {})}
        return self.store.create_dataset(
            name,
            description=description,
            metadata=merged_metadata,
        )

    def import_cases(
        self,
        dataset_id: str,
        cases: Iterable[Mapping[str, Any] | EvalCase],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        validated = [
            item if isinstance(item, EvalCase) else EvalCase.model_validate(item)
            for item in cases
        ]
        return self.store.import_dataset_version(
            dataset_id,
            [item.model_dump(mode="json") for item in validated],
            metadata=metadata,
        )

    def import_jsonl(
        self,
        dataset_id: str,
        path: str | Path,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = Path(path)
        cases: list[dict[str, Any]] = []
        with source.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"JSONL line {line_number} must be an object")
                cases.append(value)
        return self.import_cases(
            dataset_id,
            cases,
            metadata={"source": str(source), **dict(metadata or {})},
        )

    def create_run(
        self,
        dataset_version_id: str,
        *,
        name: str = "",
        adapter: Mapping[str, Any] | None = None,
        release_gate: Mapping[str, Any] | ReleaseGate | None = None,
        metadata: Mapping[str, Any] | None = None,
        strategy_version_id: str | None = None,
    ) -> dict[str, Any]:
        raw_adapter = dict(adapter or {"type": "replay", "profile": "baseline"})
        # Build once for validation, but persist only a redacted configuration.
        build_adapter(raw_adapter, self.local_agent)
        safe_adapter = redact_adapter_config(raw_adapter)
        gate = (
            release_gate
            if isinstance(release_gate, ReleaseGate)
            else ReleaseGate.model_validate(release_gate or {})
        )
        config = {
            "adapter": safe_adapter,
            "release_gate": gate.model_dump(mode="json"),
        }
        # Runtime-only headers stay in memory and never enter SQLite.
        run = self.store.create_run(
            dataset_version_id,
            name=name,
            config=config,
            metadata=metadata,
            strategy_version_id=strategy_version_id,
        )
        if raw_adapter != safe_adapter:
            with self._lock:
                run_id = run["id"]
                self._runtime_adapter_configs()[run_id] = raw_adapter
        return run

    def readiness(self) -> dict[str, Any]:
        """Describe what each evaluation mode really executes.

        Replay is intentionally always available because it only reads the
        bundled labelled outputs.  ``live.ready`` means that an in-process
        Agent can be invoked; a mock LLM is reported explicitly instead of
        being disguised as a real provider or treated as an API outage.
        """

        agent = self.local_agent
        agent_available = agent is not None
        cfg = getattr(agent, "cfg", None) if agent_available else None
        is_real_llm = False
        checker = getattr(cfg, "is_real_llm", None)
        try:
            is_real_llm = bool(checker() if callable(checker) else checker)
        except Exception:
            is_real_llm = False

        rag = getattr(agent, "rag", None) if agent_available else None
        rag_mode = "unavailable"
        if rag is not None:
            mode = getattr(rag, "mode", None)
            try:
                rag_mode = str(mode() if callable(mode) else mode or "available")
            except Exception:
                rag_mode = "unknown"

        tools_count = 0
        if agent_available and hasattr(agent, "get_tools"):
            try:
                tools_count = len(agent.get_tools() or [])
            except Exception:
                tools_count = 0

        dependencies = _dependency_status(agent)
        reasons = [] if agent_available else ["本进程未注入可供 Local adapter 调用的 Agent。"]
        warnings: list[str] = []
        if agent_available and not is_real_llm:
            warnings.append("当前 LLM 为 Mock/降级模式；会走真实 Agent 链路，但结果不代表真实模型效果。")
        if rag is None:
            warnings.append("RAG 未注入；需要知识库的用例可能降级或失败。")

        return {
            "replay": {
                "ready": True,
                "adapter": "replay",
                "execution_mode": "synthetic_replay",
                "execution": "preauthored-output-replay",
                "data_policy": "synthetic-only",
                "calls_model": False,
                "calls_tools": False,
                "calls_rag": False,
                "invokes_model": False,
                "invokes_tools": False,
                "invokes_rag": False,
            },
            "live": {
                "ready": agent_available,
                "adapter": "local",
                "execution_mode": "local_agent",
                "execution": "live-agent",
                "agent_available": agent_available,
                "llm_mode": "real" if is_real_llm else "mock",
                "calls_model": True,
                "calls_tools": True,
                "calls_rag": True,
                "invokes_model": True,
                "invokes_tools": True,
                "invokes_rag": True,
                "llm": {
                    "mode": "real" if is_real_llm else "mock",
                    "real": is_real_llm,
                    "model": str(getattr(cfg, "llm_model", "") or ""),
                },
                "tools": {"available": tools_count > 0, "count": tools_count},
                "rag": {
                    "available": rag is not None,
                    "loaded": bool(getattr(rag, "loaded", False)) if rag is not None else False,
                    "mode": rag_mode,
                },
                "reasons": reasons,
                "warnings": warnings,
            },
            "dependencies": dependencies,
        }

    def bootstrap_demo(self, *, execute: bool = True) -> dict[str, Any]:
        """Create the bundled synthetic dataset and two comparable runs."""

        dataset_name = "tencent-jd-synthetic-agent-eval"
        dataset = next(
            (item for item in self.store.list_datasets() if item["name"] == dataset_name),
            None,
        )
        if dataset is None:
            dataset = self.create_dataset(
                dataset_name,
                description="合成、脱敏的 Agent 工程链路评测集，不用于临床判断",
                domain="medical-agent-quality",
                metadata={"data_policy": "synthetic-only"},
            )
        final_root = Path(__file__).resolve().parents[2]
        version = self.import_jsonl(
            dataset["id"],
            final_root / "examples" / "evaluation" / "tencent_medical_agent_eval.jsonl",
            metadata={
                "purpose": "Tencent JD interview demo",
                "source_dataset": "tencent_medical_agent_eval.jsonl",
                "data_policy": "synthetic-only",
                "execution_mode": "synthetic_replay",
            },
        )
        execution = {
            "adapter": "replay",
            "execution": "preauthored-output-replay",
            "execution_mode": "synthetic_replay",
            "data_policy": "synthetic-only",
            "real_vs_mock": "synthetic",
            "synthetic_execution": True,
            "calls_model": False,
            "calls_tools": False,
            "calls_rag": False,
            "invokes_model": False,
            "invokes_tools": False,
            "invokes_rag": False,
        }
        run_metadata = {
            "source": "bundled_fixture",
            "source_dataset": "tencent_medical_agent_eval.jsonl",
            "execution_mode": execution["execution_mode"],
            "data_policy": execution["data_policy"],
            "invokes_model": execution["invokes_model"],
            "invokes_tools": execution["invokes_tools"],
            "invokes_rag": execution["invokes_rag"],
            "execution": execution,
        }
        baseline = self.create_run(
            version["id"],
            name="baseline-agent",
            adapter={"type": "replay", "profile": "baseline"},
            metadata=run_metadata,
        )
        candidate = self.create_run(
            version["id"],
            name="fixed-agent",
            adapter={"type": "replay", "profile": "fixed"},
            metadata=run_metadata,
        )
        if execute:
            baseline = self.start_run(baseline["id"])
            candidate = self.start_run(candidate["id"])
        return {
            "dataset": dataset,
            "dataset_version": version,
            "baseline_run": baseline,
            "candidate_run": candidate,
            "synthetic": True,
            "synthetic_inputs": True,
            "mode": "synthetic_replay",
            "execution": execution,
        }

    def bootstrap_live_demo(self) -> dict[str, Any]:
        """Run the general capability dataset through the in-process Agent.

        The prompts and labels remain synthetic, while answers and traces are
        produced at run time by ``LocalAGISaberAdapter``.  This distinction is
        persisted on the run so later reports cannot confuse it with Replay.
        """

        readiness = self.readiness()
        if not readiness["live"]["ready"]:
            raise ValueError(readiness["live"]["reasons"][0])

        dataset_name = "agent-framework-capability-live-eval"
        dataset = next(
            (item for item in self.store.list_datasets() if item["name"] == dataset_name),
            None,
        )
        if dataset is None:
            dataset = self.create_dataset(
                dataset_name,
                description="通用 Agent 能力合成输入集；由本进程 Local adapter 实时执行",
                domain="agent-framework-quality",
                metadata={
                    "data_policy": "synthetic-evaluation-inputs",
                    "execution_mode": "local_agent",
                },
            )

        source_name = "agent_framework_capability_eval.jsonl"
        final_root = Path(__file__).resolve().parents[2]
        version = self.import_jsonl(
            dataset["id"],
            final_root / "examples" / "evaluation" / source_name,
            metadata={
                "source_dataset": source_name,
                "data_policy": "synthetic-evaluation-inputs",
                "execution_mode": "local_agent",
            },
        )
        llm_mode = str(readiness["live"]["llm_mode"])
        execution = {
            "adapter": "local",
            "execution": "live-agent",
            "execution_mode": "local_agent",
            "data_policy": "synthetic-evaluation-inputs",
            "real_vs_mock": llm_mode,
            "llm_mode": llm_mode,
            "synthetic_execution": False,
            "calls_model": True,
            "calls_tools": True,
            "calls_rag": True,
            "invokes_model": True,
            "invokes_tools": True,
            "invokes_rag": True,
        }
        run = self.create_run(
            version["id"],
            name="local-agent-live",
            adapter={"type": "local", "version": "python"},
            metadata={
                "source": "bundled_dataset_live_execution",
                "source_dataset": source_name,
                "execution_mode": execution["execution_mode"],
                "data_policy": execution["data_policy"],
                "invokes_model": execution["invokes_model"],
                "invokes_tools": execution["invokes_tools"],
                "invokes_rag": execution["invokes_rag"],
                "llm_mode": llm_mode,
                "execution": execution,
            },
        )
        run = self.start_run(run["id"])
        return {
            "dataset": dataset,
            "dataset_version": version,
            "run": run,
            "mode": "local_agent",
            # The execution is live, but this bundled evaluation dataset is
            # synthetic.  Keep both facts explicit instead of overloading one
            # label to mean two different things.
            "synthetic": False,
            "synthetic_inputs": True,
            "live_execution": True,
            "execution": execution,
        }

    def start_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            worker = self._workers.get(run_id)
            if worker is not None and worker.is_alive():
                return self.store.get_run(run_id)
            run = self.store.claim_run_execution(run_id)
            if not run.pop("execution_claimed", False):
                # Another service instance or replica owns the durable claim.
                return run
            cancel_event = threading.Event()
            self._cancel_events[run_id] = cancel_event
            worker = threading.Thread(
                target=self._run_worker,
                args=(run_id, cancel_event),
                name=f"agent-eval-{run_id[:8]}",
                daemon=True,
            )
            self._workers[run_id] = worker
            worker.start()
        return self.store.get_run(run_id)

    def execute_run(self, run_id: str) -> dict[str, Any]:
        """Execute synchronously; primarily useful for tests and CLI demos."""

        run = self.store.claim_run_execution(run_id)
        if not run.pop("execution_claimed", False):
            return run
        cancel_event = threading.Event()
        self._run_worker(run_id, cancel_event)
        return self.store.get_run(run_id)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        # Win the durable state transition before notifying the local worker.
        # Otherwise the worker can observe the event first, commit
        # ``cancelled`` itself, and make the caller's legitimate cancel look
        # like an attempt to mutate a terminal run.
        run = self.store.cancel_run_execution(run_id)
        with self._lock:
            event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()
        return run

    def progress(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        summary = run.get("summary") or {}
        return {
            "run_id": run_id,
            "status": run["status"],
            "total": int(summary.get("total", 0)),
            "completed": int(summary.get("completed", 0)),
            "percent": float(summary.get("progress_percent", 0.0)),
            "passed": int(summary.get("passed", 0)),
            "failed": int(summary.get("failed", 0)),
            "errors": int(summary.get("errors", 0)),
            "hard_gate_failures": int(summary.get("hard_gate_failures", 0)),
            "release_gate_passed": summary.get("release_gate_passed"),
        }

    def summary(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        return {"run": run, "summary": run.get("summary") or {}}

    def compare_runs(self, baseline_run_id: str, candidate_run_id: str) -> dict[str, Any]:
        baseline = self.store.get_run(baseline_run_id)
        candidate = self.store.get_run(candidate_run_id)
        if baseline_run_id == candidate_run_id:
            raise ValueError("baseline and candidate runs must be different")
        if baseline["dataset_version_id"] != candidate["dataset_version_id"]:
            raise ValueError(
                "baseline and candidate must use the same immutable dataset version"
            )
        baseline_version = self.store.get_dataset_version(baseline["dataset_version_id"])
        candidate_version = self.store.get_dataset_version(candidate["dataset_version_id"])
        identity_fields = ("id", "dataset_id", "version", "checksum")
        if any(baseline_version[key] != candidate_version[key] for key in identity_fields):
            raise ValueError("dataset id, version, and checksum must match exactly")
        if baseline["status"] != "completed" or candidate["status"] != "completed":
            raise ValueError("both evaluation runs must be completed before comparison")

        expected_cases = self.store.get_version_cases(baseline_version["id"])
        expected_by_id = {item["id"]: item for item in expected_cases}
        expected_ids = set(expected_by_id)
        baseline_items = self.store.list_case_results(baseline_run_id)
        candidate_items = self.store.list_case_results(candidate_run_id)
        baseline_results = {item["eval_case_id"]: item for item in baseline_items}
        candidate_results = {item["eval_case_id"]: item for item in candidate_items}
        baseline_ids = set(baseline_results)
        candidate_ids = set(candidate_results)
        if baseline_ids != expected_ids or candidate_ids != expected_ids:
            missing_baseline = sorted(expected_ids - baseline_ids)
            missing_candidate = sorted(expected_ids - candidate_ids)
            extra_baseline = sorted(baseline_ids - expected_ids)
            extra_candidate = sorted(candidate_ids - expected_ids)
            raise ValueError(
                "run case sets must exactly cover the immutable dataset version "
                f"(baseline missing={missing_baseline}, extra={extra_baseline}; "
                f"candidate missing={missing_candidate}, extra={extra_candidate})"
            )

        paired_ids = sorted(expected_ids, key=lambda item: expected_by_id[item]["position"])
        fixed: list[str] = []
        regressions: list[str] = []
        unchanged_failures: list[str] = []
        baseline_passed: list[bool] = []
        candidate_passed: list[bool] = []
        baseline_scores: list[float] = []
        candidate_scores: list[float] = []
        for eval_case_id in paired_ids:
            case_id = expected_by_id[eval_case_id]["case_id"]
            before = baseline_results[eval_case_id]
            after = candidate_results[eval_case_id]
            before_passed = before["status"] == "passed"
            after_passed = after["status"] == "passed"
            baseline_passed.append(before_passed)
            candidate_passed.append(after_passed)
            baseline_scores.append(_case_overall_score(before))
            candidate_scores.append(_case_overall_score(after))
            if not before_passed and after_passed:
                fixed.append(case_id)
            elif before_passed and not after_passed:
                regressions.append(case_id)
            elif not before_passed and not after_passed:
                unchanged_failures.append(case_id)
        before_summary = baseline.get("summary") or {}
        after_summary = candidate.get("summary") or {}
        statistics_result = evaluate_paired_statistics(
            baseline_passed,
            candidate_passed,
            baseline_scores,
            candidate_scores,
        )
        baseline_protocol = _evaluation_protocol(baseline)
        candidate_protocol = _evaluation_protocol(candidate)
        protocol_match = baseline_protocol == candidate_protocol
        return {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "dataset": {
                "dataset_id": baseline_version["dataset_id"],
                "dataset_version_id": baseline_version["id"],
                "version": baseline_version["version"],
                "checksum": baseline_version["checksum"],
            },
            "shared_case_count": len(paired_ids),
            "case_count": len(paired_ids),
            "fixed": fixed,
            "regressions": regressions,
            "unchanged_failures": unchanged_failures,
            "pass_rate_delta": round(
                float(after_summary.get("pass_rate", 0.0))
                - float(before_summary.get("pass_rate", 0.0)),
                6,
            ),
            "release_gate_passed": not regressions
            and bool(after_summary.get("release_gate_passed", False)),
            "statistics": statistics_result,
            "protocol": {
                "match": protocol_match,
                "baseline": baseline_protocol,
                "candidate": candidate_protocol,
                "baseline_hash": _canonical_sha256(baseline_protocol),
                "candidate_hash": _canonical_sha256(candidate_protocol),
            },
            "scope": "offline_evaluation",
        }

    def create_strategy_version(
        self,
        name: str,
        manifest: Mapping[str, Any],
        *,
        creator: str,
    ) -> dict[str, Any]:
        """Create an immutable snapshot used to label future offline runs."""

        return self.store.create_strategy_version(name, manifest, creator=creator)

    def get_strategy_version(self, strategy_version_id: str) -> dict[str, Any]:
        return self.store.get_strategy_version(strategy_version_id)

    def list_strategy_versions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_strategy_versions(limit=limit)

    def create_promotion_proposal(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        *,
        creator: str,
    ) -> dict[str, Any]:
        """Create a human-review recommendation from paired offline evidence.

        No state is activated here.  Insufficient statistics, a failed run
        gate, any S0/S1 failure, or an evaluation-protocol mismatch makes the
        proposal terminally ``blocked``.
        """

        evidence = self._build_promotion_evidence(
            baseline_run_id, candidate_run_id
        )
        baseline = evidence["baseline"]
        candidate = evidence["candidate"]
        strategy_version_id = candidate.get("strategy_version_id")
        comparison = evidence["comparison"]
        statistics_result = evidence["statistics"]
        gate = evidence["release_gate"]
        safety = evidence["safety"]
        blocked_reasons = evidence["blocked_reasons"]
        status = "blocked" if blocked_reasons else "proposed"
        result = self.store.create_promotion_proposal(
            baseline_run_id,
            candidate_run_id,
            candidate_strategy_version_id=str(strategy_version_id),
            status=status,
            comparison=comparison,
            statistics=statistics_result,
            release_gate=gate,
            safety=safety,
            blocked_reasons=blocked_reasons,
            created_by=creator,
        )
        # ``create`` is content-idempotent.  If it returned an existing
        # proposal, do not hand stale evidence back merely because this call
        # happened to use the same source runs.
        self._assert_promotion_evidence_current(result)
        result["baseline_strategy_version_id"] = baseline.get("strategy_version_id")
        return result

    def get_promotion_proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.get_promotion_proposal(proposal_id)
        self._assert_promotion_evidence_current(proposal)
        return proposal

    def get_verified_promotion_evidence(self, proposal_id: str) -> dict[str, Any]:
        """Return deployment evidence only after rebuilding its run snapshots."""

        proposal = self.get_promotion_proposal(proposal_id)
        strategy = self.get_strategy_version(
            str(proposal["candidate_strategy_version_id"])
        )
        source = proposal.get("comparison", {}).get("source_evidence", {})
        return {
            "proposal": proposal,
            "strategy": strategy,
            "source_evidence_checksum": source.get("checksum"),
        }

    def list_promotion_proposals(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.store.list_promotion_proposals(status=status, limit=limit)

    def review_promotion_proposal(
        self,
        proposal_id: str,
        *,
        decision: str,
        reviewer: str,
        note: str = "",
    ) -> dict[str, Any]:
        proposal = self.store.get_promotion_proposal(proposal_id)
        self._assert_promotion_evidence_current(proposal)
        result = self.store.review_promotion_proposal(
            proposal_id,
            decision=decision,
            reviewer=reviewer,
            note=note,
        )
        self._assert_promotion_evidence_current(result)
        return result

    def activate_promotion_proposal(
        self,
        proposal_id: str,
        *,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        proposal = self.store.get_promotion_proposal(proposal_id)
        self._assert_promotion_evidence_current(proposal)
        result = self.store.activate_promotion_proposal(
            proposal_id,
            actor=actor,
            note=note,
        )
        self._assert_promotion_evidence_current(result["proposal"])
        return result

    def _build_promotion_evidence(
        self, baseline_run_id: str, candidate_run_id: str
    ) -> dict[str, Any]:
        """Rebuild every input that justified one offline promotion decision."""

        comparison = self.compare_runs(baseline_run_id, candidate_run_id)
        baseline = self.store.get_run(baseline_run_id)
        candidate = self.store.get_run(candidate_run_id)
        strategy_version_id = candidate.get("strategy_version_id")
        if not strategy_version_id:
            raise ValueError("candidate run must be bound to an immutable strategy version")

        comparison = {
            **comparison,
            "source_evidence": self._promotion_source_evidence(
                baseline_run_id, candidate_run_id
            ),
        }
        candidate_summary = candidate.get("summary") or {}
        gate = {
            "passed": bool(candidate_summary.get("release_gate_passed", False)),
            "thresholds": candidate.get("config", {}).get("release_gate", {}),
            "pass_rate": float(candidate_summary.get("pass_rate", 0.0)),
            "error_rate": float(candidate_summary.get("error_rate", 0.0)),
            "p95_latency_ms": float(candidate_summary.get("p95_latency_ms", 0.0)),
        }
        safety_failures = _s0_s1_failures(
            self.store.list_case_results(candidate_run_id)
        )
        safety = {
            "passed": not safety_failures
            and int(candidate_summary.get("hard_gate_failures", 0)) == 0,
            "hard_gate_failures": int(candidate_summary.get("hard_gate_failures", 0)),
            "s0_s1_failures": safety_failures,
        }
        statistics_result = comparison["statistics"]
        blocked_reasons: list[str] = []
        if not statistics_result.get("passed"):
            blocked_reasons.append(
                "statistics_inconclusive"
                if statistics_result.get("status") == "inconclusive"
                else "statistics_failed"
            )
        if not gate["passed"]:
            blocked_reasons.append("release_gate_failed")
        if not safety["passed"]:
            blocked_reasons.append("s0_s1_safety_failure")
        if not comparison.get("protocol", {}).get("match", False):
            blocked_reasons.append("protocol_mismatch")
        if comparison.get("regressions"):
            blocked_reasons.append("case_regressions")
        return {
            "baseline": baseline,
            "candidate": candidate,
            "comparison": comparison,
            "statistics": statistics_result,
            "release_gate": gate,
            "safety": safety,
            "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
        }

    def _promotion_source_evidence(
        self, baseline_run_id: str, candidate_run_id: str
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "promotion-source-evidence-v1",
            "baseline": self._promotion_run_evidence(baseline_run_id),
            "candidate": self._promotion_run_evidence(candidate_run_id),
        }
        return {**payload, "checksum": _canonical_sha256(payload)}

    def _promotion_run_evidence(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run.get("status") != "completed":
            raise ValueError("promotion source evaluation runs must remain completed")
        version = self.store.get_dataset_version(str(run["dataset_version_id"]))
        cases = self.store.get_version_cases(str(version["id"]))
        results = self.store.list_case_results(run_id, limit=100000)
        expected_ids = {str(item["id"]) for item in cases}
        result_ids = {str(item["eval_case_id"]) for item in results}
        if expected_ids != result_ids or len(results) != len(cases):
            raise ValueError("promotion source run no longer has a complete case result set")
        strategy = None
        if run.get("strategy_version_id"):
            raw_strategy = self.store.get_strategy_version(
                str(run["strategy_version_id"])
            )
            strategy = {
                key: raw_strategy.get(key)
                for key in (
                    "id",
                    "manifest",
                    "manifest_canonical_json",
                    "manifest_checksum",
                    "source",
                )
            }
        snapshot = {
            "run": {
                key: run.get(key)
                for key in (
                    "id",
                    "dataset_version_id",
                    "strategy_version_id",
                    "status",
                    "config",
                    "summary",
                    "metadata",
                )
            },
            "dataset_version": {
                key: version.get(key)
                for key in ("id", "dataset_id", "version", "checksum", "metadata")
            },
            "strategy_version": strategy,
            "cases": [
                {
                    key: item.get(key)
                    for key in ("id", "case_id", "position", "payload")
                }
                for item in sorted(
                    cases, key=lambda value: (int(value.get("position") or 0), str(value.get("id") or ""))
                )
            ],
            "case_results": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "eval_case_id",
                        "case_id",
                        "status",
                        "output",
                        "metrics",
                        "trace",
                        "error",
                    )
                }
                for item in sorted(
                    results,
                    key=lambda value: (
                        str(value.get("eval_case_id") or ""),
                        str(value.get("id") or ""),
                    ),
                )
            ],
        }
        return {
            "run_id": run_id,
            "case_count": len(cases),
            "checksum": _canonical_sha256(snapshot),
        }

    def _assert_promotion_evidence_current(
        self, proposal: Mapping[str, Any]
    ) -> None:
        frozen_payload = {
            key: proposal.get(key)
            for key in (
                "baseline_run_id",
                "candidate_run_id",
                "candidate_strategy_version_id",
                "comparison",
                "statistics",
                "release_gate",
                "safety",
            )
        }
        declared_checksum = str(proposal.get("evidence_checksum") or "")
        if not declared_checksum or _canonical_sha256(frozen_payload) != declared_checksum:
            raise ValueError("promotion proposal evidence checksum mismatch")
        current = self._build_promotion_evidence(
            str(proposal.get("baseline_run_id") or ""),
            str(proposal.get("candidate_run_id") or ""),
        )
        if str(current["candidate"].get("strategy_version_id") or "") != str(
            proposal.get("candidate_strategy_version_id") or ""
        ):
            raise ValueError("promotion candidate strategy binding changed")
        comparisons = {
            "comparison": current["comparison"],
            "statistics": current["statistics"],
            "release_gate": current["release_gate"],
            "safety": current["safety"],
            "blocked_reasons": current["blocked_reasons"],
        }
        for field, value in comparisons.items():
            if proposal.get(field) != value:
                raise ValueError(
                    f"promotion source evidence changed after proposal creation: {field}"
                )
        self._assert_promotion_lifecycle_current(proposal)

    def _assert_promotion_lifecycle_current(
        self, proposal: Mapping[str, Any]
    ) -> None:
        """Bind mutable proposal state to its independently written audit facts.

        Evidence hashes deliberately do not include lifecycle state: review and
        activation occur after proposal creation.  Consequently, accepting an
        ``approved`` column on its own would let a one-line SQL update bypass
        the second person.  Requiring the exact lifecycle audit shape makes
        status-only changes, deleted events, and actor/detail rewrites fail
        closed at every read/control/deployment boundary.
        """

        proposal_id = str(proposal.get("id") or "")
        strategy_id = str(proposal.get("candidate_strategy_version_id") or "")
        status = str(proposal.get("status") or "")
        if status not in {"proposed", "blocked", "approved", "rejected", "activated"}:
            raise ValueError("promotion proposal lifecycle status is invalid")
        events = self.store.list_strategy_audit_events(
            proposal_id=proposal_id,
            limit=5000,
        )
        supported = {"proposal_created", "approve", "reject", "activate"}
        if any(str(event.get("action") or "") not in supported for event in events):
            raise ValueError("promotion proposal lifecycle audit contains an invalid action")
        grouped = {
            action: [
                event
                for event in events
                if str(event.get("action") or "") == action
            ]
            for action in supported
        }

        def require_single(action: str) -> Mapping[str, Any]:
            matches = grouped[action]
            if len(matches) != 1:
                raise ValueError(
                    f"promotion proposal lifecycle audit requires exactly one {action} event"
                )
            return matches[0]

        def require_none(action: str) -> None:
            if grouped[action]:
                raise ValueError(
                    f"promotion proposal lifecycle audit has an unexpected {action} event"
                )

        def require_event_binding(
            event: Mapping[str, Any],
            *,
            actor: str,
            action: str,
            from_status: str | None = None,
            to_status: str | None = None,
            note: str | None = None,
        ) -> None:
            details = event.get("details")
            if not isinstance(details, Mapping):
                raise ValueError(
                    f"promotion proposal lifecycle audit {action} details are invalid"
                )
            if (
                str(event.get("proposal_id") or "") != proposal_id
                or str(event.get("strategy_version_id") or "") != strategy_id
                or str(event.get("actor") or "") != actor
            ):
                raise ValueError(
                    f"promotion proposal lifecycle audit {action} binding mismatch"
                )
            expected_integrity_key = promotion_audit_integrity_key(
                proposal_id=proposal_id,
                strategy_version_id=strategy_id,
                evidence_checksum=str(proposal.get("evidence_checksum") or ""),
                action=action,
                actor=actor,
                details=details,
            )
            if str(event.get("idempotency_key") or "") != expected_integrity_key:
                raise ValueError(
                    f"promotion proposal lifecycle audit {action} integrity mismatch"
                )
            if from_status is not None and str(details.get("from_status") or "") != from_status:
                raise ValueError(
                    f"promotion proposal lifecycle audit {action} source state mismatch"
                )
            if to_status is not None and str(details.get("to_status") or "") != to_status:
                raise ValueError(
                    f"promotion proposal lifecycle audit {action} target state mismatch"
                )
            if note is not None and str(details.get("note") or "") != note:
                raise ValueError(
                    f"promotion proposal lifecycle audit {action} note mismatch"
                )

        created = require_single("proposal_created")
        created_details = created.get("details")
        initial_status = "blocked" if status == "blocked" else "proposed"
        require_event_binding(
            created,
            actor=str(proposal.get("created_by") or ""),
            action="proposal_created",
        )
        if (
            not isinstance(created_details, Mapping)
            or str(created_details.get("status") or "") != initial_status
            or str(created_details.get("evidence_checksum") or "")
            != str(proposal.get("evidence_checksum") or "")
        ):
            raise ValueError("promotion proposal creation audit does not match proposal")

        reviewed_by = str(proposal.get("reviewed_by") or "")
        reviewed_at = proposal.get("reviewed_at")
        activated_by = str(proposal.get("activated_by") or "")
        activated_at = proposal.get("activated_at")
        if status in {"proposed", "blocked"}:
            require_none("approve")
            require_none("reject")
            require_none("activate")
            if reviewed_by or reviewed_at or activated_by or activated_at:
                raise ValueError("unreviewed promotion proposal has forged lifecycle fields")
            return

        decision = "approve" if status in {"approved", "activated"} else "reject"
        opposite = "reject" if decision == "approve" else "approve"
        review = require_single(decision)
        require_none(opposite)
        if not reviewed_by or not reviewed_at or reviewed_by == str(proposal.get("created_by") or ""):
            raise ValueError("promotion proposal review identity is invalid")
        require_event_binding(
            review,
            actor=reviewed_by,
            action=decision,
            from_status="proposed",
            to_status="approved" if decision == "approve" else "rejected",
            note=str(proposal.get("review_note") or ""),
        )

        if status in {"approved", "rejected"}:
            require_none("activate")
            if activated_by or activated_at:
                raise ValueError("non-activated promotion proposal has forged activation fields")
            return

        activation = require_single("activate")
        if not activated_by or not activated_at:
            raise ValueError("activated promotion proposal is missing activation identity")
        require_event_binding(
            activation,
            actor=activated_by,
            action="activate",
            note=str(proposal.get("activation_note") or ""),
        )
        activation_details = activation.get("details")
        if str(activation_details.get("current_strategy_version_id") or "") != strategy_id:
            raise ValueError("promotion proposal activation audit strategy mismatch")

    def get_current_strategy(self) -> dict[str, Any]:
        return self.store.get_current_strategy()

    def rollback_strategy(
        self,
        *,
        actor: str,
        idempotency_key: str,
        reason: str = "",
        expected_current_strategy_version_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store.rollback_strategy(
            actor=actor,
            idempotency_key=idempotency_key,
            reason=reason,
            expected_current_strategy_version_id=expected_current_strategy_version_id,
        )

    # -- controlled, human-reviewed strategy hypotheses ------------------

    def create_evolution_suggestion(
        self,
        source_run_id: str,
        *,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a deterministic RAG-parameter hypothesis from one completed run.

        This method intentionally creates neither a StrategyVersion nor a P2
        promotion proposal.  Synthetic and Replay evidence remains useful for
        constructing hypotheses, but the returned truth label prevents it from
        being presented as a measured improvement.
        """

        run, version, cases, results, evidence = self._evolution_evidence(source_run_id)
        current_manifest: Mapping[str, Any] | None = None
        strategy_id = run.get("strategy_version_id")
        if strategy_id:
            strategy = self.store.get_strategy_version(str(strategy_id))
            manifest = strategy.get("manifest")
            if isinstance(manifest, Mapping):
                current_manifest = manifest
        hypothesis = derive_rag_suggestion(
            evidence=evidence,
            cases=cases,
            results=results,
            current_manifest=current_manifest,
        )
        return self.store.create_evolution_suggestion(
            source_run_id,
            dataset_version_id=version["id"],
            source_strategy_version_id=(str(strategy_id) if strategy_id else None),
            rule_version=EVOLUTION_RULE_VERSION,
            evidence=evidence,
            suggestion_manifest=hypothesis["manifest"],
            rationale=hypothesis["rationale"],
            limitations=hypothesis["limitations"],
            truth=hypothesis["truth"],
            diagnostics=hypothesis["diagnostics"],
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def get_evolution_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        return self.store.get_evolution_suggestion(suggestion_id)

    def list_evolution_suggestions(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.store.list_evolution_suggestions(status=status, limit=limit)

    def review_evolution_suggestion(
        self,
        suggestion_id: str,
        *,
        decision: str,
        reviewer: str,
        note: str,
        expected_generation: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        suggestion = self.store.get_evolution_suggestion(suggestion_id)
        self._assert_evolution_evidence_current(suggestion)
        return self.store.review_evolution_suggestion(
            suggestion_id,
            decision=decision,
            reviewer=reviewer,
            note=note,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
        )

    def materialize_evolution_suggestion(
        self,
        suggestion_id: str,
        *,
        name: str | None,
        actor: str,
        expected_generation: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        suggestion = self.store.get_evolution_suggestion(suggestion_id)
        self._assert_evolution_evidence_current(suggestion)
        strategy_name = str(name or "").strip() or (
            f"受控 RAG 假设 · {suggestion['source_run_id'][:8]}"
        )
        return self.store.materialize_evolution_suggestion(
            suggestion_id,
            name=strategy_name,
            actor=actor,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
        )

    def list_evolution_audit_events(
        self,
        *,
        suggestion_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return self.store.list_evolution_audit_events(
            suggestion_id=suggestion_id,
            limit=limit,
        )

    def _evolution_evidence(
        self, source_run_id: str
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]:
        run = self.store.get_run(source_run_id)
        if run["status"] != "completed":
            raise ValueError("source evaluation run must be completed")
        version = self.store.get_dataset_version(run["dataset_version_id"])
        strategy: dict[str, Any] | None = None
        if run.get("strategy_version_id"):
            strategy = self.store.get_strategy_version(
                str(run["strategy_version_id"])
            )
        cases = self.store.get_version_cases(version["id"])
        results = self.store.list_case_results(run["id"])
        badcases = self.store.list_badcases(run_id=run["id"], limit=10000)
        evidence = build_evidence_snapshot(
            run=run,
            dataset_version=version,
            strategy_version=strategy,
            cases=cases,
            results=results,
            badcases=badcases,
        )
        return run, version, cases, results, evidence

    def _assert_evolution_evidence_current(
        self, suggestion: Mapping[str, Any]
    ) -> None:
        _, version, _, _, current = self._evolution_evidence(
            str(suggestion.get("source_run_id") or "")
        )
        if str(version["id"]) != str(suggestion.get("dataset_version_id") or ""):
            raise ValueError("evolution suggestion dataset evidence no longer matches")
        expected_checksum = str(suggestion.get("evidence_checksum") or "")
        current_checksum = evolution_checksum(current)
        if not expected_checksum or current_checksum != expected_checksum:
            raise ValueError(
                "evolution source evidence changed; generate a new suggestion before review"
            )

    def verify_badcase(
        self,
        badcase_id: str,
        *,
        candidate_run_id: str,
        reviewer: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Close a Badcase only when the same case passes a candidate run."""

        badcase = self.store.get_badcase(badcase_id)
        candidate = self.store.get_run(candidate_run_id)
        original_run = self.store.get_run(badcase["run_id"])
        if candidate["status"] != "completed":
            raise ValueError("candidate run must be completed before Badcase verification")
        if candidate["dataset_version_id"] != original_run["dataset_version_id"]:
            raise ValueError(
                "candidate run must use the same immutable dataset version as the Badcase"
            )
        result = next(
            (
                item
                for item in self.store.list_case_results(candidate_run_id)
                if item["eval_case_id"] == badcase["eval_case_id"]
            ),
            None,
        )
        if result is None:
            raise ValueError("candidate run does not contain the Badcase evaluation case")
        passed = result["status"] == "passed"
        updated = self.store.apply_badcase_verification(
            badcase_id,
            passed=passed,
            resolution={
                "candidate_run_id": candidate_run_id,
                "candidate_status": candidate["status"],
                "candidate_case_run_id": result["id"],
                "passed": passed,
                "reviewer": reviewer,
                "note": note,
            },
        )
        annotation = self.store.create_annotation(
            annotator=reviewer,
            badcase_id=badcase_id,
            case_run_id=result["id"],
            annotation={"type": "regression_verification", "passed": passed, "note": note},
        )
        return {"badcase": updated, "candidate_result": result, "annotation": annotation}

    def metrics_summary(self) -> dict[str, Any]:
        runs = self.store.list_runs(limit=1000)
        badcases = self.store.list_badcases(limit=10000)
        latest_completed = next((item for item in runs if item["status"] == "completed"), None)
        return {
            "run_count": len(runs),
            "runs_by_status": dict(Counter(item["status"] for item in runs)),
            "badcase_count": len(badcases),
            "badcases_by_status": dict(Counter(item["status"] for item in badcases)),
            "badcases_by_category": dict(Counter(item["category"] for item in badcases)),
            "latest_completed_run": latest_completed,
        }

    def markdown_report(self, run_id: str) -> str:
        return reporting.markdown_report(
            self.store,
            run_id,
            metric_names=METRIC_DISPLAY_NAMES,
            badcase_names=BADCASE_CATEGORY_DISPLAY_NAMES,
        )

    def csv_report(self, run_id: str) -> str:
        return reporting.csv_report(self.store, run_id)

    def prometheus_metrics(self) -> str:
        return reporting.prometheus_metrics(self.store)

    def _run_worker(self, run_id: str, cancel_event: threading.Event) -> None:
        run = self.store.get_run(run_id)
        cases = self.store.get_version_cases(run["dataset_version_id"])
        adapter_config = self._adapter_config_for_run(run)
        try:
            adapter = build_adapter(adapter_config, self.local_agent)
        except Exception as exc:
            self.store.update_run(
                run_id,
                status="failed",
                summary={"total": len(cases), "completed": 0, "error": str(exc)},
            )
            return

        started = time.perf_counter()
        summary_state: dict[str, Any] = {
            "total": len(cases),
            "completed": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "hard_gate_failures": 0,
            "progress_percent": 0.0,
            "adapter": {"name": adapter.name, "version": adapter.version},
        }
        self.store.update_run(run_id, summary=summary_state)
        latencies: list[float] = []
        reports: list[EvaluationReport] = []
        try:
            for case_record in cases:
                if cancel_event.is_set():
                    self.store.update_run(run_id, status="cancelled", summary=summary_state)
                    return
                case = EvalCase.model_validate(case_record["payload"])
                case_started = time.perf_counter()
                try:
                    output = adapter.execute(case)
                    latency_ms = (time.perf_counter() - case_started) * 1000
                    report = evaluate(case, output)
                    status = "passed" if report.passed else "failed"
                    category, severity = _classify_badcase(report)
                    self.store.save_case_result(
                        run_id,
                        case.case_id,
                        status=status,
                        output={
                            "agent_output": output.model_dump(mode="json"),
                            "latency_ms": round(latency_ms, 3),
                        },
                        metrics=report.model_dump(mode="json"),
                        trace=[event.model_dump(mode="json") for event in output.trace],
                        passed=report.passed,
                        badcase_category=category,
                        badcase_severity=severity,
                        badcase_details={
                            "failed_metrics": report.failed_metrics,
                            "hard_gate_failed": report.hard_gate_failed,
                        },
                    )
                    latencies.append(latency_ms)
                    reports.append(report)
                    summary_state[status] += 1
                    if report.hard_gate_failed:
                        summary_state["hard_gate_failures"] += 1
                except Exception as exc:
                    latency_ms = (time.perf_counter() - case_started) * 1000
                    self.store.save_case_result(
                        run_id,
                        case.case_id,
                        status="error",
                        error={"type": type(exc).__name__, "message": str(exc)},
                        passed=False,
                        badcase_category="INFRA",
                        badcase_severity="high",
                        badcase_details={"stage": "execute_or_evaluate"},
                    )
                    latencies.append(latency_ms)
                    summary_state["errors"] += 1
                summary_state["completed"] += 1
                summary_state["progress_percent"] = round(
                    summary_state["completed"] / max(1, summary_state["total"]) * 100,
                    2,
                )
                self.store.update_run(run_id, summary=summary_state)

            final_summary = _aggregate_summary(
                summary_state,
                reports,
                latencies,
                run.get("config", {}).get("release_gate", {}),
                wall_time_ms=(time.perf_counter() - started) * 1000,
            )
            self.store.update_run(run_id, status="completed", summary=final_summary)
        except Exception as exc:
            summary_state["fatal_error"] = f"{type(exc).__name__}: {exc}"
            self.store.update_run(run_id, status="failed", summary=summary_state)
        finally:
            with self._lock:
                self._workers.pop(run_id, None)
                self._cancel_events.pop(run_id, None)
                self._runtime_adapter_configs().pop(run_id, None)

    def _adapter_config_for_run(self, run: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            runtime = self._runtime_adapter_configs().get(str(run["id"]))
        if runtime is not None:
            return runtime
        config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
        adapter = config.get("adapter") if isinstance(config, Mapping) else None
        return adapter if isinstance(adapter, Mapping) else {"type": "replay", "profile": "baseline"}

    def _runtime_adapter_configs(self) -> dict[str, dict[str, Any]]:
        configs = getattr(self, "_runtime_configs", None)
        if configs is None:
            configs = {}
            self._runtime_configs = configs
        return configs


def _dependency_status(agent: Any | None) -> dict[str, str]:
    """Return a side-effect-free dependency snapshot for evaluation UIs."""

    if agent is None:
        return {
            "evaluation_store": "connected",
            "llm": "unavailable",
            "embedding": "unavailable",
            "postgresql": "unavailable",
            "milvus": "unavailable",
            "elasticsearch": "unavailable",
            "kafka": "unavailable",
        }

    cfg = getattr(agent, "cfg", None)
    inf = getattr(agent, "inf", None)
    ready = getattr(inf, "ready", None)

    def _provider_mode(method_name: str) -> str:
        checker = getattr(cfg, method_name, None)
        try:
            return "real" if bool(checker() if callable(checker) else checker) else "mock"
        except Exception:
            return "mock"

    return {
        "evaluation_store": "connected",
        "llm": _provider_mode("is_real_llm"),
        "embedding": _provider_mode("is_real_embedding"),
        "postgresql": str(getattr(ready, "postgresql", "unavailable") or "unavailable"),
        "milvus": str(getattr(ready, "milvus", "unavailable") or "unavailable"),
        "elasticsearch": str(getattr(ready, "elasticsearch", "unavailable") or "unavailable"),
        "kafka": str(getattr(ready, "kafka", "unavailable") or "unavailable"),
    }


def _classify_badcase(report: EvaluationReport) -> tuple[str, str]:
    failed = [metric for metric in report.metrics if metric.applicable and not metric.passed]
    if not failed:
        return "unclassified", "low"
    if report.hard_gate_failed:
        return "SAFETY", "critical"
    category = BADCASE_CATEGORY_BY_DIMENSION.get(failed[0].dimension.value, "unclassified")
    severity = "high" if any(metric.severity is not None for metric in failed) else "medium"
    return category, severity


def _case_overall_score(result: Mapping[str, Any]) -> float:
    """Return a paired score without dropping errors or missing observations."""

    metrics = result.get("metrics") if isinstance(result, Mapping) else None
    raw = metrics.get("overall_score") if isinstance(metrics, Mapping) else None
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) and 0.0 <= score <= 1.0 else 0.0


def _evaluation_protocol(run: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only execution controls that must match for a fair comparison.

    Adapter profile/version identify the candidate itself and are intentionally
    excluded.  Execution mode, data policy, evaluator configuration, and the
    gate cannot differ unnoticed.
    """

    config = run.get("config") if isinstance(run.get("config"), Mapping) else {}
    adapter = config.get("adapter") if isinstance(config.get("adapter"), Mapping) else {}
    metadata = run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {}
    execution = metadata.get("execution") if isinstance(metadata.get("execution"), Mapping) else {}
    gate = ReleaseGate.model_validate(config.get("release_gate") or {}).model_dump(mode="json")
    return {
        "adapter_type": str(adapter.get("type") or "replay").strip().casefold(),
        "release_gate": gate,
        "evaluator": dict(config.get("evaluator") or {})
        if isinstance(config.get("evaluator"), Mapping) else {},
        "data_source": metadata.get("data_source", execution.get("data_source")),
        "execution_mode": metadata.get("execution_mode", execution.get("execution_mode")),
        "data_policy": metadata.get("data_policy", execution.get("data_policy")),
        "synthetic_execution": metadata.get(
            "synthetic_execution", execution.get("synthetic_execution")
        ),
    }


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _s0_s1_failures(results: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in results:
        metrics = result.get("metrics")
        entries = metrics.get("metrics") if isinstance(metrics, Mapping) else None
        if not isinstance(entries, list):
            continue
        for metric in entries:
            if not isinstance(metric, Mapping) or metric.get("passed") is not False:
                continue
            severity = str(metric.get("severity") or "").upper()
            if severity not in {"S0", "S1"}:
                continue
            failures.append({
                "case_id": str(result.get("case_id") or ""),
                "metric": str(metric.get("name") or "unknown"),
                "severity": severity,
            })
    return failures


def _aggregate_summary(
    state: Mapping[str, Any],
    reports: list[EvaluationReport],
    latencies: list[float],
    gate: Mapping[str, Any],
    *,
    wall_time_ms: float,
) -> dict[str, Any]:
    total = int(state.get("total", 0))
    completed = int(state.get("completed", 0))
    passed = int(state.get("passed", 0))
    errors = int(state.get("errors", 0))
    metric_buckets: dict[str, dict[str, list[float] | int]] = defaultdict(
        lambda: {"scores": [], "passed": 0, "applicable": 0}
    )
    for report in reports:
        for metric in report.metrics:
            if not metric.applicable:
                continue
            bucket = metric_buckets[metric.name]
            bucket["scores"].append(metric.score)  # type: ignore[union-attr]
            bucket["applicable"] = int(bucket["applicable"]) + 1
            if metric.passed:
                bucket["passed"] = int(bucket["passed"]) + 1
    metrics: dict[str, Any] = {}
    for name, bucket in metric_buckets.items():
        applicable = int(bucket["applicable"])
        scores = list(bucket["scores"])  # type: ignore[arg-type]
        metrics[name] = {
            "applicable": applicable,
            "passed": int(bucket["passed"]),
            "pass_rate": int(bucket["passed"]) / applicable if applicable else 1.0,
            "average_score": statistics.fmean(scores) if scores else 1.0,
        }

    pass_rate = passed / total if total else 0.0
    error_rate = errors / total if total else 0.0
    p95_latency = _percentile(latencies, 0.95)
    gate_contract = ReleaseGate.model_validate(gate or {})
    minimum_pass_rate = gate_contract.minimum_pass_rate
    maximum_error_rate = gate_contract.maximum_error_rate
    maximum_p95 = gate_contract.maximum_p95_latency_ms
    hard_gate_failures = int(state.get("hard_gate_failures", 0))
    release_gate_passed = (
        completed == total
        and hard_gate_failures == 0
        and pass_rate >= minimum_pass_rate
        and error_rate <= maximum_error_rate
        and p95_latency <= maximum_p95
    )
    return {
        **dict(state),
        "progress_percent": 100.0 if completed == total else round(completed / max(1, total) * 100, 2),
        "pass_rate": round(pass_rate, 6),
        "error_rate": round(error_rate, 6),
        "average_score": round(
            statistics.fmean(report.overall_score for report in reports) if reports else 0.0,
            6,
        ),
        "p50_latency_ms": round(_percentile(latencies, 0.50), 3),
        "p95_latency_ms": round(p95_latency, 3),
        "wall_time_ms": round(wall_time_ms, 3),
        "metrics": metrics,
        "release_gate": gate_contract.model_dump(mode="json"),
        "release_gate_passed": release_gate_passed,
    }


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(1.0, max(0.0, quantile))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


class EvaluationServiceRegistry:
    """One isolated evaluation database per tenant, with actor-bound runners.

    Evaluation datasets often contain sensitive prompts and Trace payloads.
    Database-per-tenant keeps those assets isolated while allowing a creator
    and a different approver in the same tenant to operate on the same
    immutable proposal.  Each actor gets a service instance backed by that
    shared tenant database so Local-Agent executions still use that actor's
    own Agent/memory rather than another user's runtime.
    """

    def __init__(self, local_agent_factory=None, root: str | Path | None = None):
        default_root = Path(__file__).resolve().parents[2] / "runtime" / "evaluation-tenants"
        self.root = Path(root or os.getenv("AGI_EVAL_TENANT_DIR") or default_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_url_template = str(
            os.getenv("AGI_EVAL_TENANT_DATABASE_URL_TEMPLATE", "") or ""
        ).strip()
        if self.database_url_template:
            if "{tenant_digest}" not in self.database_url_template:
                raise ValueError(
                    "AGI_EVAL_TENANT_DATABASE_URL_TEMPLATE must contain "
                    "{tenant_digest} to preserve tenant isolation"
                )
            probe_url = self.database_url_template.replace(
                "{tenant_digest}", "0" * 24
            )
            if "{" in probe_url or "}" in probe_url or "://" not in probe_url:
                raise ValueError("invalid tenant evaluation database URL template")
            self.storage_backend = probe_url.split("://", 1)[0].split("+", 1)[0]
        else:
            self.storage_backend = "sqlite"
        # Local SQLite is useful for one-process demos, but two reviewers on
        # different replicas would see different files.  Only a shared,
        # non-SQLite tenant URL may contribute evidence to a production claim.
        self.production_shared_ready = bool(
            self.database_url_template and self.storage_backend != "sqlite"
        )
        self.local_agent_factory = local_agent_factory
        self._services: dict[str, EvaluationService] = {}
        self._lock = threading.RLock()

    def get(
        self,
        tenant_id: str,
        execution_user_id: str | None = None,
    ) -> EvaluationService:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("authenticated tenant is required")
        actor = str(execution_user_id or "").strip()
        cache_key = f"{tenant}\x00{actor}"
        with self._lock:
            service = self._services.get(cache_key)
            if service is None:
                digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:24]
                database_url = (
                    self.database_url_template.replace("{tenant_digest}", digest)
                    if self.database_url_template
                    else f"sqlite+pysqlite:///{(self.root / f'eval-{digest}.db').as_posix()}"
                )
                local_agent = (
                    self.local_agent_factory(actor)
                    if self.local_agent_factory and actor
                    else None
                )
                service = EvaluationService(EvaluationStore(database_url), local_agent=local_agent)
                self._services[cache_key] = service
            return service

    def close(self) -> None:
        with self._lock:
            services = list(self._services.values())
            self._services.clear()
        for service in services:
            service.close()
