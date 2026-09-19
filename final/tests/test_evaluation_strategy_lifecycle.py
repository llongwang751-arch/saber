from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from internal.evaluation.api import create_evaluation_router
from internal.evaluation.service import EvaluationService, EvaluationServiceRegistry
from internal.evaluation.store import EvalRunRecord, EvaluationStore
from internal.evaluation.strategy import MAX_STRATEGY_MANIFEST_BYTES


@pytest.fixture()
def service(tmp_path: Path):
    repository = EvaluationStore(
        f"sqlite+pysqlite:///{(tmp_path / 'strategy-lifecycle.db').as_posix()}"
    )
    value = EvaluationService(repository)
    try:
        yield value
    finally:
        value.close()


def _manifest(label: str) -> dict:
    return {
        "model": {"provider": "mock", "model_id": f"model-{label}"},
        "prompt": {"sha256": hashlib.sha256(f"prompt-{label}".encode()).hexdigest()},
        "rag": {"top_k": 5, "reranker": "deterministic"},
        "memory": {"policy": "tenant-scoped-v1"},
        "tools": [{"name": "farm_copilot", "schema_version": "v1"}],
        "knowledge_snapshot_id": "synthetic-none",
        "code_git_sha": "0" * 40,
    }


def _create_strategy(service: EvaluationService, label: str, creator: str = "creator-a"):
    return service.create_strategy_version(
        f"策略 {label}",
        _manifest(label),
        creator=creator,
    )


def _case(case_id: str, *, forbidden_severity: str | None = None) -> dict:
    expected = {
        "intents": ["target"],
        "required_content": ["正确"],
    }
    candidate_content = "正确"
    if forbidden_severity:
        marker = f"unsafe-{forbidden_severity.casefold()}"
        expected["forbidden_content"] = [
            {
                "rule_id": f"forbidden-{forbidden_severity.casefold()}",
                "pattern": marker,
                "severity": forbidden_severity,
            }
        ]
        candidate_content += f" {marker}"
    return {
        "case_id": case_id,
        "scenario": f"离线候选策略用例 {case_id}",
        "turns": [{"role": "user", "content": case_id}],
        "expected": expected,
        "risk_tags": [forbidden_severity] if forbidden_severity else [],
        "metadata": {
            "synthetic": True,
            "data_policy": "synthetic-only",
            "outputs": {
                "baseline": {
                    "intent": "wrong",
                    "content": "错误",
                    "trace": [
                        {"sequence": 0, "event_type": "intent_predicted"},
                        {"sequence": 1, "event_type": "final_response"},
                    ],
                },
                "candidate": {
                    "intent": "target",
                    "content": candidate_content,
                    "trace": [
                        {"sequence": 0, "event_type": "intent_predicted"},
                        {"sequence": 1, "event_type": "final_response"},
                    ],
                },
            },
        },
    }


def _dataset_version(
    service: EvaluationService,
    label: str,
    *,
    forbidden_severity: str | None = None,
):
    dataset = service.create_dataset(
        f"strategy-{label}",
        metadata={"data_policy": "synthetic-only", "data_source": "offline_eval"},
    )
    cases = [
        _case(
            f"{label}-{index:02d}",
            forbidden_severity=forbidden_severity if index == 0 else None,
        )
        for index in range(24)
    ]
    return service.import_cases(
        dataset["id"],
        cases,
        metadata={"data_policy": "synthetic-only", "data_source": "offline_eval"},
    )


def _run(
    service: EvaluationService,
    version_id: str,
    strategy_id: str,
    profile: str,
    *,
    release_gate: dict | None = None,
    metadata: dict | None = None,
):
    run_metadata = {
        "data_source": "offline_eval",
        "data_policy": "synthetic-only",
        "execution_mode": "synthetic_replay",
        "synthetic_execution": True,
        "online_ab": False,
    }
    run_metadata.update(metadata or {})
    run = service.create_run(
        version_id,
        name=f"{profile}-{strategy_id[:8]}",
        adapter={"type": "replay", "profile": profile},
        release_gate=release_gate or {"minimum_pass_rate": 0.8},
        metadata=run_metadata,
        strategy_version_id=strategy_id,
    )
    return service.execute_run(run["id"])


def _ready_proposal(
    service: EvaluationService,
    label: str,
    baseline_strategy_id: str,
    candidate_strategy_id: str,
    *,
    creator: str,
):
    version = _dataset_version(service, label)
    baseline = _run(service, version["id"], baseline_strategy_id, "baseline")
    candidate = _run(service, version["id"], candidate_strategy_id, "candidate")
    proposal = service.create_promotion_proposal(
        baseline["id"],
        candidate["id"],
        creator=creator,
    )
    return proposal, baseline, candidate


def test_strategy_manifest_is_canonical_content_addressed_and_immutable(service):
    manifest = _manifest("canonical")
    reordered = dict(reversed(list(manifest.items())))

    created = service.create_strategy_version("候选策略", manifest, creator="alice")
    duplicate = service.create_strategy_version("重复导入", reordered, creator="bob")
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert created["created"] is True
    assert duplicate["created"] is False
    assert duplicate["id"] == created["id"]
    assert created["manifest_canonical_json"] == canonical
    assert created["manifest_checksum"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert created["source"] == "offline_eval"
    assert created["creator"] == "alice"
    assert created["created_at"] is not None

    created["manifest"]["rag"]["top_k"] = 999
    persisted = service.get_strategy_version(created["id"])
    assert persisted["manifest"]["rag"]["top_k"] == 5
    assert not hasattr(service, "update_strategy_version")


@pytest.mark.parametrize(
    "manifest",
    [
        {"nested": {"api_key": "literal-credential"}},
        {"items": [{"Authorization": "credential"}]},
        {"credentials": {"private-key": "literal-credential"}},
        {"config": {"password": "literal-credential"}},
        {"notes": "Bearer abcdefghijklmnopqrstuvwxyz"},
        {"notes": "Basic dXNlcjpwYXNzd29yZA=="},
        {"notes": "sk-abcdefghijklmnopqrstuvwxyz012345"},
        {"notes": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"},
        {"notes": "sessionid=abcdefghijklmno;"},
        {
            "notes": (
                "-----BEGIN PRIVATE KEY-----\n"
                "synthetic-placeholder-that-must-still-be-rejected\n"
                "-----END PRIVATE KEY-----"
            )
        },
    ],
)
def test_strategy_manifest_rejects_recursive_sensitive_keys_and_credential_values(
    service,
    manifest,
):
    with pytest.raises(ValueError, match="(?i)secret reference|credential|sensitive"):
        service.create_strategy_version(
            "不得持久化密钥",
            manifest,
            creator="security-test",
        )


def test_strategy_manifest_accepts_only_secret_references(service):
    created = service.create_strategy_version(
        "仅保存凭据引用",
        {
            "provider": {
                "api_key_env": "OPENAI_API_KEY",
                "authorization_ref": "vault://agent/auth-header",
                "private_key_reference": "kms://agent/signing-key",
                "cookie_secret_name": "agent-session-cookie",
            }
        },
        creator="security-test",
    )

    assert created["created"] is True
    assert created["manifest"]["provider"]["api_key_env"] == "OPENAI_API_KEY"


def test_strategy_manifest_has_a_utf8_byte_size_limit(service):
    manifest = {"prompt_text": "猪" * MAX_STRATEGY_MANIFEST_BYTES}

    with pytest.raises(ValueError, match="(?i)exceeds.*bytes"):
        service.create_strategy_version(
            "超大策略快照",
            manifest,
            creator="security-test",
        )


def test_run_can_be_bound_only_to_an_existing_strategy_version(service):
    version = _dataset_version(service, "run-binding")
    strategy = _create_strategy(service, "bound")

    run = _run(service, version["id"], strategy["id"], "candidate")

    assert run["strategy_version_id"] == strategy["id"]
    assert service.store.get_run(run["id"])["strategy_version_id"] == strategy["id"]
    with pytest.raises(LookupError, match="(?i)strategy|策略"):
        service.create_run(
            version["id"],
            adapter={"type": "replay", "profile": "candidate"},
            strategy_version_id="missing-strategy-version",
        )


def test_ready_proposal_is_offline_only_and_never_auto_activates(service):
    baseline_strategy = _create_strategy(service, "ready-base")
    candidate_strategy = _create_strategy(service, "ready-candidate")

    proposal, _baseline, _candidate = _ready_proposal(
        service,
        "ready-proposal",
        baseline_strategy["id"],
        candidate_strategy["id"],
        creator="proposal-creator",
    )

    assert proposal["status"] == "proposed"
    assert proposal["ready_for_review"] is True
    assert proposal["blocked_reasons"] == []
    assert proposal["scope"] == "offline_evaluation"
    assert proposal["auto_activate"] is False
    assert proposal["candidate_strategy_version_id"] == candidate_strategy["id"]
    assert proposal["comparison"]["protocol"]["match"] is True
    assert proposal["statistics"]["status"] == "passed"
    assert proposal["statistics"]["mcnemar"]["passed"] is True
    assert proposal["statistics"]["paired_bootstrap"]["passed"] is True
    assert proposal["release_gate"]["passed"] is True
    assert proposal["safety"]["passed"] is True
    assert proposal["created_by"] == "proposal-creator"
    assert proposal["created_at"] is not None
    assert service.get_current_strategy()["current_strategy_version_id"] is None


@pytest.mark.parametrize("protocol_change", ["release_gate", "adapter_type"])
def test_protocol_manipulation_blocks_promotion_without_fabricating_statistics(
    service,
    protocol_change,
):
    baseline_strategy = _create_strategy(service, f"protocol-{protocol_change}-base")
    candidate_strategy = _create_strategy(
        service,
        f"protocol-{protocol_change}-candidate",
    )
    version = _dataset_version(service, f"protocol-{protocol_change}")
    baseline = _run(service, version["id"], baseline_strategy["id"], "baseline")
    candidate_gate = (
        {"minimum_pass_rate": 0.81}
        if protocol_change == "release_gate"
        else None
    )
    candidate = _run(
        service,
        version["id"],
        candidate_strategy["id"],
        "candidate",
        release_gate=candidate_gate,
    )
    if protocol_change == "adapter_type":
        # Simulate a persisted record being altered after execution.  A reviewer
        # must not compare Replay evidence with a different execution adapter.
        with service.store._transaction() as session:
            record = session.get(EvalRunRecord, candidate["id"])
            config = json.loads(json.dumps(record.config))
            config["adapter"]["type"] = "local"
            record.config = config

    proposal = service.create_promotion_proposal(
        baseline["id"],
        candidate["id"],
        creator="protocol-review-generator",
    )

    assert proposal["status"] == "blocked"
    assert proposal["ready_for_review"] is False
    assert "protocol_mismatch" in proposal["blocked_reasons"]
    assert proposal["comparison"]["protocol"]["match"] is False
    assert proposal["statistics"] == proposal["comparison"]["statistics"]
    assert proposal["statistics"]["mcnemar"]["n_pairs"] == 24
    assert proposal["statistics"]["paired_bootstrap"]["n_pairs"] == 24
    with pytest.raises(ValueError, match="(?i)blocked|review|approve"):
        service.review_promotion_proposal(
            proposal["id"],
            decision="approve",
            reviewer="protocol-reviewer",
        )


@pytest.mark.parametrize("severity", ["S0", "S1"])
def test_s0_and_s1_candidate_failures_are_hard_blockers(service, severity):
    baseline_strategy = _create_strategy(service, f"{severity}-base")
    candidate_strategy = _create_strategy(service, f"{severity}-candidate")
    version = _dataset_version(service, f"safety-{severity}", forbidden_severity=severity)
    baseline = _run(service, version["id"], baseline_strategy["id"], "baseline")
    candidate = _run(service, version["id"], candidate_strategy["id"], "candidate")

    proposal = service.create_promotion_proposal(
        baseline["id"],
        candidate["id"],
        creator="safety-generator",
    )

    assert proposal["status"] == "blocked"
    assert proposal["ready_for_review"] is False
    assert proposal["blocked_reasons"]
    assert proposal["safety"]["passed"] is False
    assert proposal["safety"]["hard_gate_failures"] >= 1
    with pytest.raises(ValueError, match="(?i)blocked|approve|阻断|批准"):
        service.review_promotion_proposal(
            proposal["id"],
            decision="approve",
            reviewer="safety-reviewer",
        )


def test_creator_cannot_self_approve_and_approval_requires_explicit_activation(service):
    baseline_strategy = _create_strategy(service, "approval-base")
    candidate_strategy = _create_strategy(service, "approval-candidate")
    proposal, _baseline, _candidate = _ready_proposal(
        service,
        "approval",
        baseline_strategy["id"],
        candidate_strategy["id"],
        creator="candidate-generator",
    )

    with pytest.raises(ValueError, match="(?i)creator|review|self|创建|审批|自己"):
        service.review_promotion_proposal(
            proposal["id"],
            decision="approve",
            reviewer="candidate-generator",
        )

    approved = service.review_promotion_proposal(
        proposal["id"],
        decision="approve",
        reviewer="human-reviewer",
        note="离线证据已人工复核",
    )
    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == "human-reviewer"
    assert approved["review_note"] == "离线证据已人工复核"
    assert approved["reviewed_at"] is not None
    assert service.get_current_strategy()["current_strategy_version_id"] is None

    activated = service.activate_promotion_proposal(
        proposal["id"],
        actor="release-operator",
        note="显式激活已审批候选",
    )
    assert activated["idempotent"] is False
    assert activated["proposal"]["status"] == "activated"
    assert activated["proposal"]["activated_by"] == "release-operator"
    assert activated["proposal"]["activation_note"] == "显式激活已审批候选"
    assert activated["proposal"]["activated_at"] is not None
    assert activated["pointer"]["current_strategy_version_id"] == candidate_strategy["id"]
    assert activated["pointer"]["previous_strategy_version_id"] is None
    assert activated["audit"]["action"] == "activate"
    assert activated["audit"]["actor"] == "release-operator"
    assert activated["audit"]["created_at"] is not None


def test_rejected_proposal_cannot_be_activated(service):
    baseline_strategy = _create_strategy(service, "reject-base")
    candidate_strategy = _create_strategy(service, "reject-candidate")
    proposal, _baseline, _candidate = _ready_proposal(
        service,
        "reject",
        baseline_strategy["id"],
        candidate_strategy["id"],
        creator="generator-a",
    )

    rejected = service.review_promotion_proposal(
        proposal["id"],
        decision="reject",
        reviewer="reviewer-b",
        note="需要补充真实模型复测",
    )

    assert rejected["status"] == "rejected"
    assert rejected["reviewed_by"] == "reviewer-b"
    assert rejected["review_note"] == "需要补充真实模型复测"
    with pytest.raises(ValueError, match="(?i)approved|activate|批准|激活"):
        service.activate_promotion_proposal(
            proposal["id"],
            actor="release-operator",
        )


def _approve_and_activate(
    service: EvaluationService,
    label: str,
    baseline_strategy_id: str,
    candidate_strategy_id: str,
):
    proposal, _baseline, _candidate = _ready_proposal(
        service,
        label,
        baseline_strategy_id,
        candidate_strategy_id,
        creator=f"generator-{label}",
    )
    service.review_promotion_proposal(
        proposal["id"],
        decision="approve",
        reviewer=f"reviewer-{label}",
    )
    return service.activate_promotion_proposal(
        proposal["id"],
        actor=f"operator-{label}",
    )


def test_current_previous_and_rollback_are_audited_and_idempotent(service):
    base = _create_strategy(service, "pointer-base")
    first = _create_strategy(service, "pointer-first")
    second = _create_strategy(service, "pointer-second")
    _approve_and_activate(service, "pointer-one", base["id"], first["id"])
    _approve_and_activate(service, "pointer-two", first["id"], second["id"])

    before = service.get_current_strategy()
    assert before["scope"] == "offline_evaluation"
    assert before["current_strategy_version_id"] == second["id"]
    assert before["previous_strategy_version_id"] == first["id"]
    assert before["current_strategy"]["id"] == second["id"]
    assert before["previous_strategy"]["id"] == first["id"]

    first_result = service.rollback_strategy(
        actor="rollback-operator",
        idempotency_key="rollback-pointer-second-to-first",
        reason="离线门禁复核要求回退",
        expected_current_strategy_version_id=second["id"],
    )
    second_result = service.rollback_strategy(
        actor="rollback-operator",
        idempotency_key="rollback-pointer-second-to-first",
        reason="离线门禁复核要求回退",
        expected_current_strategy_version_id=second["id"],
    )

    assert first_result["idempotent"] is False
    assert first_result["pointer"]["current_strategy_version_id"] == first["id"]
    assert first_result["pointer"]["previous_strategy_version_id"] == second["id"]
    assert first_result["audit"]["action"] == "rollback"
    assert first_result["audit"]["actor"] == "rollback-operator"
    assert first_result["audit"]["idempotency_key"] == "rollback-pointer-second-to-first"
    assert first_result["audit"]["details"] == {
        "reason": "离线门禁复核要求回退",
        "before_current": second["id"],
        "before_previous": first["id"],
        "after_current": first["id"],
        "after_previous": second["id"],
    }
    assert first_result["audit"]["created_at"] is not None
    assert second_result["idempotent"] is True
    assert second_result["audit"]["id"] == first_result["audit"]["id"]
    for field in (
        "current_strategy_version_id",
        "previous_strategy_version_id",
        "generation",
        "updated_by",
    ):
        assert second_result["pointer"][field] == first_result["pointer"][field]
    first_updated = datetime.fromisoformat(first_result["pointer"]["updated_at"])
    second_updated = datetime.fromisoformat(second_result["pointer"]["updated_at"])
    if first_updated.tzinfo is None:
        first_updated = first_updated.replace(tzinfo=timezone.utc)
    if second_updated.tzinfo is None:
        second_updated = second_updated.replace(tzinfo=timezone.utc)
    assert second_updated == first_updated
    assert service.get_current_strategy()["current_strategy_version_id"] == first["id"]


def test_strategy_and_proposal_state_are_tenant_isolated(tmp_path: Path):
    registry = EvaluationServiceRegistry(root=tmp_path / "strategy-tenants")
    try:
        alice = registry.get("tenant-alice")
        bob = registry.get("tenant-bob")
        base = _create_strategy(alice, "tenant-base", creator="alice-generator")
        candidate = _create_strategy(
            alice,
            "tenant-candidate",
            creator="alice-generator",
        )
        proposal, _baseline, _candidate = _ready_proposal(
            alice,
            "tenant-ready",
            base["id"],
            candidate["id"],
            creator="alice-generator",
        )

        assert [item["id"] for item in alice.list_strategy_versions()]
        assert bob.list_strategy_versions() == []
        assert bob.list_promotion_proposals() == []
        with pytest.raises(LookupError):
            bob.get_strategy_version(candidate["id"])
        with pytest.raises(LookupError):
            bob.get_promotion_proposal(proposal["id"])
        assert bob.get_current_strategy()["current_strategy_version_id"] is None
    finally:
        registry.close()


def test_authenticated_tenant_registry_supports_real_two_person_review_and_rbac(
    tmp_path: Path,
):
    registry = EvaluationServiceRegistry(root=tmp_path / "shared-tenant-evaluation")
    tenant_id = "tenant-team"
    admin_service = registry.get(tenant_id, execution_user_id="admin-user")
    baseline_strategy = _create_strategy(
        admin_service, "tenant-http-base", creator="admin-user"
    )
    candidate_strategy = _create_strategy(
        admin_service, "tenant-http-candidate", creator="admin-user"
    )
    version = _dataset_version(admin_service, "tenant-http")
    baseline = _run(
        admin_service, version["id"], baseline_strategy["id"], "baseline"
    )
    candidate = _run(
        admin_service, version["id"], candidate_strategy["id"], "candidate"
    )

    app = FastAPI()

    @app.middleware("http")
    async def inject_identity(request: Request, call_next):
        actor = request.headers.get("x-test-actor", "participant-user")
        roles = {
            "admin-user": ["participant", "experiment_admin"],
            "approver-user": ["participant", "experiment_approver"],
            "participant-user": ["participant"],
        }.get(actor, ["participant"])
        request.state.user = {
            "id": actor,
            "username": actor,
            "tenant_id": request.headers.get("x-test-tenant", tenant_id),
            "roles": roles,
        }
        return await call_next(request)

    app.include_router(create_evaluation_router())
    app.state.evaluation_service_registry = registry
    app.state.auth_required = True
    try:
        with TestClient(app) as client:
            forbidden_create = client.post(
                "/api/eval/promotion-proposals",
                headers={"x-test-actor": "participant-user"},
                json={
                    "baseline_run_id": baseline["id"],
                    "candidate_run_id": candidate["id"],
                },
            )
            assert forbidden_create.status_code == 403

            created = client.post(
                "/api/eval/promotion-proposals",
                headers={"x-test-actor": "admin-user"},
                json={
                    "baseline_run_id": baseline["id"],
                    "candidate_run_id": candidate["id"],
                },
            )
            assert created.status_code == 201, created.text
            proposal = created.json()

            self_role_escalation = client.post(
                f"/api/eval/promotion-proposals/{proposal['id']}/decision",
                headers={"x-test-actor": "admin-user"},
                json={"decision": "approve", "note": "管理员不能代替审批人"},
            )
            assert self_role_escalation.status_code == 403

            approved = client.post(
                f"/api/eval/promotion-proposals/{proposal['id']}/decision",
                headers={"x-test-actor": "approver-user"},
                json={"decision": "approve", "note": "同租户独立审批"},
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["reviewed_by"] == "approver-user"

            cross_tenant = client.get(
                f"/api/eval/promotion-proposals/{proposal['id']}",
                headers={
                    "x-test-actor": "approver-user",
                    "x-test-tenant": "tenant-other",
                },
            )
            assert cross_tenant.status_code == 404

            activated = client.post(
                f"/api/eval/promotion-proposals/{proposal['id']}/activate",
                headers={"x-test-actor": "admin-user"},
                json={"note": "审批后由管理员显式激活"},
            )
            assert activated.status_code == 200, activated.text
            assert activated.json()["proposal"]["status"] == "activated"
    finally:
        registry.close()


def test_strategy_api_takes_actor_from_auth_and_labels_every_response_offline(service):
    app = FastAPI()

    @app.middleware("http")
    async def inject_test_identity(request: Request, call_next):
        actor = request.headers.get("x-test-actor", "anonymous")
        request.state.user = {"id": actor, "username": actor}
        return await call_next(request)

    app.include_router(create_evaluation_router())
    app.state.evaluation_service = service
    with TestClient(app) as client:
        created = client.post(
            "/api/eval/strategies",
            headers={"x-test-actor": "authenticated-creator"},
            json={"name": "API 离线策略", "manifest": _manifest("api")},
        )
        assert created.status_code == 201, created.text
        strategy = created.json()
        assert strategy["creator"] == "authenticated-creator"
        assert strategy["source"] == "offline_eval"

        fetched = client.get(f"/api/eval/strategies/{strategy['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["source"] == "offline_eval"
        listed = client.get("/api/eval/strategies")
        assert listed.status_code == 200
        assert listed.json()["strategies"][0]["source"] == "offline_eval"


def test_promotion_api_uses_authenticated_actors_and_never_auto_activates(service):
    baseline_strategy = _create_strategy(service, "api-proposal-base")
    candidate_strategy = _create_strategy(service, "api-proposal-candidate")
    version = _dataset_version(service, "api-proposal")
    baseline = _run(service, version["id"], baseline_strategy["id"], "baseline")
    candidate = _run(service, version["id"], candidate_strategy["id"], "candidate")
    app = FastAPI()

    @app.middleware("http")
    async def inject_test_identity(request: Request, call_next):
        actor = request.headers.get("x-test-actor", "anonymous")
        request.state.user = {"id": actor, "username": actor}
        return await call_next(request)

    app.include_router(create_evaluation_router())
    app.state.evaluation_service = service
    with TestClient(app) as client:
        spoofed = client.post(
            "/api/eval/promotion-proposals",
            headers={"x-test-actor": "real-creator"},
            json={
                "baseline_run_id": baseline["id"],
                "candidate_run_id": candidate["id"],
                "created_by": "spoofed-creator",
            },
        )
        assert spoofed.status_code == 422

        created = client.post(
            "/api/eval/promotion-proposals",
            headers={"x-test-actor": "real-creator"},
            json={
                "baseline_run_id": baseline["id"],
                "candidate_run_id": candidate["id"],
            },
        )
        assert created.status_code == 201, created.text
        proposal = created.json()
        assert proposal["created_by"] == "real-creator"
        assert proposal["scope"] == "offline_evaluation"
        assert proposal["statistics"]["scope"] == "offline_evaluation"
        assert proposal["auto_activate"] is False

        self_review = client.post(
            f"/api/eval/promotion-proposals/{proposal['id']}/decision",
            headers={"x-test-actor": "real-creator"},
            json={"decision": "approve", "note": "试图自批"},
        )
        assert self_review.status_code in {400, 409}

        spoofed_review = client.post(
            f"/api/eval/promotion-proposals/{proposal['id']}/decision",
            headers={"x-test-actor": "real-reviewer"},
            json={
                "decision": "approve",
                "note": "人工审批",
                "reviewer": "spoofed-reviewer",
            },
        )
        assert spoofed_review.status_code == 422

        approved = client.post(
            f"/api/eval/promotion-proposals/{proposal['id']}/decision",
            headers={"x-test-actor": "real-reviewer"},
            json={"decision": "approve", "note": "人工审批"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["reviewed_by"] == "real-reviewer"
        assert approved.json()["status"] == "approved"

        current_before = client.get("/api/eval/strategies/current")
        assert current_before.status_code == 200
        assert current_before.json()["scope"] == "offline_evaluation"
        assert current_before.json()["current_strategy_version_id"] is None

        spoofed_activation = client.post(
            f"/api/eval/promotion-proposals/{proposal['id']}/activate",
            headers={"x-test-actor": "real-operator"},
            json={"note": "显式激活", "actor": "spoofed-operator"},
        )
        assert spoofed_activation.status_code == 422

        activated = client.post(
            f"/api/eval/promotion-proposals/{proposal['id']}/activate",
            headers={"x-test-actor": "real-operator"},
            json={"note": "显式激活"},
        )
        assert activated.status_code == 200, activated.text
        payload = activated.json()
        assert payload["proposal"]["scope"] == "offline_evaluation"
        assert payload["proposal"]["auto_activate"] is False
        assert payload["pointer"]["scope"] == "offline_evaluation"
        assert payload["audit"]["actor"] == "real-operator"
