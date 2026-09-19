"""Smoke tests for the one-command Agent/RAG/memory benchmark runner."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.run_benchmark_suite import (
    RAG_QUALITY_FIXTURE,
    load_cases,
    rag_quality_summary,
    replay_profiles,
    run_component_suite,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_DATASET = PROJECT_ROOT / "examples" / "evaluation" / "agent_framework_capability_eval.jsonl"
MEMORY_DATASET = PROJECT_ROOT / "examples" / "evaluation" / "memory_behavior_eval.jsonl"


def test_agent_benchmark_has_comparable_replay_profiles():
    cases = load_cases(AGENT_DATASET)

    assert len(cases) == 19
    assert replay_profiles(cases) == ["baseline", "fixed"]


def test_memory_benchmark_has_comparable_replay_profiles():
    cases = load_cases(MEMORY_DATASET)

    assert len(cases) == 6
    assert replay_profiles(cases) == ["baseline", "fixed"]


def test_rag_golden_set_summary_uses_production_metric_implementation():
    summary = rag_quality_summary(RAG_QUALITY_FIXTURE)

    assert summary["case_count"] == 9
    assert summary["profiles"]["baseline"]["recall_at_3"] == 0.875
    assert summary["profiles"]["baseline"]["mrr_at_3"] == 0.6875
    assert summary["profiles"]["baseline"]["ndcg_at_3"] == pytest.approx(0.721043, abs=1e-6)
    assert all(value == 1.0 for value in summary["profiles"]["candidate"].values())
    assert summary["release_gate_passed"] is True


def test_component_suite_returns_junit_counts(tmp_path):
    test_path = tmp_path / "test_component_smoke.py"
    test_path.write_text("def test_component_smoke():\n    assert 2 + 2 == 4\n", encoding="utf-8")

    result = run_component_suite("组件冒烟", test_path, tmp_path)

    assert result["passed"] is True
    assert result["tests"] == 1
    assert result["failures"] == 0
    assert Path(result["junit_xml"]).exists()


def test_benchmark_runner_emits_machine_and_human_reports(tmp_path):
    output_dir = tmp_path / "reports"
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_benchmark_suite.py"),
            "--dataset",
            str(AGENT_DATASET),
            "--database",
            str(tmp_path / "benchmark.db"),
            "--output-dir",
            str(output_dir),
            "--skip-component-tests",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads((output_dir / "benchmark-results.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "benchmark-report.md").read_text(encoding="utf-8")
    assert result["passed"] is True
    assert result["replay_case_count"] == 19
    assert result["datasets"][0]["runs"]["baseline"]["pass_rate"] == 0.0
    assert result["datasets"][0]["runs"]["fixed"]["pass_rate"] == 1.0
    assert result["datasets"][0]["comparison"]["regressions"] == []
    assert "总体门禁：通过" in markdown
    assert "Replay Benchmark" in markdown
