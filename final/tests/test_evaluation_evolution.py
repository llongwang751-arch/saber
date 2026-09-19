from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from internal.evaluation.api import create_evaluation_router
from internal.evaluation.evolution import validate_suggestion_manifest
from internal.evaluation.service import EvaluationService, EvaluationServiceRegistry
from internal.evaluation.store import (
    CaseRunRecord,
    EvaluationStore,
    EvolutionAuditEventRecord,
    EvolutionSuggestionRecord,
    ImmutableEvolutionSuggestionError,
    StrategyVersionRecord,
)


@pytest.fixture()
def service(tmp_path: Path):
    value = EvaluationService(
        EvaluationStore(
            f"sqlite+pysqlite:///{(tmp_path / 'controlled-evolution.db').as_posix()}"
        )
    )
    try:
        yield value
    finally:
        value.close()


def _case(case_id: str, *, answerable: bool) -> dict:
    return {
        "case_id": case_id,
        "scenario": f"RAG 受控建议 {case_id}",
        "turns": [{"role": "user", "content": "合成评测输入"}],
        "expected": {
            "answerable": answerable,
            "evidence_ids": ["evidence-1"] if answerable else [],
        },
        "metadata": {"synthetic": True},
    }


def _completed_rag_run(service: EvaluationService, name: str = "evolution") -> dict:
    strategy = service.create_strategy_version(
        f"{name}-source-strategy",
        {
            "runtime_overrides": {
                "rag": {"top_k": 5, "no_answer_threshold": 0.5}
            }
        },
        creator="strategy-owner",
    )
    dataset = service.create_dataset(
        f"{name}-dataset",
        metadata={"data_policy": "synthetic-only", "synthetic": True},
    )
    version = service.import_cases(
        dataset["id"],
        [
            _case("retrieval-miss", answerable=True),
            _case("unsafe-answer", answerable=False),
        ],
        metadata={"data_policy": "synthetic-only", "synthetic": True},
    )
    run = service.create_run(
        version["id"],
        name=name,
        strategy_version_id=strategy["id"],
        adapter={"type": "replay", "profile": "baseline"},
        metadata={
            "data_source": "synthetic_fixture",
            "execution_mode": "synthetic_replay",
            "synthetic_execution": True,
        },
    )
    service.store.save_case_result(
        run["id"],
        "retrieval-miss",
        status="failed",
        passed=False,
        output={"abstained": True, "evidence_ids": []},
        metrics={
            "passed": False,
            "overall_score": 0.25,
            "metrics": [
                {"name": "rag_recall_at_k", "passed": False, "score": 0.0}
            ],
        },
        badcase_category="RETRIEVAL",
    )
    service.store.save_case_result(
        run["id"],
        "unsafe-answer",
        status="failed",
        passed=False,
        output={"abstained": False, "content": "无依据回答"},
        metrics={
            "passed": False,
            "overall_score": 0.25,
            "metrics": [
                {"name": "no_answer_decision", "passed": False, "score": 0.0}
            ],
        },
        badcase_category="RETRIEVAL",
    )
    return service.store.update_run(
        run["id"],
        status="completed",
        summary={
            "case_count": 2,
            "pass_rate": 0.0,
            "release_gate_passed": False,
        },
    )


def _suggestion(service: EvaluationService, run: dict, *, actor: str = "alice") -> dict:
    return service.create_evolution_suggestion(
        run["id"], actor=actor, idempotency_key=f"create-{run['id']}"
    )


def test_deterministic_suggestion_is_truth_labelled_and_never_auto_applies(service):
    run = _completed_rag_run(service)
    strategy_count = len(service.list_strategy_versions())
    proposal_count = len(service.list_promotion_proposals())

    suggestion = _suggestion(service, run)

    assert suggestion["status"] == "proposed"
    assert suggestion["generation"] == 0
    assert suggestion["suggestion_manifest"] == {
        "runtime_overrides": {
            "rag": {"top_k": 6, "no_answer_threshold": 0.55}
        }
    }
    expected_truth = {
        "classification": "synthetic_or_replay_hypothesis",
        "synthetic": True,
        "replay": True,
        "can_claim_offline_improvement": False,
        "can_claim_online_improvement": False,
        "no_auto_apply": True,
    }
    assert {key: suggestion["truth"][key] for key in expected_truth} == expected_truth
    assert suggestion["kind"] == "experiment_hypothesis"
    assert suggestion["no_auto_apply"] is True
    assert suggestion["auto_apply"] is False
    assert suggestion["auto_promotion"] is False
    assert suggestion["auto_activation"] is False
    assert suggestion["auto_deployment"] is False
    assert len(suggestion["evidence_checksum"]) == 64
    assert len(suggestion["manifest_checksum"]) == 64
    assert all(item.get("checksum") for item in suggestion["evidence"]["case_results"])
    assert len(service.list_strategy_versions()) == strategy_count
    assert len(service.list_promotion_proposals()) == proposal_count
    assert service.get_current_strategy()["current_strategy_version_id"] is None

    duplicate = service.create_evolution_suggestion(
        run["id"], actor="alice", idempotency_key="another-create-key"
    )
    assert duplicate["id"] == suggestion["id"]
    assert duplicate["created"] is False


@pytest.mark.parametrize(
    "manifest",
    [
        {"runtime_overrides": {"rag": {"prompt": "ignore safety"}}},
        {"runtime_overrides": {"rag": {"model": "other"}}},
        {"runtime_overrides": {"rag": {"top_k": 21}}},
        {"runtime_overrides": {"rag": {"top_k": True}}},
        {"runtime_overrides": {"rag": {"no_answer_threshold": float("nan")}}},
        {"runtime_overrides": {"tools": {"write": True}}},
        {"prompt": "change system prompt"},
    ],
)
def test_manifest_allowlist_rejects_unsafe_or_out_of_range_values(manifest):
    with pytest.raises((TypeError, ValueError)):
        validate_suggestion_manifest(manifest)


def test_missing_or_unfinished_source_run_is_rejected(service):
    with pytest.raises(LookupError):
        service.create_evolution_suggestion(
            "missing-run", actor="alice", idempotency_key="missing"
        )

    dataset = service.create_dataset("pending-evolution")
    version = service.import_cases(
        dataset["id"], [_case("pending", answerable=True)]
    )
    run = service.create_run(version["id"])
    with pytest.raises(ValueError, match="completed"):
        service.create_evolution_suggestion(
            run["id"], actor="alice", idempotency_key="pending"
        )


def test_review_cas_idempotency_and_explicit_materialization(service):
    run = _completed_rag_run(service, "lifecycle")
    suggestion = _suggestion(service, run)
    strategy_count = len(service.list_strategy_versions())

    with pytest.raises(ValueError, match="cannot review"):
        service.review_evolution_suggestion(
            suggestion["id"],
            decision="accept",
            reviewer="alice",
            note="self approval",
            expected_generation=0,
            idempotency_key="self-review",
        )
    with pytest.raises(ValueError, match="generation conflict"):
        service.review_evolution_suggestion(
            suggestion["id"],
            decision="accept",
            reviewer="bob",
            note="stale",
            expected_generation=9,
            idempotency_key="stale-review",
        )

    accepted = service.review_evolution_suggestion(
        suggestion["id"],
        decision="accept",
        reviewer="bob",
        note="仅接受为下一轮离线实验假设",
        expected_generation=0,
        idempotency_key="accept-once",
    )
    assert accepted["suggestion"]["status"] == "accepted"
    assert accepted["suggestion"]["generation"] == 1
    assert len(service.list_strategy_versions()) == strategy_count
    assert service.list_promotion_proposals() == []
    assert service.get_current_strategy()["current_strategy_version_id"] is None

    replay = service.review_evolution_suggestion(
        suggestion["id"],
        decision="accept",
        reviewer="bob",
        note="仅接受为下一轮离线实验假设",
        expected_generation=0,
        idempotency_key="accept-once",
    )
    assert replay["idempotent"] is True
    assert replay["suggestion"]["generation"] == 1
    with pytest.raises(ValueError, match="idempotency"):
        service.review_evolution_suggestion(
            suggestion["id"],
            decision="reject",
            reviewer="bob",
            note="different request",
            expected_generation=0,
            idempotency_key="accept-once",
        )

    materialized = service.materialize_evolution_suggestion(
        suggestion["id"],
        name="人工物化的 RAG 候选策略",
        actor="carol",
        expected_generation=1,
        idempotency_key="materialize-once",
    )
    assert materialized["strategy"]["source"] == "offline_eval"
    assert materialized["strategy"]["manifest"] == suggestion["suggestion_manifest"]
    assert materialized["suggestion"]["generation"] == 2
    assert materialized["promotion_proposal_created"] is False
    assert materialized["strategy_activated"] is False
    assert materialized["online_deployment_created"] is False
    assert len(service.list_strategy_versions()) == strategy_count + 1
    assert service.list_promotion_proposals() == []
    assert service.get_current_strategy()["current_strategy_version_id"] is None

    materialized_replay = service.materialize_evolution_suggestion(
        suggestion["id"],
        name="人工物化的 RAG 候选策略",
        actor="carol",
        expected_generation=1,
        idempotency_key="materialize-once",
    )
    assert materialized_replay["idempotent"] is True
    assert materialized_replay["strategy"]["id"] == materialized["strategy"]["id"]
    actions = {
        item["action"]
        for item in service.list_evolution_audit_events(
            suggestion_id=suggestion["id"]
        )
    }
    assert {"evolution_create", "evolution_review", "evolution_materialize"} <= actions


def test_rejected_suggestion_cannot_be_materialized(service):
    run = _completed_rag_run(service, "rejected")
    suggestion = _suggestion(service, run)
    rejected = service.review_evolution_suggestion(
        suggestion["id"],
        decision="reject",
        reviewer="bob",
        note="insufficient causal evidence",
        expected_generation=0,
        idempotency_key="reject-once",
    )
    assert rejected["suggestion"]["status"] == "rejected"
    with pytest.raises(ValueError, match="accepted"):
        service.materialize_evolution_suggestion(
            suggestion["id"],
            name="must not exist",
            actor="carol",
            expected_generation=1,
            idempotency_key="bad-materialize",
        )


def test_manifest_or_live_evidence_tampering_fails_closed(service):
    run = _completed_rag_run(service, "tamper-manifest")
    suggestion = _suggestion(service, run)
    with service.store.engine.begin() as connection:
        connection.execute(
            update(EvolutionSuggestionRecord)
            .where(EvolutionSuggestionRecord.id == suggestion["id"])
            .values(suggestion_manifest={"runtime_overrides": {"rag": {"top_k": 20}}})
        )
    with pytest.raises(ImmutableEvolutionSuggestionError, match="checksum"):
        service.get_evolution_suggestion(suggestion["id"])

    other = EvaluationService(EvaluationStore("sqlite:///:memory:"))
    try:
        other_run = _completed_rag_run(other, "tamper-evidence")
        other_suggestion = _suggestion(other, other_run)
        result = next(
            item
            for item in other.store.list_case_results(other_run["id"])
            if item["case_id"] == "retrieval-miss"
        )
        # Public APIs reject this rewrite.  Raw SQL models corruption or an
        # out-of-band writer and must still be caught by evidence reconstruction.
        with other.store.engine.begin() as connection:
            connection.execute(
                update(CaseRunRecord)
                .where(CaseRunRecord.id == result["id"])
                .values(
                    status="passed",
                    output={"abstained": False, "evidence_ids": ["evidence-1"]},
                    metrics={"passed": True, "overall_score": 1.0, "metrics": []},
                )
            )
        with pytest.raises(ValueError, match="evidence changed"):
            other.review_evolution_suggestion(
                other_suggestion["id"],
                decision="accept",
                reviewer="bob",
                note="stale evidence",
                expected_generation=0,
                idempotency_key="tampered-review",
            )
    finally:
        other.close()


def test_raw_sql_cannot_forge_reviewed_lifecycle_state(service):
    run = _completed_rag_run(service, "forged-review")
    suggestion = _suggestion(service, run)

    # Simulate an internal/raw-SQL caller bypassing the ORM state machine.
    with service.store.engine.begin() as connection:
        connection.execute(
            update(EvolutionSuggestionRecord)
            .where(EvolutionSuggestionRecord.id == suggestion["id"])
            .values(
                status="accepted",
                generation=1,
                reviewed_by="mallory",
                review_note="forged approval",
            )
        )

    with pytest.raises(ImmutableEvolutionSuggestionError, match="audit|state"):
        service.get_evolution_suggestion(suggestion["id"])
    with pytest.raises(ImmutableEvolutionSuggestionError, match="audit|state"):
        service.materialize_evolution_suggestion(
            suggestion["id"],
            name="must not materialize",
            actor="carol",
            expected_generation=1,
            idempotency_key="forged-materialize",
        )


def test_raw_sql_reviewer_or_audit_deletion_tampering_fails_closed(service):
    reviewer_run = _completed_rag_run(service, "forged-reviewer")
    reviewer_suggestion = _suggestion(service, reviewer_run)
    service.review_evolution_suggestion(
        reviewer_suggestion["id"],
        decision="accept",
        reviewer="bob",
        note="real approval",
        expected_generation=0,
        idempotency_key="real-review",
    )
    with service.store.engine.begin() as connection:
        connection.execute(
            update(EvolutionSuggestionRecord)
            .where(EvolutionSuggestionRecord.id == reviewer_suggestion["id"])
            .values(reviewed_by="mallory")
        )
    with pytest.raises(ImmutableEvolutionSuggestionError, match="state"):
        service.get_evolution_suggestion(reviewer_suggestion["id"])

    deleted_run = _completed_rag_run(service, "deleted-review-audit")
    deleted_suggestion = _suggestion(service, deleted_run)
    service.review_evolution_suggestion(
        deleted_suggestion["id"],
        decision="accept",
        reviewer="bob",
        note="review to be deleted",
        expected_generation=0,
        idempotency_key="review-before-delete",
    )
    with service.store.engine.begin() as connection:
        connection.execute(
            delete(EvolutionAuditEventRecord).where(
                EvolutionAuditEventRecord.suggestion_id == deleted_suggestion["id"],
                EvolutionAuditEventRecord.action == "evolution_review",
            )
        )
    with pytest.raises(ImmutableEvolutionSuggestionError, match="audit"):
        service.get_evolution_suggestion(deleted_suggestion["id"])


def test_raw_sql_audit_body_tampering_breaks_hash_chain(service):
    run = _completed_rag_run(service, "tampered-audit-body")
    suggestion = _suggestion(service, run)
    with service.store.engine.begin() as connection:
        connection.execute(
            update(EvolutionAuditEventRecord)
            .where(EvolutionAuditEventRecord.suggestion_id == suggestion["id"])
            .values(details={"request": {}, "state": {}})
        )
    with pytest.raises(ImmutableEvolutionSuggestionError, match="checksum"):
        service.get_evolution_suggestion(suggestion["id"])


def test_materialization_audit_and_strategy_reference_are_verified(service):
    run = _completed_rag_run(service, "materialize-audit")
    suggestion = _suggestion(service, run)
    service.review_evolution_suggestion(
        suggestion["id"],
        decision="accept",
        reviewer="bob",
        note="accept hypothesis only",
        expected_generation=0,
        idempotency_key="materialize-audit-review",
    )
    materialized = service.materialize_evolution_suggestion(
        suggestion["id"],
        name="verified candidate",
        actor="carol",
        expected_generation=1,
        idempotency_key="materialize-audit-create",
    )
    deleted_run = _completed_rag_run(service, "deleted-materialize-audit")
    deleted_suggestion = _suggestion(service, deleted_run)
    service.review_evolution_suggestion(
        deleted_suggestion["id"],
        decision="accept",
        reviewer="bob",
        note="valid review",
        expected_generation=0,
        idempotency_key="deleted-materialize-review",
    )
    service.materialize_evolution_suggestion(
        deleted_suggestion["id"],
        name="audit must remain",
        actor="carol",
        expected_generation=1,
        idempotency_key="deleted-materialize-create",
    )
    with service.store.engine.begin() as connection:
        connection.execute(
            delete(EvolutionAuditEventRecord).where(
                EvolutionAuditEventRecord.suggestion_id == deleted_suggestion["id"],
                EvolutionAuditEventRecord.action == "evolution_materialize",
            )
        )
    with pytest.raises(ImmutableEvolutionSuggestionError, match="audit"):
        service.get_evolution_suggestion(deleted_suggestion["id"])

    with service.store.engine.begin() as connection:
        connection.execute(
            update(StrategyVersionRecord)
            .where(StrategyVersionRecord.id == materialized["strategy"]["id"])
            .values(manifest={"runtime_overrides": {"rag": {"top_k": 20}}})
        )
    with pytest.raises(ImmutableEvolutionSuggestionError, match="strategy"):
        service.get_evolution_suggestion(suggestion["id"])


def test_source_strategy_manifest_is_bound_into_live_evidence(service):
    run = _completed_rag_run(service, "source-strategy-evidence")
    suggestion = _suggestion(service, run)
    source = suggestion["evidence"]["strategy_version"]
    assert source["id"] == run["strategy_version_id"]
    assert source["manifest_checksum"]
    assert source["checksum"]

    with service.store.engine.begin() as connection:
        connection.execute(
            update(StrategyVersionRecord)
            .where(StrategyVersionRecord.id == run["strategy_version_id"])
            .values(manifest={"runtime_overrides": {"rag": {"top_k": 20}}})
        )
    with pytest.raises(
        ImmutableEvolutionSuggestionError,
        match="source strategy manifest checksum",
    ):
        service.get_evolution_suggestion(suggestion["id"])
    with pytest.raises(
        ImmutableEvolutionSuggestionError,
        match="source strategy manifest checksum",
    ):
        service.review_evolution_suggestion(
            suggestion["id"],
            decision="accept",
            reviewer="bob",
            note="must fail on changed source strategy",
            expected_generation=0,
            idempotency_key="changed-source-strategy",
        )


def test_api_uses_server_actor_rejects_spoofing_and_enforces_review_separation(service):
    run = _completed_rag_run(service, "api")
    app = FastAPI()

    @app.middleware("http")
    async def inject_identity(request: Request, call_next):
        actor = request.headers.get("x-test-actor")
        if actor:
            request.state.user = {"id": actor, "username": actor}
        return await call_next(request)

    app.include_router(create_evaluation_router())
    app.state.evaluation_service = service
    with TestClient(app) as client:
        unauthenticated = client.post(
            "/api/eval/evolution-suggestions",
            json={"source_run_id": run["id"], "idempotency_key": "api-no-auth"},
        )
        assert unauthenticated.status_code == 401

        spoofed = client.post(
            "/api/eval/evolution-suggestions",
            headers={"x-test-actor": "alice"},
            json={
                "source_run_id": run["id"],
                "idempotency_key": "api-spoof",
                "actor": "mallory",
            },
        )
        assert spoofed.status_code == 422

        created = client.post(
            "/api/eval/evolution-suggestions",
            headers={"x-test-actor": "alice"},
            json={"source_run_id": run["id"], "idempotency_key": "api-create"},
        )
        assert created.status_code == 201, created.text
        suggestion = created.json()
        assert suggestion["created_by"] == "alice"

        spoofed_review = client.post(
            f"/api/eval/evolution-suggestions/{suggestion['id']}/decision",
            headers={"x-test-actor": "bob"},
            json={
                "decision": "accept",
                "expected_generation": 0,
                "idempotency_key": "api-review-spoof",
                "reviewer": "mallory",
            },
        )
        assert spoofed_review.status_code == 422

        self_review = client.post(
            f"/api/eval/evolution-suggestions/{suggestion['id']}/decision",
            headers={"x-test-actor": "alice"},
            json={
                "decision": "accept",
                "expected_generation": 0,
                "idempotency_key": "api-self-review",
            },
        )
        assert self_review.status_code == 400

        accepted = client.post(
            f"/api/eval/evolution-suggestions/{suggestion['id']}/decision",
            headers={"x-test-actor": "bob"},
            json={
                "decision": "accept",
                "note": "人工接受假设",
                "expected_generation": 0,
                "idempotency_key": "api-review",
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["suggestion"]["reviewed_by"] == "bob"

        listing = client.get(
            "/api/eval/evolution-suggestions",
            headers={"x-test-actor": "alice"},
        )
        assert listing.status_code == 200
        assert listing.json()["auto_apply"] is False
        assert listing.json()["suggestions"][0]["truth"]["replay"] is True


def test_api_enforces_admin_and_approver_roles_when_auth_is_required(service):
    run = _completed_rag_run(service, "rbac")
    app = FastAPI()

    @app.middleware("http")
    async def inject_identity(request: Request, call_next):
        actor = request.headers.get("x-test-actor", "participant")
        role = request.headers.get("x-test-role", "participant")
        request.state.user = {
            "id": actor,
            "username": actor,
            "tenant_id": "tenant-rbac",
            "roles": [role],
        }
        return await call_next(request)

    app.include_router(create_evaluation_router())
    app.state.evaluation_service = service
    app.state.auth_required = True
    with TestClient(app) as client:
        forbidden_create = client.post(
            "/api/eval/evolution-suggestions",
            json={"source_run_id": run["id"], "idempotency_key": "rbac-denied"},
        )
        assert forbidden_create.status_code == 403

        created = client.post(
            "/api/eval/evolution-suggestions",
            headers={
                "x-test-actor": "admin",
                "x-test-role": "experiment_admin",
            },
            json={"source_run_id": run["id"], "idempotency_key": "rbac-create"},
        )
        assert created.status_code == 201, created.text
        suggestion = created.json()

        wrong_role_review = client.post(
            f"/api/eval/evolution-suggestions/{suggestion['id']}/decision",
            headers={
                "x-test-actor": "admin-2",
                "x-test-role": "experiment_admin",
            },
            json={
                "decision": "accept",
                "expected_generation": 0,
                "idempotency_key": "rbac-wrong-review",
            },
        )
        assert wrong_role_review.status_code == 403

        approved = client.post(
            f"/api/eval/evolution-suggestions/{suggestion['id']}/decision",
            headers={
                "x-test-actor": "approver",
                "x-test-role": "experiment_approver",
            },
            json={
                "decision": "accept",
                "expected_generation": 0,
                "idempotency_key": "rbac-approve",
            },
        )
        assert approved.status_code == 200, approved.text

        forbidden_materialize = client.post(
            f"/api/eval/evolution-suggestions/{suggestion['id']}/materialize",
            headers={
                "x-test-actor": "approver",
                "x-test-role": "experiment_approver",
            },
            json={
                "expected_generation": 1,
                "idempotency_key": "rbac-denied-materialize",
            },
        )
        assert forbidden_materialize.status_code == 403

        materialized = client.post(
            f"/api/eval/evolution-suggestions/{suggestion['id']}/materialize",
            headers={
                "x-test-actor": "admin",
                "x-test-role": "experiment_admin",
            },
            json={
                "expected_generation": 1,
                "idempotency_key": "rbac-materialize",
            },
        )
        assert materialized.status_code == 200, materialized.text
        assert materialized.json()["online_deployment_created"] is False


def test_tenant_registry_shares_review_evidence_but_isolates_other_tenants(tmp_path):
    registry = EvaluationServiceRegistry(root=tmp_path / "tenant-eval")
    try:
        creator_service = registry.get("tenant-a", execution_user_id="alice")
        reviewer_service = registry.get("tenant-a", execution_user_id="bob")
        isolated_service = registry.get("tenant-b", execution_user_id="mallory")
        run = _completed_rag_run(creator_service, "tenant-shared")
        suggestion = _suggestion(creator_service, run, actor="alice")

        accepted = reviewer_service.review_evolution_suggestion(
            suggestion["id"],
            decision="accept",
            reviewer="bob",
            note="same tenant, different principal",
            expected_generation=0,
            idempotency_key="tenant-review",
        )
        assert accepted["suggestion"]["reviewed_by"] == "bob"
        with pytest.raises(LookupError):
            isolated_service.get_evolution_suggestion(suggestion["id"])
    finally:
        registry.close()


def test_http_registry_shares_tenant_evidence_across_users_and_isolates_tenants(
    tmp_path,
):
    registry = EvaluationServiceRegistry(root=tmp_path / "tenant-http-eval")
    creator_service = registry.get("tenant-a", execution_user_id="admin-a")
    run = _completed_rag_run(creator_service, "tenant-http-shared")
    app = FastAPI()

    @app.middleware("http")
    async def inject_identity(request: Request, call_next):
        actor = request.headers.get("x-test-actor", "participant")
        role = request.headers.get("x-test-role", "participant")
        tenant = request.headers.get("x-test-tenant", "tenant-a")
        request.state.user = {
            "id": actor,
            "username": actor,
            "tenant_id": tenant,
            "roles": [role],
        }
        return await call_next(request)

    app.include_router(create_evaluation_router())
    app.state.evaluation_service_registry = registry
    app.state.auth_required = True
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/eval/evolution-suggestions",
                headers={
                    "x-test-actor": "admin-a",
                    "x-test-role": "experiment_admin",
                    "x-test-tenant": "tenant-a",
                },
                json={
                    "source_run_id": run["id"],
                    "idempotency_key": "tenant-http-create",
                },
            )
            assert created.status_code == 201, created.text
            suggestion = created.json()

            participant_denied = client.get(
                f"/api/eval/evolution-suggestions/{suggestion['id']}",
                headers={
                    "x-test-actor": "reader-a",
                    "x-test-role": "participant",
                    "x-test-tenant": "tenant-a",
                },
            )
            assert participant_denied.status_code == 403
            visible = client.get(
                f"/api/eval/evolution-suggestions/{suggestion['id']}",
                headers={
                    "x-test-actor": "approver-a",
                    "x-test-role": "experiment_approver",
                    "x-test-tenant": "tenant-a",
                },
            )
            assert visible.status_code == 200, visible.text

            reviewed = client.post(
                f"/api/eval/evolution-suggestions/{suggestion['id']}/decision",
                headers={
                    "x-test-actor": "approver-a",
                    "x-test-role": "experiment_approver",
                    "x-test-tenant": "tenant-a",
                },
                json={
                    "decision": "accept",
                    "expected_generation": 0,
                    "idempotency_key": "tenant-http-review",
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["suggestion"]["reviewed_by"] == "approver-a"

            isolated = client.get(
                f"/api/eval/evolution-suggestions/{suggestion['id']}",
                headers={
                    "x-test-actor": "reader-b",
                    # Pass the role gate first so this assertion exercises
                    # tenant isolation rather than participant denial.
                    "x-test-role": "experiment_approver",
                    "x-test-tenant": "tenant-b",
                },
            )
            assert isolated.status_code == 404
    finally:
        registry.close()
