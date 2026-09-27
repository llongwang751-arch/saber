"""Low-rate HTTP acceptance against an existing deployment and dedicated account.

Credentials come from a private JSON file; output contains only timings, states
and run IDs. TLS verification is on. No service restarts or fault injection.
"""
import argparse
import json
import math
from pathlib import Path
import time
from uuid import uuid4

import requests


def verify(base_url, account_file, output, *, ca=True, duration=30, required=("application", "run_store", "milvus")):
    results = []
    session = requests.Session()
    session.verify = ca
    session.headers["User-Agent"] = "Saber-Production-Acceptance/1"
    base_url = base_url.rstrip("/")

    def request(method, path, **kwargs):
        return session.request(method, base_url + path, timeout=(5, 120), **kwargs)

    def record(name, action):
        started = time.monotonic()
        try:
            evidence = action()
            results.append({"name": name, "passed": True, "evidence": evidence})
        except Exception as exc:
            results.append({"name": name, "passed": False, "error_type": type(exc).__name__})
        results[-1]["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"checks": results, "passed": all(r["passed"] for r in results)}, indent=2), encoding="utf-8")
        print(json.dumps(results[-1]), flush=True)
        return results[-1]["passed"]

    def authentication():
        response = request("GET", "/api/agent-runs")
        assert response.status_code == 401
        response = request("POST", "/api/auth/login", json=json.loads(Path(account_file).read_text()))
        assert response.ok
        session.headers["Authorization"] = "Bearer " + response.json()["token"]
        return {"anonymous_rejected": True, "login": True}

    def ready():
        response = request("GET", "/api/ops/readiness")
        assert response.ok
        report = response.json()
        assert report["enforced"] and report["ready"]
        assert set(required).issubset(report["checks"])
        assert all(report["checks"][k]["ready"] for k in required)
        assert request("GET", "/readyz").status_code == 200
        return report

    def run():
        key = uuid4().hex
        body = {"message": "请只回复：生产验收通过。不要调用工具。", "conversation_id": uuid4().hex}
        response = request("POST", "/api/agent-runs", json=body, headers={"Idempotency-Key": key})
        assert response.ok
        run_id = response.json()["run_id"]
        duplicate = request("POST", "/api/agent-runs", json=body, headers={"Idempotency-Key": key})
        assert duplicate.ok and duplicate.json()["run_id"] == run_id
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            item = request("GET", "/api/agent-runs/" + run_id).json()
            if item["status"] not in {"pending", "running", "cancelling"}:
                break
            time.sleep(0.5)
        assert item["status"] == "completed" and item["result"]["response"]["answer"].strip()
        events = request("GET", f"/api/agent-runs/{run_id}/events").json()
        assert sum(e["type"] == "done" for e in events) == 1
        cursor = events[0]["event_id"]
        replay = request("GET", f"/api/agent-runs/{run_id}/stream", headers={"Last-Event-ID": str(cursor)})
        assert replay.ok and "event: done" in replay.text and "event: queued" not in replay.text
        return {"run_id": run_id, "status": item["status"], "idempotent": True, "sse_replay": True}

    def steady_reads():
        timings, failures = [], 0
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            start = time.monotonic()
            response = request("GET", "/api/agent-runs?limit=5")
            timings.append((time.monotonic() - start) * 1000)
            failures += int(response.status_code != 200)
            time.sleep(0.2)
        assert failures == 0 and timings
        p95 = sorted(timings)[max(0, math.ceil(len(timings) * .95) - 1)]
        assert p95 < 2000
        return {"duration_seconds": duration, "requests": len(timings), "errors": failures,
                "p95_ms": round(p95, 2), "p95_limit_ms": 2000, "profile": "serial-read-max-5rps"}

    try:
        if record("authentication", authentication):
            record("strict_readiness", ready)
            record("real_model_background_idempotency_replay", run)
            record("steady_authenticated_reads", steady_reads)
            record("final_readiness", ready)
    finally:
        session.close()
    return all(r["passed"] for r in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--account-file", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ca-file")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--required", default="application,run_store,milvus")
    args = parser.parse_args()
    if not 1 <= args.duration <= 3600:
        parser.error("duration must be 1..3600 seconds")
    raise SystemExit(0 if verify(args.url, args.account_file, args.output, ca=args.ca_file or True,
                                duration=args.duration, required=args.required.split(",")) else 1)
