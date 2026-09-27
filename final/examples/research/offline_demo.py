"""Deterministic integration fixture; not a real-world research or LLM benchmark.

Run from final/: python examples/research/offline_demo.py
No network, credentials, database, or Docker is used.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from internal.research import ResearchEngine  # noqa: E402

PLAN = {
    "objective": "离线样例：验证计划、两轮研究和引用报告",
    "constraints": ["仅使用明确标记的本地演示材料；这不是现实事实调查"],
    "steps": [
        {"id": "research", "kind": "research", "title": "核对演示系统的部署和恢复要求"},
        {"id": "report", "kind": "write", "title": "整理演示报告", "depends_on": ["research"]},
    ],
}


class FixtureLLM:
    """Explicitly scripted responses for demonstrating the provider contract."""

    def __init__(self):
        self.responses = iter([
            {"queries": ["部署要求"]},
            {"findings": [{"source_id": "S1", "quote": "演示系统需要 Python 3.11。", "claim": "部署版本约束"}],
             "gaps": ["尚未找到恢复要求"], "queries": ["恢复要求"], "sufficient": False},
            {"findings": [{"source_id": "S2", "quote": "演示系统在恢复时保留已完成步骤。", "claim": "恢复行为"}],
             "gaps": [], "queries": [], "sufficient": True},
            {"sections": ["演示材料中的要求"]},
            "本离线样例的部署要求是 Python 3.11 [S1]。恢复时保留已完成步骤 [S2]。\n\n"
            "以上为演示材料与脚本模型输出，用于验证执行链路，不代表真实外部研究。",
        ])

    def chat(self, messages, system_prompt=""):
        value = next(self.responses)
        return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value


def fixture_search(query, *, token=None):
    if "恢复" in query:
        return [{"url_or_doc_id": "doc:offline-fixture:recovery", "title": "演示恢复要求",
                 "content": "演示系统在恢复时保留已完成步骤。", "content_kind": "fixture"}]
    return [{"url_or_doc_id": "doc:offline-fixture:deployment", "title": "演示部署要求",
             "content": "演示系统需要 Python 3.11。", "content_kind": "fixture"}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="runtime/research-demo/report.md")
    args = parser.parse_args()
    result = ResearchEngine(FixtureLLM(), search=fixture_search).execute(
        PLAN, run_id="explicit-offline-fixture",
        on_event=lambda event, data: print(json.dumps({"type": event, "data": data}, ensure_ascii=False))
        if event in {"research_round", "node_done"} else None,
    )
    destination = Path(args.output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(result.report_markdown, encoding="utf-8")
    print(json.dumps({"fixture": True, "status": result.status, "rounds": result.usage["rounds"],
                      "sources": len(result.sources), "report": str(destination)}, ensure_ascii=False))
    if result.status != "completed" or len(result.sources) != 2 or result.usage["rounds"] != 2:
        raise SystemExit("Offline fixture did not satisfy its assertions")


if __name__ == "__main__":
    main()
