"""Run every replay-capable Agent benchmark and emit one comparison report.

This is intentionally offline and deterministic: it validates benchmark
assets and scoring/release-gate logic without spending model tokens.  Live
model runs use the same datasets through the local or HTTP adapter.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from internal.evaluation.evaluators import evaluate_case  # noqa: E402
from internal.evaluation.schemas import AgentOutput, EvalCase, Expected, TraceEvent  # noqa: E402
from internal.evaluation.service import EvaluationService  # noqa: E402
from internal.evaluation.store import EvaluationStore  # noqa: E402


COMPONENT_SUITES = (
    ("RAG 质量", PROJECT_ROOT / "tests" / "test_rag_quality_eval_dataset.py"),
    ("记忆与 Harness", PROJECT_ROOT / "tests" / "test_memory_harness_eval_dataset.py"),
)
RAG_QUALITY_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "rag_quality_eval_v1.jsonl"
PROFILE_NAMES = {"baseline": "基线版", "fixed": "修复版", "candidate": "候选版"}


def load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            cases.append(EvalCase.model_validate_json(raw))
        except Exception as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
    if not cases:
        raise ValueError(f"benchmark has no cases: {path}")
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"benchmark contains duplicate case_id values: {path}")
    return cases


def replay_profiles(cases: list[EvalCase]) -> list[str]:
    common: set[str] | None = None
    for case in cases:
        outputs = case.metadata.get("outputs") if isinstance(case.metadata, dict) else None
        profiles = set(outputs) if isinstance(outputs, dict) else set()
        common = profiles if common is None else common.intersection(profiles)
    preferred = [name for name in ("baseline", "fixed", "candidate") if name in (common or set())]
    return preferred + sorted((common or set()).difference(preferred))


def metric_summary(run: dict[str, Any]) -> dict[str, Any]:
    summary = run.get("summary") or {}
    return {
        "run_id": run["id"],
        "status": run["status"],
        "total": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "pass_rate": summary.get("pass_rate", 0.0),
        "p95_latency_ms": summary.get("p95_latency_ms", 0.0),
        "hard_gate_failures": summary.get("hard_gate_failures", 0),
        "release_gate_passed": summary.get("release_gate_passed", False),
        "metrics": summary.get("metrics") or {},
    }


def _junit_counts(path: Path) -> dict[str, int | float]:
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {
        "tests": sum(int(item.attrib.get("tests", 0)) for item in suites),
        "failures": sum(int(item.attrib.get("failures", 0)) for item in suites),
        "errors": sum(int(item.attrib.get("errors", 0)) for item in suites),
        "skipped": sum(int(item.attrib.get("skipped", 0)) for item in suites),
        "duration_seconds": round(sum(float(item.attrib.get("time", 0.0)) for item in suites), 3),
    }


def run_component_suite(name: str, test_path: Path, output_dir: Path) -> dict[str, Any]:
    """Run one deterministic component benchmark and retain machine-readable evidence."""

    junit_path = output_dir / f"component-{test_path.stem}.xml"
    pytest_temp = output_dir / f"pytest-{test_path.stem}-{uuid.uuid4().hex[:10]}"
    started = time.perf_counter()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_path),
            "-q",
            "--junitxml",
            str(junit_path),
            "--basetemp",
            str(pytest_temp),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    counts: dict[str, Any] = {
        "tests": 0,
        "failures": 0,
        "errors": 1,
        "skipped": 0,
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
    parse_error = ""
    if junit_path.exists():
        try:
            counts.update(_junit_counts(junit_path))
        except (ElementTree.ParseError, OSError, ValueError) as exc:
            parse_error = str(exc)
    output_lines = (completed.stdout + "\n" + completed.stderr).strip().splitlines()
    return {
        "name": name,
        "test_path": str(test_path),
        "passed": completed.returncode == 0,
        "exit_code": completed.returncode,
        **counts,
        "junit_xml": str(junit_path),
        "parse_error": parse_error,
        "output_tail": output_lines[-30:],
    }


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _claim_attribution(row: dict[str, Any], profile: str) -> tuple[float, float] | None:
    expected = {
        str(claim["claim_id"]): set(claim.get("evidence_ids") or [])
        for claim in row["oracle"].get("claims") or []
    }
    if not expected:
        return None
    actual = {
        str(claim["claim_id"]): set(claim.get("evidence_ids") or [])
        for claim in row["runs"][profile].get("answer_claims") or []
    }
    coverage = len(expected.keys() & actual.keys()) / len(expected)
    cited = sum(len(values) for values in actual.values())
    supported = sum(
        len(values.intersection(expected.get(claim_id, set())))
        for claim_id, values in actual.items()
    )
    return coverage, supported / cited if cited else 0.0


def rag_quality_summary(path: Path, *, k: int = 3) -> dict[str, Any]:
    """Score the portable RAG golden set with the production metric functions."""

    rows = [
        json.loads(raw)
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]
    if not rows:
        raise ValueError(f"RAG benchmark has no cases: {path}")
    common_profiles = set(rows[0].get("runs") or {})
    for row in rows[1:]:
        common_profiles.intersection_update(row.get("runs") or {})
    ordered_profiles = [
        profile for profile in ("baseline", "fixed", "candidate") if profile in common_profiles
    ]
    ordered_profiles.extend(sorted(common_profiles.difference(ordered_profiles)))
    profiles: dict[str, dict[str, Any]] = {}
    for profile in ordered_profiles:
        retrieval_values: dict[str, list[float]] = {
            "recall_at_3": [],
            "mrr_at_3": [],
            "ndcg_at_3": [],
        }
        abstention_values: list[float] = []
        claim_coverage: list[float] = []
        evidence_precision: list[float] = []
        for row in rows:
            oracle = row["oracle"]
            grades = {
                str(chunk_id): int(grade)
                for chunk_id, grade in (oracle.get("relevance_grades") or {}).items()
                if int(grade) > 0
            }
            relevant_ids = list(oracle.get("relevant_chunk_ids") or grades)
            run = row["runs"][profile]
            trace = [TraceEvent(sequence=0, event_type="final_response")]
            if relevant_ids:
                trace.insert(0, TraceEvent(sequence=0, event_type="retrieval"))
                trace[1] = TraceEvent(sequence=1, event_type="final_response")
            case = EvalCase(
                case_id=str(row["case_id"]),
                scenario=str(row.get("query") or row["case_id"]),
                expected=Expected(
                    evidence_ids=relevant_ids,
                    evidence_relevance=grades,
                    retrieval_k=k,
                    min_recall_at_k=0.0,
                    min_mrr=0.0,
                    min_ndcg_at_k=0.0,
                    allow_extra_evidence=True,
                    answerable=bool(oracle["answerable"]),
                ),
            )
            output = AgentOutput(
                content="" if run.get("abstained") else "synthetic benchmark answer",
                evidence_ids=[str(value) for value in run.get("ranking") or []],
                fallback=bool(run.get("abstained")),
                abstained=bool(run.get("abstained")),
                trace=trace,
            )
            metrics = {item.name: item for item in evaluate_case(case, output).metrics}
            if relevant_ids:
                retrieval_values["recall_at_3"].append(metrics["rag_recall_at_k"].score)
                retrieval_values["mrr_at_3"].append(metrics["rag_mrr"].score)
                retrieval_values["ndcg_at_3"].append(metrics["rag_ndcg_at_k"].score)
            abstention_values.append(metrics["no_answer_decision"].score)
            attribution = _claim_attribution(row, profile)
            if attribution is not None:
                claim_coverage.append(attribution[0])
                evidence_precision.append(attribution[1])
        profile_summary = {
            key: round(_average(values), 6) for key, values in retrieval_values.items()
        }
        profile_summary.update(
            {
                "abstention_accuracy": round(_average(abstention_values), 6),
                "claim_coverage": round(_average(claim_coverage), 6),
                "evidence_precision": round(_average(evidence_precision), 6),
            }
        )
        profiles[profile] = profile_summary

    candidate_profile = "fixed" if "fixed" in profiles else "candidate" if "candidate" in profiles else None
    gate_metrics = (
        profiles.get(candidate_profile or "", {})
        if candidate_profile
        else {}
    )
    deltas = {}
    if "baseline" in profiles and candidate_profile:
        deltas = {
            key: round(float(value) - float(profiles["baseline"].get(key, 0.0)), 6)
            for key, value in gate_metrics.items()
        }
    regressions = sorted(key for key, value in deltas.items() if value < 0.0)
    gate_passed = (
        bool(gate_metrics)
        and all(float(value) >= 0.90 for value in gate_metrics.values())
        and not regressions
    )
    return {
        "path": str(path),
        "case_count": len(rows),
        "k": k,
        "profiles": profiles,
        "candidate_profile": candidate_profile,
        "deltas": deltas,
        "regressions": regressions,
        "release_gate_threshold": 0.90,
        "release_gate_passed": gate_passed,
    }


def markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "# AGI-saber Benchmark Suite",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 已校验数据集：{result['validated_dataset_count']}",
        f"- Replay 用例：{result['replay_case_count']}",
        f"- RAG Golden Query：{result['rag_quality']['case_count']}",
        f"- 组件门禁测试：{result['component_test_count']}",
        f"- 总体门禁：{'通过' if result['passed'] else '未通过'}",
        "",
        "## Agent 离线回放与版本对比",
        "",
        "| 数据集 | Profile | 用例 | 通过率 | P95(ms) | 硬门禁失败 | Release Gate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for dataset in result["datasets"]:
        for profile, run in dataset["runs"].items():
            lines.append(
                f"| {dataset['name']} | {PROFILE_NAMES.get(profile, profile)} | {run['total']} | "
                f"{float(run['pass_rate']):.1%} | {float(run['p95_latency_ms']):.2f} | "
                f"{run['hard_gate_failures']} | {'通过' if run['release_gate_passed'] else '未通过'} |"
            )
        comparison = dataset.get("comparison")
        if comparison:
            lines.extend(
                [
                    "",
                    f"- **{dataset['name']}**：修复 {len(comparison['fixed'])} 条，"
                    f"新增回归 {len(comparison['regressions'])} 条，通过率变化 "
                    f"{float(comparison['pass_rate_delta']):+.1%}。",
                ]
            )
    if result["validation_only_datasets"]:
        lines.extend(["", "以下 EvalCase 数据集由组件门禁直接执行，不伪造 Replay 输出："])
        for dataset in result["validation_only_datasets"]:
            lines.append(f"- `{dataset['name']}`：{dataset['case_count']} 条，Schema 校验通过。")
    lines.extend(
        [
            "",
            "## RAG Golden Set 效果指标",
            "",
            "| Profile | Recall@3 | MRR@3 | nDCG@3 | 无答案准确率 | Claim 覆盖率 | 证据精确率 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile, metrics in result["rag_quality"]["profiles"].items():
        lines.append(
            f"| {PROFILE_NAMES.get(profile, profile)} | {metrics['recall_at_3']:.3f} | {metrics['mrr_at_3']:.3f} | "
            f"{metrics['ndcg_at_3']:.3f} | {metrics['abstention_accuracy']:.3f} | "
            f"{metrics['claim_coverage']:.3f} | {metrics['evidence_precision']:.3f} |"
        )
    lines.append(
        f"\nRAG 候选版本门禁：{'通过' if result['rag_quality']['release_gate_passed'] else '未通过'} "
        f"（各项最低 {result['rag_quality']['release_gate_threshold']:.0%}）。"
    )
    lines.extend(
        [
            "",
            "## RAG、记忆与 Harness 组件门禁",
            "",
            "| 专项 | 测试 | 失败 | 错误 | 跳过 | 耗时(s) | 门禁 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for component in result["components"]:
        lines.append(
            f"| {component['name']} | {component['tests']} | {component['failures']} | "
            f"{component['errors']} | {component['skipped']} | "
            f"{float(component['duration_seconds']):.3f} | "
            f"{'通过' if component['passed'] else '未通过'} |"
        )
    lines.extend(
        [
            "",
            "> Replay Benchmark 证明数据、评分器和门禁可复现；上线结论还必须补充 local/HTTP adapter 的真实模型运行。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行全部 AGI-saber 离线 Benchmark")
    parser.add_argument("--dataset", action="append", default=[], help="指定 JSONL；可重复")
    parser.add_argument("--database", default=str(PROJECT_ROOT / "runtime" / "benchmark-suite.db"))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "runtime" / "benchmark-suite"))
    parser.add_argument(
        "--skip-component-tests",
        action="store_true",
        help="只运行 EvaluationService Replay，不执行 RAG/记忆/Harness 组件门禁",
    )
    args = parser.parse_args()

    paths = [Path(value).resolve() for value in args.dataset]
    if not paths:
        paths = sorted((PROJECT_ROOT / "examples" / "evaluation").glob("*_eval.jsonl"))
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    database_path = Path(args.database).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    store = EvaluationStore(f"sqlite:///{database_path.as_posix()}")
    service = EvaluationService(store)
    datasets: list[dict[str, Any]] = []
    validation_only_datasets: list[dict[str, Any]] = []
    try:
        for path in paths:
            cases = load_cases(path)
            profiles = replay_profiles(cases)
            if not profiles:
                validation_only_datasets.append(
                    {"name": path.stem, "path": str(path), "case_count": len(cases)}
                )
                continue
            name = f"benchmark-{path.stem}"
            dataset = next((item for item in store.list_datasets() if item["name"] == name), None)
            if dataset is None:
                dataset = service.create_dataset(name, description=f"Offline benchmark: {path.name}", domain="agent-benchmark")
            version = service.import_cases(dataset["id"], cases, metadata={"source": str(path)})
            runs: dict[str, dict[str, Any]] = {}
            raw_runs: dict[str, dict[str, Any]] = {}
            for profile in profiles:
                run = service.create_run(
                    version["id"],
                    name=f"{path.stem}-{profile}",
                    adapter={"type": "replay", "profile": profile},
                )
                run = service.execute_run(run["id"])
                raw_runs[profile] = run
                runs[profile] = metric_summary(run)
            comparison = None
            candidate_name = "fixed" if "fixed" in raw_runs else "candidate" if "candidate" in raw_runs else None
            if "baseline" in raw_runs and candidate_name:
                comparison = service.compare_runs(raw_runs["baseline"]["id"], raw_runs[candidate_name]["id"])
            datasets.append(
                {
                    "name": path.stem,
                    "path": str(path),
                    "case_count": len(cases),
                    "runs": runs,
                    "comparison": comparison,
                    "candidate_profile": candidate_name,
                }
            )
    finally:
        service.close()

    replay_passed = all(
        dataset["runs"][dataset["candidate_profile"]]["release_gate_passed"]
        and not (dataset.get("comparison") or {}).get("regressions")
        for dataset in datasets
        if dataset.get("candidate_profile")
    )
    rag_quality = rag_quality_summary(RAG_QUALITY_FIXTURE)
    components = [] if args.skip_component_tests else [
        run_component_suite(name, test_path, output_dir)
        for name, test_path in COMPONENT_SUITES
    ]
    components_passed = all(item["passed"] for item in components)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": replay_passed and rag_quality["release_gate_passed"] and components_passed,
        "replay_passed": replay_passed,
        "rag_quality_passed": rag_quality["release_gate_passed"],
        "components_passed": components_passed,
        "validated_dataset_count": len(datasets) + len(validation_only_datasets),
        "replay_case_count": sum(item["case_count"] for item in datasets),
        "component_test_count": sum(int(item["tests"]) for item in components),
        "datasets": datasets,
        "validation_only_datasets": validation_only_datasets,
        "rag_quality": rag_quality,
        "components": components,
    }
    json_report_path = output_dir / "benchmark-results.json"
    markdown_report_path = output_dir / "benchmark-report.md"
    json_report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_report_path.write_text(markdown_report(result), encoding="utf-8")
    # ASCII-only console output is stable on Windows terminals using GBK, while
    # the persisted reports retain readable UTF-8 Chinese and full details.
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "validated_dataset_count": result["validated_dataset_count"],
                "replay_case_count": result["replay_case_count"],
                "rag_golden_case_count": result["rag_quality"]["case_count"],
                "component_test_count": result["component_test_count"],
                "json_report": str(json_report_path),
                "markdown_report": str(markdown_report_path),
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
