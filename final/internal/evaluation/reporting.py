"""评测报告渲染（Markdown / CSV / Prometheus）——从 evaluation/service.py 拆出。

纯格式化逻辑：读 store 的查询结果，产出面向人的报告文本；
不含任何持久化写入与生命周期状态变更。显示名常量仍归 service.py 所有，
由调用方经参数注入，保持本模块与 service 解耦。
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from io import StringIO
from typing import Mapping


def markdown_report(
    store,
    run_id: str,
    *,
    metric_names: Mapping[str, str],
    badcase_names: Mapping[str, str],
) -> str:
    run = store.get_run(run_id)
    summary = run.get("summary") or {}
    results = store.list_case_results(run_id)
    badcases = store.list_badcases(run_id=run_id)
    lines = [
        f"# Agent 质量评测报告：{run.get('name') or run_id}",
        "",
        "> 本报告评测 Agent 工程链路，不替代医生、医保政策专家或人工安全审核。",
        "",
        "## 运行信息",
        "",
        f"- Run ID：`{run_id}`",
        f"- 状态：`{run['status']}`",
        f"- 数据集版本：`{run['dataset_version_id']}`",
        f"- 通过率：{float(summary.get('pass_rate', 0.0)):.2%}",
        f"- 平均得分：{float(summary.get('average_score', 0.0)):.3f}",
        f"- P95 延迟：{float(summary.get('p95_latency_ms', 0.0)):.1f} ms",
        f"- S0/S1 硬门禁失败：{int(summary.get('hard_gate_failures', 0))}",
        f"- 发布门禁：{'通过' if summary.get('release_gate_passed') else '未通过'}",
        "",
        "## 指标汇总",
        "",
        "| 指标 | 适用样本 | 通过率 | 平均得分 |",
        "|---|---:|---:|---:|",
    ]
    for name, metric in sorted((summary.get("metrics") or {}).items()):
        lines.append(
            f"| {metric_names.get(name, name)} (`{name}`) | {int(metric.get('applicable', 0))} | "
            f"{float(metric.get('pass_rate', 0.0)):.2%} | "
            f"{float(metric.get('average_score', 0.0)):.3f} |"
        )
    lines.extend(
        [
            "",
            "## 失败用例",
            "",
            "| Case | 状态 | 失败指标 | Badcase 分类 | 严重级别 |",
            "|---|---|---|---|---|",
        ]
    )
    badcase_by_case_run = {item["case_run_id"]: item for item in badcases}
    failed_results = [item for item in results if item["status"] != "passed"]
    if not failed_results:
        lines.append("| - | 全部通过 | - | - | - |")
    for item in failed_results:
        metrics = item.get("metrics") or {}
        badcase = badcase_by_case_run.get(item["id"], {})
        failed_metric_names = metrics.get("failed_metrics") or []
        failed_metric_labels = [metric_names.get(name, name) for name in failed_metric_names]
        category = str(badcase.get("category", "-"))
        lines.append(
            f"| `{item['case_id']}` | {item['status']} | "
            f"{', '.join(failed_metric_labels) or '-'} | "
            f"{badcase_names.get(category, category)} | "
            f"{badcase.get('severity', '-')} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "安全指标使用独立硬门禁，不参与平均分抵消。失败用例应完成归因、修复与同版本数据集回归后再关闭。",
            "",
        ]
    )
    return "\n".join(lines)


def csv_report(store, run_id: str) -> str:
    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        ["case_id", "status", "overall_score", "hard_gate_failed", "failed_metrics", "error"]
    )
    for item in store.list_case_results(run_id):
        metrics = item.get("metrics") or {}
        writer.writerow(
            [
                item["case_id"],
                item["status"],
                metrics.get("overall_score", ""),
                metrics.get("hard_gate_failed", ""),
                "|".join(metrics.get("failed_metrics") or []),
                json.dumps(item.get("error") or {}, ensure_ascii=False),
            ]
        )
    return output.getvalue()


def prometheus_metrics(store) -> str:
    """Expose low-cardinality operational metrics in Prometheus text format."""

    runs = store.list_runs(limit=1000)
    badcases = store.list_badcases(limit=10000)
    run_statuses = Counter(item["status"] for item in runs)
    badcase_statuses = Counter((item["status"], item["severity"]) for item in badcases)
    lines = [
        "# HELP agi_eval_runs_total Number of persisted evaluation runs.",
        "# TYPE agi_eval_runs_total gauge",
    ]
    for status in sorted(run_statuses):
        lines.append(f'agi_eval_runs_total{{status="{status}"}} {run_statuses[status]}')
    lines.extend(
        [
            "# HELP agi_eval_badcases_total Number of evaluation Badcases.",
            "# TYPE agi_eval_badcases_total gauge",
        ]
    )
    for status, severity in sorted(badcase_statuses):
        lines.append(
            f'agi_eval_badcases_total{{status="{status}",severity="{severity}"}} '
            f"{badcase_statuses[(status, severity)]}"
        )
    completed = next((run for run in runs if run["status"] == "completed"), None)
    if completed is not None:
        summary = completed.get("summary") or {}
        lines.extend(
            [
                "# HELP agi_eval_latest_pass_rate Pass rate of the latest completed run.",
                "# TYPE agi_eval_latest_pass_rate gauge",
                f"agi_eval_latest_pass_rate {float(summary.get('pass_rate', 0.0))}",
                "# HELP agi_eval_latest_hard_gate_failures S0/S1 failures in the latest completed run.",
                "# TYPE agi_eval_latest_hard_gate_failures gauge",
                f"agi_eval_latest_hard_gate_failures {int(summary.get('hard_gate_failures', 0))}",
                "# HELP agi_eval_latest_p95_latency_ms P95 case latency of the latest completed run.",
                "# TYPE agi_eval_latest_p95_latency_ms gauge",
                f"agi_eval_latest_p95_latency_ms {float(summary.get('p95_latency_ms', 0.0))}",
            ]
        )
    return "\n".join(lines) + "\n"
