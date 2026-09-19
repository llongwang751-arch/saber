from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from internal.evaluation.adapters import ReplayAgentAdapter
from internal.evaluation.evaluators import evaluate_case
from internal.evaluation.schemas import EvalCase, MetricDimension, TraceEventType


DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "evaluation"
    / "agent_framework_capability_eval.jsonl"
)


def _load_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    with DATASET_PATH.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            try:
                cases.append(EvalCase.model_validate(payload))
            except Exception as exc:  # pragma: no cover - improves fixture diagnostics
                raise AssertionError(f"invalid case at JSONL line {line_number}: {exc}") from exc
    return cases


CASES = _load_cases()


def _metric(report, name: str):
    return next(metric for metric in report.metrics if metric.name == name)


def test_dataset_is_nonempty_unique_synthetic_and_offline_replayable():
    assert DATASET_PATH.is_file()
    assert len(CASES) >= 16
    assert len({case.case_id for case in CASES}) == len(CASES)
    assert all(case.turns and any(turn.role == "user" for turn in case.turns) for case in CASES)
    assert all(case.metadata.get("synthetic") is True for case in CASES)
    assert all(set(case.metadata.get("outputs", {})) == {"baseline", "fixed"} for case in CASES)


def test_dataset_covers_the_agent_framework_capability_matrix():
    capabilities = {
        capability
        for case in CASES
        for capability in case.metadata.get("capabilities", [])
    }
    required = {
        "intent_classification",
        "multi_intent_detection",
        "slot_extraction",
        "slot_correction",
        "tool_selection",
        "tool_arguments",
        "tool_abstention",
        "task_planning",
        "dependency_order",
        "parallel_execution",
        "failure_aware_replan",
        "multi_step_trace",
        "trace_order",
        "tool_pairing",
        "refusal",
        "secret_redaction",
        "prompt_injection_defense",
        "timeout_handling",
        "malformed_result_handling",
        "unknown_tool_handling",
        "partial_failure",
        "retry_policy",
    }
    assert required <= capabilities

    categories = Counter(str(case.metadata.get("category")) for case in CASES)
    assert {"intent", "slot", "tool", "planning", "trace", "safety", "fallback"} <= set(categories)
    assert categories["safety"] >= 3
    assert categories["fallback"] >= 4


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_fixed_replay_is_a_complete_passing_oracle(case: EvalCase):
    output = ReplayAgentAdapter(profile="fixed").execute(case)
    report = evaluate_case(case, output)

    assert report.passed is True, {
        "case_id": case.case_id,
        "score": report.overall_score,
        "failed_metrics": report.failed_metrics,
        "evidence": {
            metric.name: metric.evidence for metric in report.metrics if not metric.passed
        },
    }
    assert report.hard_gate_failed is False
    assert report.overall_score == 1.0


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_baseline_replay_exposes_a_deterministic_badcase(case: EvalCase):
    output = ReplayAgentAdapter(profile="baseline").execute(case)
    report = evaluate_case(case, output)

    assert report.passed is False, f"baseline fixture unexpectedly passed: {case.case_id}"
    assert report.failed_metrics


def test_targeted_baseline_failures_are_attributed_to_the_right_dimensions():
    by_id = {case.case_id: case for case in CASES}

    checks = {
        "intent_smalltalk_no_tool": "intent_accuracy",
        "slot_nested_extraction": "required_slot_recall",
        "tool_search_selection": "tool_selection_f1",
        "tool_document_exact_arguments": "tool_argument_accuracy",
        "trace_sequence_integrity": "trace_completeness",
        "fallback_tool_timeout": "fallback_recovery",
        "safety_secret_exfiltration_refusal": "privacy_non_disclosure",
        "safety_rag_prompt_injection": "rag_evidence_f1",
    }
    for case_id, metric_name in checks.items():
        report = evaluate_case(by_id[case_id], ReplayAgentAdapter("baseline").execute(by_id[case_id]))
        metric = _metric(report, metric_name)
        assert metric.applicable is True
        assert metric.passed is False

    secret_report = evaluate_case(
        by_id["safety_secret_exfiltration_refusal"],
        ReplayAgentAdapter("baseline").execute(by_id["safety_secret_exfiltration_refusal"]),
    )
    assert secret_report.hard_gate_failed is True


def test_multistep_cases_have_explicit_plans_and_complete_tool_pairs():
    multistep = [
        case
        for case in CASES
        if "multi_step_trace" in case.metadata.get("capabilities", [])
    ]
    assert len(multistep) >= 2

    for case in multistep:
        output = ReplayAgentAdapter("fixed").execute(case)
        event_types = [event.event_type for event in output.trace]
        assert "plan_created" in event_types
        assert event_types[-1] == TraceEventType.FINAL_RESPONSE.value
        assert [event.sequence for event in output.trace] == list(range(len(output.trace)))
        assert _metric(evaluate_case(case, output), "trace_completeness").passed is True


def test_failure_cases_are_observable_and_do_not_silently_claim_success():
    failure_cases = [
        case
        for case in CASES
        if case.expected.require_fallback
    ]
    assert len(failure_cases) >= 4

    for case in failure_cases:
        output = ReplayAgentAdapter("fixed").execute(case)
        event_types = {event.event_type for event in output.trace}
        assert output.fallback is True
        assert output.error
        assert TraceEventType.FALLBACK.value in event_types
        assert TraceEventType.FINAL_RESPONSE.value in event_types
        assert _metric(evaluate_case(case, output), "fallback_recovery").passed is True


def test_dataset_activates_every_agent_framework_evaluator_dimension():
    observed = set()
    for case in CASES:
        report = evaluate_case(case, ReplayAgentAdapter("fixed").execute(case))
        observed.update(metric.dimension for metric in report.metrics if metric.applicable)

    assert observed == {
        MetricDimension.INTENT,
        MetricDimension.INFORMATION,
        MetricDimension.TOOL,
        MetricDimension.RESPONSE,
        MetricDimension.RAG,
        MetricDimension.FALLBACK,
        MetricDimension.SAFETY,
        MetricDimension.TRACE,
        MetricDimension.HARNESS,
    }
