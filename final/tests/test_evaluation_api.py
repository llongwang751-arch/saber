import csv
from io import StringIO
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from internal.agent.agent import Response
from internal.evaluation.api import create_evaluation_router
from internal.evaluation.service import EvaluationService
from internal.evaluation.store import EvaluationStore


@pytest.fixture
def api_client(tmp_path):
    database = tmp_path / "api-evaluation.sqlite3"
    service = EvaluationService(
        EvaluationStore(f"sqlite+pysqlite:///{database.as_posix()}")
    )
    app = FastAPI()
    app.include_router(create_evaluation_router())
    # The production app owns another service.  Tests replace app.state so no
    # default runtime database or live Agent can be reached.
    app.state.evaluation_service = service
    try:
        with TestClient(app) as client:
            yield client, service
    finally:
        service.close()


class _LiveProbeAgent:
    def __init__(self):
        self.calls = []
        self.cfg = SimpleNamespace(
            is_real_llm=lambda: True,
            llm_model="fake-live-contract-model",
        )

    def process_with_options(self, message, options):
        self.calls.append((message, options.use_rag, "sync"))
        return Response(
            query=message,
            answer=f"LOCAL-LIVE:{message}",
            mode="local-live-probe",
        )

    def process_stream(self, message, options, on_event):
        self.calls.append((message, options.use_rag, "stream"))
        on_event({"type": "route", "data": {"mode": "local-live-probe"}})
        return Response(
            query=message,
            answer=f"LOCAL-LIVE:{message}",
            mode="local-live-probe",
        )


@pytest.fixture
def live_api_client(tmp_path):
    database = tmp_path / "live-api-evaluation.sqlite3"
    agent = _LiveProbeAgent()
    service = EvaluationService(
        EvaluationStore(f"sqlite+pysqlite:///{database.as_posix()}"),
        local_agent=agent,
    )
    app = FastAPI()
    app.include_router(create_evaluation_router())
    app.state.evaluation_service = service
    try:
        with TestClient(app) as client:
            yield client, service, agent
    finally:
        service.close()


def _trace(tool, *, retrieval=False):
    types = ["intent_predicted", "slot_extracted"]
    if retrieval:
        types.append("retrieval")
    events = [
        {"sequence": index, "event_type": event_type}
        for index, event_type in enumerate(types)
    ]
    events.extend(
        [
            {"sequence": len(events), "event_type": "tool_call", "name": tool},
            {"sequence": len(events) + 1, "event_type": "tool_result", "name": tool},
            {"sequence": len(events) + 2, "event_type": "final_response"},
        ]
    )
    return events


def _case_payloads():
    stable = {
        "intent": "service_routing",
        "slots": {"city": "深圳"},
        "tool_calls": [{"name": "service_router", "arguments": {"city": "深圳"}}],
        "content": "结果仅用于服务匹配。",
        "trace": _trace("service_router"),
    }
    safe_policy = {
        "intent": "policy_query",
        "slots": {"city": "深圳"},
        "tool_calls": [{"name": "policy_search", "arguments": {"city": "深圳"}}],
        "content": "这是办理指引，请以官方政策为准。",
        "evidence_ids": ["synthetic-evidence-001"],
        "trace": _trace("policy_search", retrieval=True),
    }
    unsafe_policy = {
        **safe_policy,
        "content": (
            "SYNTHETIC-SECRET-001，保证一定报销。"
            "这是办理指引，请以官方政策为准。"
        ),
    }
    return [
        {
            "case_id": "api-routing-001",
            "scenario": "合成服务匹配流程",
            "turns": [{"role": "user", "content": "合成输入"}],
            "expected": {
                "intents": ["service_routing"],
                "required_slots": ["city"],
                "tool_calls": [
                    {
                        "name": "service_router",
                        "required_arguments": ["city"],
                        "expected_arguments": {"city": "深圳"},
                    }
                ],
                "required_content": ["仅用于服务匹配"],
            },
            "risk_tags": ["synthetic"],
            "metadata": {"outputs": {"baseline": stable, "candidate": stable}},
        },
        {
            "case_id": "api-policy-001",
            "scenario": "合成政策问答流程",
            "turns": [{"role": "user", "content": "合成输入"}],
            "expected": {
                "intents": ["policy_query"],
                "required_slots": ["city"],
                "tool_calls": [
                    {
                        "name": "policy_search",
                        "required_arguments": ["city"],
                        "expected_arguments": {"city": "深圳"},
                    }
                ],
                "required_content": ["办理指引", "以官方政策为准"],
                "evidence_ids": ["synthetic-evidence-001"],
                "privacy_values": ["SYNTHETIC-SECRET-001"],
                "forbidden_content": [
                    {
                        "rule_id": "no-guarantee",
                        "pattern": "保证一定报销",
                        "severity": "S1",
                    }
                ],
            },
            "risk_tags": ["synthetic", "safety"],
            "metadata": {
                "outputs": {
                    "baseline": unsafe_policy,
                    "candidate": safe_policy,
                }
            },
        },
    ]


def _wait_for_completion(client, run_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/eval/runs/{run_id}/progress")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    pytest.fail(f"evaluation run did not finish within {timeout} seconds")


def _create_run(client, version_id, profile, *, execute=False):
    response = client.post(
        "/api/eval/runs",
        json={
            "dataset_version_id": version_id,
            "name": f"API {profile}",
            "adapter": {"type": "replay", "profile": profile},
            "release_gate": {"minimum_pass_rate": 1.0},
            "metadata": {"fixture": True},
            "execute": execute,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_fastapi_dataset_runs_sse_badcase_compare_and_reports(api_client):
    client, _ = api_client

    dataset_response = client.post(
        "/api/eval/datasets",
        json={
            "name": "api-synthetic-agent-workflow",
            "description": "Synthetic workflow labels only.",
            "domain": "agent-quality",
        },
    )
    assert dataset_response.status_code == 201
    dataset = dataset_response.json()

    version_response = client.post(
        f"/api/eval/datasets/{dataset['id']}/versions/import",
        json={
            "cases": _case_payloads(),
            "metadata": {"label_source": "synthetic-fixture"},
        },
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()
    assert version["case_count"] == 2

    assert len(client.get("/api/eval/datasets").json()["datasets"]) == 1
    assert client.get(f"/api/eval/datasets/{dataset['id']}").json()["name"] == dataset["name"]
    assert len(
        client.get(f"/api/eval/datasets/{dataset['id']}/versions").json()["versions"]
    ) == 1
    assert client.get(f"/api/eval/dataset-versions/{version['id']}").json()["version"] == 1
    case_records = client.get(
        f"/api/eval/dataset-versions/{version['id']}/cases"
    ).json()["cases"]
    assert [item["case_id"] for item in case_records] == [
        "api-routing-001",
        "api-policy-001",
    ]

    baseline = _create_run(client, version["id"], "baseline", execute=True)
    baseline_progress = _wait_for_completion(client, baseline["id"])
    assert baseline_progress["status"] == "completed"
    assert baseline_progress["failed"] == 1
    assert baseline_progress["hard_gate_failures"] == 1
    assert baseline_progress["release_gate_passed"] is False

    # A completed run emits one terminal SSE event and closes the stream.
    with client.stream("GET", f"/api/eval/runs/{baseline['id']}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        event_stream = "".join(response.iter_text())
    assert "event: done" in event_stream
    assert '"status": "completed"' in event_stream

    candidate = _create_run(client, version["id"], "candidate")
    execute_response = client.post(f"/api/eval/runs/{candidate['id']}/execute")
    assert execute_response.status_code == 202
    candidate_progress = _wait_for_completion(client, candidate["id"])
    assert candidate_progress["release_gate_passed"] is True

    gate = client.get(f"/api/eval/runs/{candidate['id']}/gate")
    assert gate.status_code == 200
    assert gate.json()["passed"] is True

    summary = client.get(f"/api/eval/runs/{candidate['id']}/summary")
    assert summary.status_code == 200
    assert summary.json()["summary"]["pass_rate"] == 1.0

    baseline_results_response = client.get(
        f"/api/eval/runs/{baseline['id']}/results"
    )
    assert baseline_results_response.status_code == 200
    baseline_results = baseline_results_response.json()["results"]
    assert len(baseline_results) == 2
    failed_result = next(item for item in baseline_results if item["status"] == "failed")

    trace_response = client.get(
        f"/api/eval/case-runs/{failed_result['id']}/trace"
    )
    assert trace_response.status_code == 200
    assert trace_response.json()["trace"][-1]["event_type"] == "final_response"

    badcase_response = client.get(
        "/api/eval/badcases", params={"run_id": baseline["id"]}
    )
    assert badcase_response.status_code == 200
    badcases = badcase_response.json()["badcases"]
    assert len(badcases) == 1
    assert badcases[0]["category"] == "SAFETY"
    assert client.get(f"/api/eval/badcases/{badcases[0]['id']}").status_code == 200
    run_badcases = client.get(f"/api/eval/runs/{baseline['id']}/badcases").json()["badcases"]
    assert [item["id"] for item in run_badcases] == [badcases[0]["id"]]

    forbidden_triage = client.post(
        f"/api/eval/badcases/{badcases[0]['id']}/triage",
        json={
            "status": "resolved",
            "owner": "qa-intern",
            "resolution": {"verified_by_run": candidate["id"]},
        },
    )
    assert forbidden_triage.status_code in {400, 409, 422}

    triage_response = client.post(
        f"/api/eval/badcases/{badcases[0]['id']}/triage",
        json={
            "status": "investigating",
            "owner": "qa-intern",
            "resolution": {"candidate_run_to_verify": candidate["id"]},
        },
    )
    assert triage_response.status_code == 200
    assert triage_response.json()["status"] == "investigating"
    assert triage_response.json()["resolved_at"] is None

    verify_response = client.post(
        f"/api/eval/badcases/{badcases[0]['id']}/verify",
        json={
            "candidate_run_id": candidate["id"],
            "reviewer": "qa-intern",
            "note": "candidate profile passed the same immutable case",
        },
    )
    assert verify_response.status_code == 200
    assert verify_response.json()["badcase"]["status"] == "resolved"
    assert verify_response.json()["candidate_result"]["status"] == "passed"

    annotation_response = client.post(
        "/api/eval/annotations",
        json={
            "annotator": "reviewer-a",
            "badcase_id": badcases[0]["id"],
            "annotation": {"label": "confirmed", "medical_review": False},
        },
    )
    assert annotation_response.status_code == 201
    annotations = client.get(
        "/api/eval/annotations", params={"badcase_id": badcases[0]["id"]}
    ).json()["annotations"]
    assert len(annotations) == 2

    comparison_response = client.post(
        "/api/eval/runs/compare",
        json={
            "baseline_run_id": baseline["id"],
            "candidate_run_id": candidate["id"],
        },
    )
    assert comparison_response.status_code == 200
    comparison = comparison_response.json()
    assert comparison["fixed"] == ["api-policy-001"]
    assert comparison["regressions"] == []
    assert comparison["pass_rate_delta"] == 0.5

    markdown_response = client.get(
        f"/api/eval/runs/{baseline['id']}/report",
        params={"format": "markdown"},
    )
    assert markdown_response.status_code == 200
    assert markdown_response.headers["content-type"].startswith("text/markdown")
    assert "不替代医生" in markdown_response.text
    assert "api-policy-001" in markdown_response.text

    csv_response = client.get(
        f"/api/eval/runs/{baseline['id']}/report", params={"format": "csv"}
    )
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(StringIO(csv_response.text)))
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"passed", "failed"}

    listed_runs = client.get(
        "/api/eval/runs", params={"dataset_version_id": version["id"]}
    ).json()["runs"]
    assert {item["id"] for item in listed_runs} == {baseline["id"], candidate["id"]}
    dataset_runs = client.get(f"/api/eval/datasets/{dataset['id']}/runs").json()["runs"]
    assert {item["id"] for item in dataset_runs} == {baseline["id"], candidate["id"]}

    metrics_summary = client.get("/api/eval/metrics/summary")
    assert metrics_summary.status_code == 200
    assert metrics_summary.json()["run_count"] == 2
    prometheus = client.get("/api/eval/metrics/prometheus")
    assert prometheus.status_code == 200
    assert "agi_eval_latest_pass_rate" in prometheus.text

    demo = client.post("/api/eval/demo/bootstrap", json={"execute": False})
    assert demo.status_code == 201, demo.text
    assert demo.json()["synthetic"] is True
    assert demo.json()["dataset_version"]["case_count"] == 12


def test_fastapi_uses_injected_service_and_maps_lookup_errors_to_404(api_client):
    client, injected_service = api_client

    assert client.app.state.evaluation_service is injected_service
    response = client.get("/api/eval/runs/not-found")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_eval_readiness_separates_fixture_replay_from_local_live(live_api_client):
    client, _, _ = live_api_client

    response = client.get("/api/eval/readiness")

    assert response.status_code == 200, response.text
    readiness = response.json()
    assert set(readiness) >= {"replay", "live", "dependencies"}
    expected_replay = {
        "ready": True,
        "execution_mode": "synthetic_replay",
        "invokes_model": False,
        "invokes_tools": False,
        "invokes_rag": False,
    }
    assert {
        key: readiness["replay"][key] for key in expected_replay
    } == expected_replay
    assert readiness["live"]["ready"] is True
    assert readiness["live"]["execution_mode"] == "local_agent"
    assert readiness["live"]["llm_mode"] == "real"
    assert isinstance(readiness["live"]["warnings"], list)
    assert readiness["replay"]["execution_mode"] != readiness["live"]["execution_mode"]


def test_live_demo_uses_local_adapter_and_never_consumes_replay_profiles(
    live_api_client,
):
    client, service, agent = live_api_client

    response = client.post("/api/eval/demo/live")

    assert response.status_code == 201, response.text
    live = response.json()
    assert live["synthetic"] is False
    assert live["synthetic_inputs"] is True
    assert live["execution"]["synthetic_execution"] is False
    assert live["execution"]["execution_mode"] == "local_agent"
    run = live["run"]
    assert run["config"]["adapter"]["type"] == "local"
    assert "profile" not in run["config"]["adapter"]
    assert run["metadata"]["execution_mode"] == "local_agent"

    cases = service.store.get_version_cases(live["dataset_version"]["id"])
    assert all("outputs" in item["payload"]["metadata"] for item in cases)

    completed = _wait_for_completion(client, run["id"])
    assert completed["status"] == "completed"
    assert agent.calls
    results = service.store.list_case_results(run["id"])
    assert results
    assert all(item["status"] != "error" for item in results)
    assert all(
        item["output"]["agent_output"]["content"].startswith("LOCAL-LIVE:")
        for item in results
    )
