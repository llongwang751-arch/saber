from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from internal.evaluation.strategy import manifest_sha256
from internal.experimentation.api import create_experiment_router
from internal.experimentation.service import ExperimentService
from internal.experimentation.store import ExperimentStore


TENANT = "tenant-api-a"
OTHER_TENANT = "tenant-api-b"
HMAC_SECRET = b"api-contract-secret-long-enough-for-production"
BASELINE_RUNTIME = {"rag": {"top_k": 3, "no_answer_threshold": 0.3}}


def _evidence(tenant_id: str, proposal_id: str):
    if tenant_id != TENANT:
        raise LookupError("offline proposal not found for tenant")
    manifest = {"runtime_overrides": {"rag": {"top_k": 6}}}
    _canonical, checksum = manifest_sha256(manifest)
    return {
        "tenant_id": tenant_id,
        "proposal": {
            "id": proposal_id,
            "status": "approved",
            "candidate_strategy_version_id": f"strategy-{proposal_id}",
            "scope": "offline_evaluation",
            "auto_activate": False,
        },
        "strategy": {
            "id": f"strategy-{proposal_id}",
            "source": "offline_eval",
            "manifest": manifest,
            "manifest_checksum": checksum,
        },
    }


@pytest.fixture()
def api(tmp_path: Path, runtime_identity_factory):
    store = ExperimentStore(
        f"sqlite+pysqlite:///{(tmp_path / 'online-api.db').as_posix()}"
    )
    service = ExperimentService(
        store,
        hmac_secret=HMAC_SECRET,
        traffic_provenance="production_authenticated",
        strategy_evidence_resolver=_evidence,
        production_evidence_ready=True,
        baseline_runtime_overrides=BASELINE_RUNTIME,
        runtime_identity=runtime_identity_factory("api-primary"),
    )
    app = FastAPI()

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.user = {
            "id": request.headers.get("x-test-user", "participant-a"),
            "tenant_id": request.headers.get("x-test-tenant", TENANT),
            "roles": [
                role
                for role in request.headers.get("x-test-roles", "participant").split(",")
                if role
            ],
        }
        return await call_next(request)

    app.state.experiment_service = service
    app.include_router(create_experiment_router())
    with TestClient(app) as client:
        yield client, service
    service.close()


def _headers(
    user: str,
    roles: str,
    tenant: str = TENANT,
) -> dict[str, str]:
    return {
        "x-test-user": user,
        "x-test-roles": roles,
        "x-test-tenant": tenant,
    }


def _create_deployment(client: TestClient, *, actor: str = "admin-a") -> dict:
    response = client.post(
        "/api/online-experiments/deployments",
        headers=_headers(actor, "experiment_admin"),
        json={"proposal_id": "approved-api", "idempotency_key": "deploy-api"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_experiment(client: TestClient, deployment: dict, *, actor="creator-a") -> dict:
    response = client.post(
        "/api/online-experiments/experiments",
        headers=_headers(actor, "experiment_admin"),
        json={
            "name": "RAG production experiment",
            "candidate_deployment_id": deployment["id"],
            "baseline_rate": 0.1,
            "minimum_detectable_effect": 0.8,
            "enrollment_bps": 10000,
            "candidate_allocation_bps": 5000,
            "min_duration_hours": 1,
            "max_duration_hours": 24,
            "idempotency_key": "create-api-experiment",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _submit_approve_start(client: TestClient, experiment: dict) -> dict:
    submitted = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/submit",
        headers=_headers("admin-a", "experiment_admin"),
        json={
            "expected_generation": experiment["generation"],
            "idempotency_key": "submit-api",
        },
    )
    assert submitted.status_code == 200, submitted.text
    approved = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/decision",
        headers=_headers("approver-a", "experiment_approver"),
        json={
            "decision": "approve",
            "expected_generation": submitted.json()["generation"],
            "idempotency_key": "approve-api",
        },
    )
    assert approved.status_code == 200, approved.text
    started = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/start",
        headers=_headers("admin-a", "experiment_admin"),
        json={
            "expected_generation": approved.json()["generation"],
            "idempotency_key": "start-api",
            "target_status": "running",
        },
    )
    assert started.status_code == 200, started.text
    return started.json()


def test_readiness_and_management_routes_enforce_roles(api):
    client, _service = api

    readiness = client.get(
        "/api/online-experiments/readiness",
        headers=_headers("reader", "participant"),
    )
    assert readiness.status_code == 200
    assert readiness.json()["rbac"] == {
        "roles": ["participant"],
        "can_manage": False,
        "can_approve": False,
        "can_submit_feedback": True,
    }
    denied = client.post(
        "/api/online-experiments/deployments",
        headers=_headers("participant-a", "participant"),
        json={"proposal_id": "approved-api", "idempotency_key": "denied"},
    )
    assert denied.status_code == 403

    deployment = _create_deployment(client)
    assert deployment["tenant_id"] == TENANT
    assert deployment["created_by"] == "admin-a"


@pytest.mark.parametrize("forged_field", ["tenant_id", "actor", "roles"])
def test_api_rejects_body_identity_and_role_injection(api, forged_field):
    client, _service = api
    body = {
        "proposal_id": "approved-api",
        "idempotency_key": f"forged-{forged_field}",
        forged_field: "attacker-controlled",
    }

    response = client.post(
        "/api/online-experiments/deployments",
        headers=_headers("admin-a", "experiment_admin"),
        json=body,
    )

    assert response.status_code == 422


def test_creator_cannot_self_approve_and_roles_are_separated(api):
    client, _service = api
    deployment = _create_deployment(client)
    experiment = _create_experiment(client, deployment, actor="creator-a")

    participant_read = client.get(
        f"/api/online-experiments/experiments/{experiment['id']}",
        headers=_headers("participant-a", "participant"),
    )
    assert participant_read.status_code == 403
    submitted = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/submit",
        headers=_headers("admin-a", "experiment_admin"),
        json={"expected_generation": 0, "idempotency_key": "submit-rbac"},
    )
    assert submitted.status_code == 200
    self_review = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/decision",
        headers=_headers("creator-a", "experiment_approver"),
        json={
            "decision": "approve",
            "expected_generation": submitted.json()["generation"],
            "idempotency_key": "self-review",
        },
    )
    assert self_review.status_code == 400
    admin_review = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/decision",
        headers=_headers("admin-a", "experiment_admin"),
        json={
            "decision": "approve",
            "expected_generation": submitted.json()["generation"],
            "idempotency_key": "admin-review",
        },
    )
    assert admin_review.status_code == 403
    approved = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/decision",
        headers=_headers("approver-a", "experiment_approver"),
        json={
            "decision": "approve",
            "expected_generation": submitted.json()["generation"],
            "idempotency_key": "independent-review",
        },
    )
    assert approved.status_code == 200
    approver_start = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/start",
        headers=_headers("approver-a", "experiment_approver"),
        json={
            "expected_generation": approved.json()["generation"],
            "idempotency_key": "approver-start",
        },
    )
    assert approver_start.status_code == 403


def test_api_cas_idempotency_and_cross_tenant_lookup_fail_closed(api):
    client, _service = api
    experiment = _create_experiment(client, _create_deployment(client))
    submitted = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/submit",
        headers=_headers("admin-a", "experiment_admin"),
        json={"expected_generation": 0, "idempotency_key": "submit-cas"},
    )
    assert submitted.status_code == 200

    same_request = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/submit",
        headers=_headers("admin-a", "experiment_admin"),
        json={"expected_generation": 0, "idempotency_key": "submit-cas"},
    )
    assert same_request.status_code == 200
    assert same_request.json() == submitted.json()
    conflicting_request = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/submit",
        headers=_headers("admin-a", "experiment_admin"),
        json={
            "expected_generation": submitted.json()["generation"],
            "idempotency_key": "submit-cas",
            "reason": "different payload under reused key",
        },
    )
    assert conflicting_request.status_code == 409
    stale_generation = client.post(
        f"/api/online-experiments/experiments/{experiment['id']}/decision",
        headers=_headers("approver-a", "experiment_approver"),
        json={
            "decision": "approve",
            "expected_generation": 0,
            "idempotency_key": "stale-review",
        },
    )
    assert stale_generation.status_code == 409
    cross_tenant = client.get(
        f"/api/online-experiments/experiments/{experiment['id']}",
        headers=_headers("other-admin", "experiment_admin", OTHER_TENANT),
    )
    assert cross_tenant.status_code == 404


def test_feedback_api_derives_owner_and_arm_from_server_exposure(api):
    client, service = api
    experiment = _create_experiment(client, _create_deployment(client))
    _submit_approve_start(client, experiment)
    allocation = service.resolve_assignment(
        TENANT,
        "participant-a",
        "rag_chat",
        "turn-feedback",
        "trace-feedback",
        identity_context={
            "id": "participant-a",
            "tenant_id": TENANT,
            "roles": ["participant"],
            "identity_provenance": "operator_provisioned",
            "experiment_eligible": True,
            "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        },
    )
    assert allocation is not None
    allocation = {
        **allocation,
        "request_fingerprint": hashlib.sha256(b"api-feedback-request").hexdigest(),
    }
    exposure = service.begin_exposure(allocation)
    service.finish_exposure(
        exposure,
        status="completed",
        latency_ms=10,
        metrics={"completed": True},
    )

    response = client.put(
        f"/api/online-experiments/exposures/{exposure['opaque_exposure_id']}/feedback",
        headers=_headers("participant-a", "participant"),
        json={"event_id": "feedback-1", "rating": 1},
    )
    assert response.status_code == 200, response.text
    assert "arm" not in response.json()
    assert "user_id" not in response.json()
    evidence = service.store.evidence(TENANT, experiment["id"])
    recorded = next(
        item for item in evidence["outcomes"] if item["event_id"] == "feedback-1"
    )
    recorded_exposure = next(
        item
        for item in evidence["exposures"]
        if item["id"] == recorded["exposure_id"]
    )
    assert recorded_exposure["arm"] == exposure["arm"]
    forged_arm = client.put(
        f"/api/online-experiments/exposures/{exposure['opaque_exposure_id']}/feedback",
        headers=_headers("participant-a", "participant"),
        json={"event_id": "feedback-2", "rating": 1, "arm": "candidate"},
    )
    assert forged_arm.status_code == 422
    other_user = client.put(
        f"/api/online-experiments/exposures/{exposure['opaque_exposure_id']}/feedback",
        headers=_headers("participant-b", "participant"),
        json={"event_id": "feedback-3", "rating": -1},
    )
    assert other_user.status_code in {400, 404}


def test_analysis_api_never_claims_sqlite_as_production_evidence(api):
    client, service = api
    service.production_evidence_ready = False
    experiment = _create_experiment(client, _create_deployment(client))

    response = client.get(
        f"/api/online-experiments/experiments/{experiment['id']}/analysis",
        headers=_headers("approver-a", "experiment_approver"),
    )

    assert response.status_code == 200
    analysis = response.json()
    assert analysis["can_claim_effect"] is False
    assert analysis["winner"] is None
    assert analysis["outcome"]["effect"] is None
    assert analysis["outcome"]["ci_low"] is None
    assert analysis["outcome"]["ci_high"] is None
    assert analysis["outcome"]["p_value"] is None
    assert "production_evidence_store_not_ready" in analysis["claim_blockers"]
