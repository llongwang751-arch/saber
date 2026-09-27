from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from internal.evaluation.api import create_evaluation_router
from internal.evaluation.service import EvaluationService, EvaluationServiceRegistry
from internal.evaluation.store import (
    EvaluationStore,
    ImmutableEvaluationEvidenceError,
)


TENANT = "tenant-evidence"


def _completed_pair(service: EvaluationService, label: str = "evidence"):
    manifest = {
        "runtime_overrides": {
            "rag": {"top_k": 7, "no_answer_threshold": 0.42}
        },
        "knowledge_snapshot_id": f"snapshot-{label}",
    }
    baseline_strategy = service.create_strategy_version(
        f"{label}-baseline-strategy", manifest, creator="admin"
    )
    candidate_strategy = service.create_strategy_version(
        f"{label}-candidate-strategy",
        {
            **manifest,
            "knowledge_snapshot_id": f"snapshot-{label}-candidate",
        },
        creator="admin",
    )
    dataset = service.store.create_dataset(f"{label}-dataset")
    version = service.store.import_dataset_version(
        dataset["id"],
        [{"case_id": f"{label}-{index:02d}"} for index in range(24)],
        metadata={"data_policy": "synthetic-only"},
    )
    metadata = {
        "data_source": "offline_eval",
        "data_policy": "synthetic-only",
        "execution_mode": "synthetic_replay",
        "synthetic_execution": True,
        "online_ab": False,
    }
    baseline = service.create_run(
        version["id"],
        name=f"{label}-baseline",
        adapter={"type": "replay", "profile": "baseline"},
        release_gate={"minimum_pass_rate": 0.8},
        metadata=metadata,
        strategy_version_id=baseline_strategy["id"],
    )
    candidate = service.create_run(
        version["id"],
        name=f"{label}-candidate",
        adapter={"type": "replay", "profile": "candidate"},
        release_gate={"minimum_pass_rate": 0.8},
        metadata=metadata,
        strategy_version_id=candidate_strategy["id"],
    )
    for case in service.store.get_version_cases(version["id"]):
        service.store.save_case_result(
            baseline["id"],
            case["id"],
            status="failed",
            output={"content": "wrong"},
            metrics={"passed": False, "overall_score": 0.0, "metrics": []},
            passed=False,
        )
        service.store.save_case_result(
            candidate["id"],
            case["id"],
            status="passed",
            output={"content": "correct"},
            metrics={"passed": True, "overall_score": 1.0, "metrics": []},
            passed=True,
        )
    baseline = service.store.update_run(
        baseline["id"],
        status="completed",
        summary={
            "pass_rate": 0.0,
            "error_rate": 0.0,
            "p95_latency_ms": 1.0,
            "hard_gate_failures": 0,
            "release_gate_passed": False,
        },
    )
    candidate = service.store.update_run(
        candidate["id"],
        status="completed",
        summary={
            "pass_rate": 1.0,
            "error_rate": 0.0,
            "p95_latency_ms": 1.0,
            "hard_gate_failures": 0,
            "release_gate_passed": True,
        },
    )
    return version, baseline, candidate


def test_terminal_run_and_case_result_cannot_be_cancelled_restarted_or_overwritten(
    tmp_path: Path,
):
    service = EvaluationService(
        EvaluationStore(
            f"sqlite+pysqlite:///{(tmp_path / 'terminal.db').as_posix()}"
        )
    )
    try:
        _version, _baseline, candidate = _completed_pair(service, "terminal")
        result = service.store.list_case_results(candidate["id"])[0]

        with pytest.raises(ImmutableEvaluationEvidenceError, match="immutable"):
            service.cancel_run(candidate["id"])
        with pytest.raises(ImmutableEvaluationEvidenceError, match="terminal"):
            service.start_run(candidate["id"])
        with pytest.raises(ImmutableEvaluationEvidenceError, match="immutable"):
            service.store.save_case_result(
                candidate["id"],
                result["eval_case_id"],
                status="failed",
                output={"content": "rewritten"},
                metrics={"passed": False, "overall_score": 0.0},
                passed=False,
            )
        unchanged = service.store.get_case_result(result["id"])
        assert unchanged["status"] == "passed"
        assert unchanged["output"] == {"content": "correct"}
    finally:
        service.close()


def test_two_services_share_one_database_but_only_one_worker_claims_run(
    tmp_path: Path,
):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'shared-cas.db').as_posix()}"
    first = EvaluationService(EvaluationStore(database_url))
    second = EvaluationService(EvaluationStore(database_url))
    release_worker = threading.Event()
    worker_entered = threading.Event()
    calls: list[str] = []
    calls_lock = threading.Lock()

    try:
        dataset = first.store.create_dataset("shared-cas")
        version = first.store.import_dataset_version(
            dataset["id"], [{"case_id": "case-1"}]
        )
        run = first.create_run(version["id"], name="one durable execution")

        def fake_worker(owner: str, service: EvaluationService):
            def execute(run_id: str, _cancel_event: threading.Event) -> None:
                with calls_lock:
                    calls.append(owner)
                worker_entered.set()
                assert release_worker.wait(timeout=5)
                service.store.update_run(
                    run_id,
                    status="completed",
                    summary={"total": 1, "completed": 1},
                )

            return execute

        first._run_worker = fake_worker("first", first)  # type: ignore[method-assign]
        second._run_worker = fake_worker("second", second)  # type: ignore[method-assign]
        callers_ready = threading.Barrier(2)

        def start(service: EvaluationService):
            callers_ready.wait(timeout=5)
            return service.start_run(run["id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, first), pool.submit(start, second)]
            assert worker_entered.wait(timeout=5)
            responses = [future.result(timeout=5) for future in futures]

        assert len(calls) == 1
        assert {item["status"] for item in responses} == {"running"}
        release_worker.set()
        deadline = time.time() + 5
        while first.store.get_run(run["id"])["status"] != "completed":
            assert time.time() < deadline
            time.sleep(0.01)
        assert len(calls) == 1
    finally:
        release_worker.set()
        first.close()
        second.close()


def test_production_evaluation_rbac_and_audit_actor_ignore_request_body(
    tmp_path: Path,
):
    registry = EvaluationServiceRegistry(root=tmp_path / "tenant-eval")
    admin_service = registry.get(TENANT, execution_user_id="admin-user")
    version, baseline, candidate = _completed_pair(admin_service, "rbac")
    badcase = admin_service.store.list_badcases(run_id=baseline["id"])[0]
    case_id = admin_service.store.get_version_cases(version["id"])[0]["id"]
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        actor = request.headers.get("x-actor", "participant-user")
        roles = {
            "admin-user": ["participant", "experiment_admin"],
            "approver-user": ["participant", "experiment_approver"],
        }.get(actor, ["participant"])
        request.state.user = {
            "id": actor,
            "username": actor,
            "tenant_id": TENANT,
            "roles": roles,
        }
        return await call_next(request)

    app.include_router(create_evaluation_router())
    app.state.evaluation_service_registry = registry
    app.state.auth_required = True
    try:
        with TestClient(app) as client:
            assert client.get("/api/eval/datasets").status_code == 403
            assert client.post(
                "/api/eval/datasets", json={"name": "forbidden"}
            ).status_code == 403
            assert client.post(
                f"/api/eval/runs/{candidate['id']}/execute"
            ).status_code == 403
            assert client.post(
                f"/api/eval/runs/{candidate['id']}/cancel"
            ).status_code == 403
            assert client.post(
                "/api/eval/annotations",
                json={
                    "annotator": "forged-approver",
                    "annotation": {"claim": "forged"},
                    "eval_case_id": case_id,
                },
            ).status_code == 403

            annotation = client.post(
                "/api/eval/annotations",
                headers={"x-actor": "admin-user"},
                json={
                    "annotator": "forged-approver",
                    "annotation": {"claim": "reviewed"},
                    "eval_case_id": case_id,
                },
            )
            assert annotation.status_code == 201, annotation.text
            assert annotation.json()["annotator"] == "admin-user"

            verified = client.post(
                f"/api/eval/badcases/{badcase['id']}/verify",
                headers={"x-actor": "approver-user"},
                json={
                    "candidate_run_id": candidate["id"],
                    "reviewer": "forged-reviewer",
                    "note": "independent verification",
                },
            )
            assert verified.status_code == 200, verified.text
            payload = verified.json()
            assert payload["annotation"]["annotator"] == "approver-user"
            assert payload["badcase"]["resolution"]["reviewer"] == "approver-user"
    finally:
        registry.close()
