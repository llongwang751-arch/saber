"""Run the complete offline evaluation and regression loop without API keys."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from internal.evaluation.service import EvaluationService  # noqa: E402
from internal.evaluation.store import EvaluationStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AGI-saber Agent 质量评测：baseline → 修复版 → 回归对比"
    )
    parser.add_argument(
        "--dataset",
        default=str(PROJECT_ROOT / "examples" / "evaluation" / "tencent_medical_agent_eval.jsonl"),
        help="JSONL 评测集路径",
    )
    parser.add_argument(
        "--database",
        default=str(PROJECT_ROOT / "runtime" / "evaluation-demo.db"),
        help="SQLite 数据库路径",
    )
    parser.add_argument(
        "--report-dir",
        default=str(PROJECT_ROOT / "runtime" / "reports"),
        help="Markdown/CSV 报告输出目录",
    )
    args = parser.parse_args()

    database_path = Path(args.database).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    store = EvaluationStore(f"sqlite:///{database_path.as_posix()}")
    service = EvaluationService(store)
    try:
        dataset_name = "tencent-jd-synthetic-agent-eval"
        dataset = next(
            (item for item in store.list_datasets() if item["name"] == dataset_name),
            None,
        )
        if dataset is None:
            dataset = service.create_dataset(
                dataset_name,
                description="合成、脱敏的 Agent 工程链路评测集，不用于临床判断",
                domain="medical-agent-quality",
                metadata={"data_policy": "synthetic-only"},
            )
        version = service.import_jsonl(
            dataset["id"],
            args.dataset,
            metadata={"purpose": "Tencent JD interview demo"},
        )

        baseline = service.create_run(
            version["id"],
            name="baseline-agent",
            adapter={"type": "replay", "profile": "baseline"},
        )
        candidate = service.create_run(
            version["id"],
            name="fixed-agent",
            adapter={"type": "replay", "profile": "fixed"},
        )
        baseline = service.execute_run(baseline["id"])
        candidate = service.execute_run(candidate["id"])
        comparison = service.compare_runs(baseline["id"], candidate["id"])

        for label, run in (("baseline", baseline), ("fixed", candidate)):
            (report_dir / f"{label}-{run['id']}.md").write_text(
                service.markdown_report(run["id"]),
                encoding="utf-8",
            )
            (report_dir / f"{label}-{run['id']}.csv").write_text(
                service.csv_report(run["id"]),
                encoding="utf-8-sig",
            )

        print(
            json.dumps(
                {
                    "dataset_id": dataset["id"],
                    "dataset_version_id": version["id"],
                    "baseline": _brief(baseline),
                    "fixed": _brief(candidate),
                    "comparison": comparison,
                    "reports": str(report_dir),
                    "note": "synthetic replay metrics; not live-model or clinical accuracy",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if candidate["summary"].get("release_gate_passed") else 1
    finally:
        service.close()


def _brief(run: dict) -> dict:
    summary = run.get("summary") or {}
    return {
        "run_id": run["id"],
        "status": run["status"],
        "passed": summary.get("passed"),
        "failed": summary.get("failed"),
        "pass_rate": summary.get("pass_rate"),
        "hard_gate_failures": summary.get("hard_gate_failures"),
        "release_gate_passed": summary.get("release_gate_passed"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
