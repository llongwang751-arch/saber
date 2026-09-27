"""Run a research request against an existing server and save its cited report.

By default this creates a plan and stops for review. --approve-plan explicitly
approves the generated plan; --run-id continues a previously reviewed run.
Authentication is read from AGI_AUTH_TOKEN, never command-line arguments.
"""

import argparse
import json
import os
from pathlib import Path
import time

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", nargs="?", default="比较 SQLite 与 PostgreSQL 的事务和并发模型，并给出来源")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--use-rag", action="store_true")
    parser.add_argument("--approve-plan", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path, default=Path("runtime/research-report.md"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/") + "/api/agent-runs"
    with requests.Session() as session:
        if token := os.getenv("AGI_AUTH_TOKEN"):
            session.headers["Authorization"] = "Bearer " + token

        def call(method, url, **kwargs):
            response = session.request(method, url, timeout=30, **kwargs)
            response.raise_for_status()
            return response.json()

        if args.run_id:
            run_id = args.run_id
        else:
            run = call("POST", base, json={"message": args.topic, "mode": "research", "use_rag": args.use_rag})
            run_id = run["run_id"]
        print("run_id:", run_id)
        endpoint = base + "/" + run_id
        deadline = time.monotonic() + args.timeout
        reviewed = False
        while time.monotonic() < deadline:
            run = call("GET", endpoint)
            if run["status"] == "awaiting_plan_review":
                review = call("GET", endpoint + "/plan")
                print(json.dumps(review["plan"], ensure_ascii=False, indent=2))
                if not args.approve_plan or reviewed:
                    print("Plan requires review in the workbench. Continue with --run-id", run_id)
                    return
                call("POST", endpoint + "/plan/review", json={"action": "approve", "version": review["version"]})
                reviewed = True
            elif run["status"] == "completed":
                response = (run.get("result") or {}).get("response") or {}
                report = response.get("report_markdown") or response.get("answer")
                if not report:
                    raise SystemExit("Run completed without a report")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(report, encoding="utf-8")
                print("Report:", args.output.resolve())
                return
            elif run["status"] in {"failed", "cancelled", "interrupted"}:
                raise SystemExit(f"Research {run['status']}: {json.dumps(run.get('result'), ensure_ascii=False)}")
            time.sleep(0.5)
        raise SystemExit(f"Timed out waiting; run {run_id} remains available in the workbench")


if __name__ == "__main__":
    main()
