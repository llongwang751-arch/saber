"""Calibrate the RAG no-answer threshold from labelled JSONL or CSV samples.

Examples:
    python scripts/calibrate_no_answer_threshold.py examples/no_answer_labels.jsonl
    python scripts/calibrate_no_answer_threshold.py labels.csv --false-answer-cost 10
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from internal.evaluation.thresholds import (  # noqa: E402
    ThresholdSample,
    calibrate_no_answer_threshold,
    render_threshold_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="校准 RAG 无答案阈值")
    parser.add_argument("input", type=Path, help="JSONL 或 CSV；字段：score, answerable[, sample_id]")
    parser.add_argument("--output", type=Path, help="Markdown 报告路径")
    parser.add_argument("--json-output", type=Path, help="完整逐阈值 JSON 结果路径")
    parser.add_argument("--false-answer-cost", type=float, default=5.0)
    parser.add_argument("--false-refusal-cost", type=float, default=1.0)
    args = parser.parse_args()

    samples = load_samples(args.input)
    calibration = calibrate_no_answer_threshold(
        samples,
        false_answer_cost=args.false_answer_cost,
        false_refusal_cost=args.false_refusal_cost,
    )
    report = render_threshold_report(calibration)

    output = args.output or args.input.with_name(f"{args.input.stem}-threshold-report.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(calibration.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"recommended_threshold={calibration.recommended.threshold:.2f}")
    print(f"report={output}")
    return 0


def load_samples(path: Path) -> list[ThresholdSample]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        raise ValueError("input must be .jsonl or .csv")
    return [_parse_sample(row, index + 1) for index, row in enumerate(rows)]


def _parse_sample(row: dict, line_number: int) -> ThresholdSample:
    if "score" not in row or "answerable" not in row:
        raise ValueError(f"row {line_number} must contain score and answerable")
    return ThresholdSample(
        score=float(row["score"]),
        answerable=_parse_bool(row["answerable"], line_number),
        sample_id=str(row.get("sample_id") or row.get("id") or line_number),
    )


def _parse_bool(value, line_number: int) -> bool:
    if isinstance(value, bool):
        return value
    normalised = str(value).strip().casefold()
    if normalised in {"1", "true", "yes", "y", "是", "可回答"}:
        return True
    if normalised in {"0", "false", "no", "n", "否", "不可回答"}:
        return False
    raise ValueError(f"row {line_number} has invalid answerable value: {value!r}")


if __name__ == "__main__":
    raise SystemExit(main())
