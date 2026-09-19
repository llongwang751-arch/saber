from __future__ import annotations

import inspect
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect as sqlalchemy_inspect, text, update

from internal.evaluation.strategy import manifest_sha256
from internal.experimentation.assignment import assign_variant
from internal.experimentation.models import OnlineStrategyDeploymentRecord
from internal.experimentation.service import ExperimentService
from internal.experimentation.store import (
    ExperimentConflictError,
    ExperimentStore,
    GenerationConflictError,
    IdempotencyConflictError,
)


TENANT = "tenant-alpha"
OTHER_TENANT = "tenant-beta"
HMAC_SECRET = b"p3-production-hmac-secret-that-is-long-enough"
BASELINE_RUNTIME = {"rag": {"top_k": 3, "no_answer_threshold": 0.3}}
ELIGIBLE_ACCOUNT_CREATED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def _strategy_evidence(tenant_id: str, proposal_id: str):
    if tenant_id != TENANT or proposal_id.startswith("missing"):
        raise LookupError("offline proposal not found for tenant")
    status = "activated" if proposal_id.startswith("activated") else "approved"
    if proposal_id.startswith(("blocked", "proposed", "rejected")):
        status = proposal_id.split("-", 1)[0]
    manifest = {
        "runtime_overrides": {
            "rag": {"top_k": 7, "no_answer_threshold": 0.42}
        },
        "description": "synthetic strategy metadata only",
    }
    _, checksum = manifest_sha256(manifest)
    strategy_id = f"strategy-{proposal_id}"
    if proposal_id.startswith("mismatch"):
        strategy_id = "different-strategy"
    if proposal_id.startswith("bad-checksum"):
        checksum = "0" * 64
    return {
        "tenant_id": tenant_id,
        "proposal": {
            "id": proposal_id,
            "status": status,
            "candidate_strategy_version_id": f"strategy-{proposal_id}",
            "scope": "offline_evaluation",
            "auto_activate": False,
        },
        "strategy": {
            "id": strategy_id,
            "source": "offline_eval",
            "manifest": manifest,
            "manifest_checksum": checksum,
        },
    }


@pytest.fixture()
def service_factory(tmp_path: Path, runtime_identity_factory):
    services: list[ExperimentService] = []
    runtime_identity = runtime_identity_factory("service-primary")

    def build(*, provenance: str = "production_authenticated"):
        clock = MutableClock()
        store = ExperimentStore(
            f"sqlite+pysqlite:///{(tmp_path / f'online-{len(services)}.db').as_posix()}"
        )
        service = ExperimentService(
            store,
            hmac_secret=HMAC_SECRET,
            traffic_provenance=provenance,
            strategy_evidence_resolver=_strategy_evidence,
            clock=clock,
            # This service suite models an authenticated durable evidence
            # backend.  The API suite separately proves that default SQLite
            # can never be presented as production evidence.
            production_evidence_ready=True,
            baseline_runtime_overrides=BASELINE_RUNTIME,
            runtime_identity=runtime_identity,
        )
        services.append(service)
        return service, clock

    yield build
    for service in services:
        service.close()


def _deployment(service: ExperimentService, proposal_id: str = "approved-main"):
    return service.create_deployment(
        TENANT,
        proposal_id,
        actor="experiment-admin",
        idempotency_key=f"deploy-{proposal_id}",
    )


def _experiment(
    service: ExperimentService,
    *,
    name: str = "RAG 线上实验",
    deployment: dict | None = None,
    creator: str = "experiment-creator",
    idempotency_key: str = "create-experiment",
    **overrides,
):
    deployment = deployment or _deployment(service)
    values = {
        "name": name,
        "candidate_deployment_id": deployment["id"],
        "baseline_rate": 0.10,
        "minimum_detectable_effect": 0.80,
        "candidate_allocation_bps": 5000,
        "enrollment_bps": 10000,
        "primary_metric": "positive_feedback",
        "min_duration_hours": 1,
        "max_duration_hours": 24,
        "attribution_window_hours": 168,
        "creator": creator,
        "idempotency_key": idempotency_key,
    }
    values.update(overrides)
    return service.create_experiment(TENANT, **values)


def _approve_and_start(service: ExperimentService, experiment: dict):
    submitted = service.submit_experiment(
        TENANT,
        experiment["id"],
        actor="experiment-admin",
        expected_generation=experiment["generation"],
        idempotency_key=f"submit-{experiment['id']}",
    )
    approved = service.review_experiment(
        TENANT,
        experiment["id"],
        decision="approve",
        actor="independent-approver",
        expected_generation=submitted["generation"],
        idempotency_key=f"approve-{experiment['id']}",
    )
    return service.start_experiment(
        TENANT,
        experiment["id"],
        actor="experiment-admin",
        expected_generation=approved["generation"],
        idempotency_key=f"start-{experiment['id']}",
        target_status="running",
    )


def _approve_without_start(service: ExperimentService, experiment: dict):
    submitted = service.submit_experiment(
        TENANT,
        experiment["id"],
        actor="experiment-admin",
        expected_generation=experiment["generation"],
        idempotency_key=f"submit-only-{experiment['id']}",
    )
    return service.review_experiment(
        TENANT,
        experiment["id"],
        decision="approve",
        actor="independent-approver",
        expected_generation=submitted["generation"],
        idempotency_key=f"approve-only-{experiment['id']}",
    )


def _allocation(
    service: ExperimentService,
    user_id: str,
    index: int,
):
    allocation = service.resolve_assignment(
        TENANT,
        user_id,
        "rag_chat",
        f"turn-{index}",
        f"trace-{index}",
        identity_context={
            "id": user_id,
            "tenant_id": TENANT,
            "roles": ["participant"],
            "identity_provenance": "operator_provisioned",
            "experiment_eligible": True,
            "created_at": ELIGIBLE_ACCOUNT_CREATED_AT,
        },
    )
    if allocation is None:
        return None
    fingerprint = hashlib.sha256(
        f"{user_id}\0turn-{index}\0trace-{index}".encode("utf-8")
    ).hexdigest()
    return {**allocation, "request_fingerprint": fingerprint}


def test_production_readiness_requires_frozen_baseline_and_verified_component_manifest(
    tmp_path: Path,
):
    service = ExperimentService(
        ExperimentStore(
            f"sqlite+pysqlite:///{(tmp_path / 'runtime-readiness.db').as_posix()}"
        ),
        hmac_secret=HMAC_SECRET,
        traffic_provenance="production_authenticated",
        strategy_evidence_resolver=_strategy_evidence,
        production_evidence_ready=True,
    )
    try:
        readiness = service.readiness()
        assert readiness["can_route"] is False
        assert "baseline_runtime_config_incomplete" in readiness["blockers"]
        assert "runtime_component_manifest_unverified" in readiness["blockers"]
        assert readiness["runtime_environment_fingerprint_configured"] is False
        assert readiness["runtime_component_manifest_verified"] is False
    finally:
        service.close()


@pytest.mark.parametrize("proposal_status", ["approved", "activated"])
def test_deployment_requires_approved_or_activated_immutable_p2_evidence(
    service_factory,
    proposal_status,
):
    service, _clock = service_factory()

    deployment = _deployment(service, f"{proposal_status}-evidence")

    assert deployment["source_proposal_id"] == f"{proposal_status}-evidence"
    assert deployment["source_strategy_version_id"] == (
        f"strategy-{proposal_status}-evidence"
    )
    assert deployment["compiled_overrides"] == {
        "rag": {"top_k": 7, "no_answer_threshold": 0.42}
    }
    assert deployment["source_manifest_checksum"]
    assert deployment["compiled_checksum"]


@pytest.mark.parametrize(
    "proposal_id",
    ["blocked-evidence", "proposed-evidence", "rejected-evidence", "mismatch-evidence", "bad-checksum-evidence"],
)
def test_deployment_rejects_unapproved_mismatched_or_tampered_evidence(
    service_factory,
    proposal_id,
):
    service, _clock = service_factory()

    with pytest.raises((LookupError, ValueError)):
        _deployment(service, proposal_id)

    with pytest.raises(LookupError):
        service.create_deployment(
            OTHER_TENANT,
            "approved-evidence",
            actor="experiment-admin",
            idempotency_key="cross-tenant-deployment",
        )


def test_experiment_preregistration_is_frozen_and_control_override_is_empty(
    service_factory,
):
    service, _clock = service_factory()
    deployment = _deployment(service)

    with pytest.raises(ValueError, match="control_overrides.*empty"):
        _experiment(
            service,
            deployment=deployment,
            name="伪造 control 配置",
            idempotency_key="nonempty-control",
            control_overrides={"rag": {"top_k": 1}},
        )

    created = _experiment(service, deployment=deployment)
    required_sample = created["required_sample_per_arm"]
    submitted = service.submit_experiment(
        TENANT,
        created["id"],
        actor="experiment-admin",
        expected_generation=0,
        idempotency_key="freeze-preregistration",
    )

    assert created["status"] == "draft"
    assert created["control_overrides"] == {}
    assert required_sample == 5
    assert submitted["required_sample_per_arm"] == required_sample
    assert submitted["preregistration_checksum"]
    assert submitted["traffic_provenance"] == "production_authenticated"


def test_lifecycle_enforces_independent_review_cas_and_idempotency(service_factory):
    service, clock = service_factory()
    created = _experiment(service)

    with pytest.raises(ValueError, match="status"):
        service.start_experiment(
            TENANT,
            created["id"],
            actor="experiment-admin",
            expected_generation=0,
            idempotency_key="start-before-review",
        )
    submitted = service.submit_experiment(
        TENANT,
        created["id"],
        actor="experiment-admin",
        expected_generation=0,
        idempotency_key="submit-lifecycle",
    )
    with pytest.raises(ValueError, match="creator"):
        service.review_experiment(
            TENANT,
            created["id"],
            decision="approve",
            actor="experiment-creator",
            expected_generation=submitted["generation"],
            idempotency_key="self-approval",
        )
    with pytest.raises(GenerationConflictError):
        service.review_experiment(
            TENANT,
            created["id"],
            decision="approve",
            actor="independent-approver",
            expected_generation=0,
            idempotency_key="stale-approval",
        )
    approved = service.review_experiment(
        TENANT,
        created["id"],
        decision="approve",
        actor="independent-approver",
        expected_generation=submitted["generation"],
        idempotency_key="approve-lifecycle",
    )
    running = service.start_experiment(
        TENANT,
        created["id"],
        actor="experiment-admin",
        expected_generation=approved["generation"],
        idempotency_key="start-lifecycle",
        target_status="running",
    )
    with pytest.raises(ValueError, match="pre-registered arm sample targets"):
        service.complete_experiment(
            TENANT,
            created["id"],
            actor="experiment-admin",
            expected_generation=running["generation"],
            idempotency_key="complete-before-fixed-stop",
        )
    paused = service.pause_experiment(
        TENANT,
        created["id"],
        actor="experiment-admin",
        expected_generation=running["generation"],
        idempotency_key="pause-lifecycle",
        reason="人工检查",
    )
    replay = service.pause_experiment(
        TENANT,
        created["id"],
        actor="experiment-admin",
        expected_generation=running["generation"],
        idempotency_key="pause-lifecycle",
        reason="人工检查",
    )

    assert paused["status"] == "paused"
    assert replay["id"] == paused["id"]
    assert replay["status"] == paused["status"]
    assert replay["generation"] == paused["generation"]
    assert replay["pause_reason"] == paused["pause_reason"]
    assert _allocation(service, "after-manual-pause", 8001) is None
    with pytest.raises(IdempotencyConflictError):
        service.pause_experiment(
            TENANT,
            created["id"],
            actor="experiment-admin",
            expected_generation=running["generation"],
            idempotency_key="pause-lifecycle",
            reason="篡改后的原因",
        )
    resumed = service.resume_experiment(
        TENANT,
        created["id"],
        actor="experiment-admin",
        expected_generation=paused["generation"],
        idempotency_key="resume-lifecycle",
    )
    clock.advance(hours=25)
    assert _allocation(service, "deadline-trigger", 8002) is None
    stopped = service.get_experiment(TENANT, created["id"])
    completed = service.complete_experiment(
        TENANT,
        created["id"],
        actor="experiment-admin",
        expected_generation=stopped["generation"],
        idempotency_key="complete-lifecycle",
    )
    assert completed["status"] == "completed"
    assert completed["completed_by"] == "experiment-admin"

    audit = service.list_audit_events(TENANT, experiment_id=created["id"])
    assert {item["actor"] for item in audit} >= {
        "experiment-creator",
        "experiment-admin",
        "independent-approver",
    }
    assert all(item["created_at"] is not None for item in audit)


def test_rejected_experiment_cannot_start_and_rollback_stops_assignments(service_factory):
    service, _clock = service_factory()
    rejected = _experiment(service, name="被拒实验", idempotency_key="create-reject")
    submitted = service.submit_experiment(
        TENANT,
        rejected["id"],
        actor="experiment-admin",
        expected_generation=0,
        idempotency_key="submit-reject",
    )
    rejected = service.review_experiment(
        TENANT,
        rejected["id"],
        decision="reject",
        actor="independent-approver",
        expected_generation=submitted["generation"],
        idempotency_key="reject-review",
    )
    with pytest.raises(ValueError, match="status"):
        service.start_experiment(
            TENANT,
            rejected["id"],
            actor="experiment-admin",
            expected_generation=rejected["generation"],
            idempotency_key="start-rejected",
        )

    running = _approve_and_start(
        service,
        _experiment(service, name="待回滚实验", idempotency_key="create-rollback"),
    )
    owner = "rollback-early-owner"
    exposure = service.begin_exposure(_allocation(service, owner, 9000))
    service.finish_exposure(exposure, status="completed", latency_ms=1)
    service.record_feedback(
        TENANT,
        owner,
        exposure["exposure_id"],
        "rollback-early-feedback",
        1,
    )
    rolled_back = service.rollback_experiment(
        TENANT,
        running["id"],
        actor="experiment-admin",
        expected_generation=running["generation"],
        idempotency_key="rollback-running",
        reason="人工回滚候选流量",
    )
    assert rolled_back["status"] == "rolled_back"
    assert _allocation(service, "after-rollback", 9001) is None
    analysis = service.analyze_experiment(TENANT, running["id"])
    assert analysis["analysis_blinded"] is True
    assert analysis["control"] is None
    assert analysis["candidate"] is None
    assert analysis["outcome"]["effect"] is None


@pytest.mark.parametrize(
    ("replacement_secret", "replacement_provenance"),
    [
        (b"a-different-production-secret-that-is-long-enough", "production_authenticated"),
        (HMAC_SECRET, "internal"),
    ],
)
def test_restart_with_different_key_or_provenance_fails_closed(
    service_factory,
    replacement_secret,
    replacement_provenance,
):
    original, clock = service_factory()
    approved = _approve_without_start(original, _experiment(original))
    replacement = ExperimentService(
        ExperimentStore(engine=original.store.engine),
        hmac_secret=replacement_secret,
        traffic_provenance=replacement_provenance,
        strategy_evidence_resolver=_strategy_evidence,
        clock=clock,
        production_evidence_ready=True,
        baseline_runtime_overrides=BASELINE_RUNTIME,
        runtime_identity=original._runtime_identity,
    )

    with pytest.raises((ExperimentConflictError, ValueError)):
        replacement.start_experiment(
            TENANT,
            approved["id"],
            actor="experiment-admin",
            expected_generation=approved["generation"],
            idempotency_key="start-after-config-drift",
            target_status="running",
        )

    running = original.start_experiment(
        TENANT,
        approved["id"],
        actor="experiment-admin",
        expected_generation=approved["generation"],
        idempotency_key="start-before-config-drift",
        target_status="running",
    )
    try:
        allocation = replacement.resolve_assignment(
            TENANT,
            "must-not-be-rebucketed",
            "rag_chat",
            "drift-turn",
            "drift-trace",
        )
    except (ExperimentConflictError, ValueError):
        allocation = None
    assert running["status"] == "running"
    assert allocation is None
    stopped = original.get_experiment(TENANT, running["id"])
    assert stopped["status"] == "safety_paused"
    assert stopped["paused_by"] == "system:integrity_monitor"
    assert any(
        item["action"] == "auto_integrity_pause"
        for item in original.list_audit_events(TENANT, experiment_id=running["id"])
    )


def test_preregistration_sql_tamper_fails_closed_and_is_audited(service_factory):
    service, _clock = service_factory()
    running = _approve_and_start(service, _experiment(service))
    with service.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE online_experiments "
                "SET required_sample_per_arm = required_sample_per_arm + 1 "
                "WHERE id = :experiment_id"
            ),
            {"experiment_id": running["id"]},
        )

    assert _allocation(service, "tamper-probe", 7001) is None
    stopped = service.get_experiment(TENANT, running["id"])
    assert stopped["status"] == "safety_paused"
    analysis = service.analyze_experiment(TENANT, running["id"])
    assert analysis["integrity"] == {
        "valid": False,
        "blocker": "preregistration_integrity_failed",
    }
    assert analysis["analysis_blinded"] is True
    assert analysis["outcome"]["control"] is None
    assert analysis["outcome"]["candidate"] is None


@pytest.mark.parametrize("tampered_from", ["pending_review", "safety_paused"])
def test_raw_sql_lifecycle_bypass_is_detected_and_safety_paused(
    service_factory, tampered_from
):
    service, _clock = service_factory()
    created = _experiment(
        service,
        name=f"审计链绕过 {tampered_from}",
        idempotency_key=f"create-chain-{tampered_from}",
    )
    if tampered_from == "pending_review":
        current = service.submit_experiment(
            TENANT,
            created["id"],
            actor="experiment-admin",
            expected_generation=created["generation"],
            idempotency_key="submit-chain-bypass",
        )
    else:
        current = _approve_and_start(service, created)
        exposure = service.begin_exposure(_allocation(service, "chain-safety", 7100))
        current = service.finish_exposure(
            exposure,
            status="completed",
            latency_ms=1,
            safety_events=[
                {
                    "severity": "S0",
                    "rule_id": "chain-safety-stop",
                    "trusted": True,
                    "source": "server_guardrail",
                }
            ],
        )["experiment"]
        assert current["status"] == "safety_paused"

    with service.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE online_experiments SET status='running' "
                "WHERE id=:experiment_id"
            ),
            {"experiment_id": created["id"]},
        )

    assert _allocation(service, f"chain-probe-{tampered_from}", 7101) is None
    stopped = service.get_experiment(TENANT, created["id"])
    assert stopped["status"] == "safety_paused"
    assert stopped["paused_by"] == "system:integrity_monitor"
    verification = service.store.verify_experiment_audit_chain(
        TENANT, created["id"]
    )
    assert verification["valid"] is False
    assert any(
        item["action"] == "auto_integrity_pause"
        and item["details"]["reason"] == "audit_chain_integrity_failed"
        for item in service.list_audit_events(TENANT, experiment_id=created["id"])
    )


def test_audit_chain_hashes_payload_and_current_lifecycle_head(service_factory):
    service, _clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            name="审计哈希链",
            idempotency_key="create-audit-chain",
        ),
    )
    verification = service.store.verify_experiment_audit_chain(TENANT, running["id"])
    assert verification["valid"] is True
    events = sorted(
        service.list_audit_events(TENANT, experiment_id=running["id"]),
        key=lambda item: item["chain_position"],
    )
    assert [item["chain_position"] for item in events] == list(
        range(1, len(events) + 1)
    )
    assert events[0]["previous_event_hash"] == ""
    assert all(len(item["event_hash"]) == 64 for item in events)
    assert all(
        event["previous_event_hash"] == events[index - 1]["event_hash"]
        for index, event in enumerate(events[1:], start=1)
    )

    with service.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE experiment_audit_events SET details=:details "
                "WHERE id=:event_id"
            ),
            {"event_id": events[1]["id"], "details": '{"tampered":true}'},
        )
    analysis = service.analyze_experiment(TENANT, running["id"])
    assert analysis["integrity"]["valid"] is False
    assert analysis["integrity"]["blocker"] == "audit_chain_integrity_failed"
    assert analysis["can_claim_effect"] is False


def test_deployment_sql_tamper_fails_closed_and_is_audited(service_factory):
    service, _clock = service_factory()
    deployment = _deployment(service, "approved-deployment-tamper")
    running = _approve_and_start(
        service,
        _experiment(
            service,
            deployment=deployment,
            idempotency_key="create-deployment-tamper",
        ),
    )
    with service.store.engine.begin() as connection:
        connection.execute(
            update(OnlineStrategyDeploymentRecord)
            .where(OnlineStrategyDeploymentRecord.id == deployment["id"])
            .values(compiled_overrides={"rag": {"top_k": 20}})
        )

    assert _allocation(service, "deployment-tamper-probe", 7002) is None
    stopped = service.get_experiment(TENANT, running["id"])
    assert stopped["status"] == "safety_paused"
    audit = service.list_audit_events(TENANT, experiment_id=running["id"])
    assert any(
        item["action"] == "auto_integrity_pause"
        and item["details"]["reason"] == "deployment_integrity_failed"
        for item in audit
    )


def test_runtime_environment_rotation_pauses_before_new_exposure(
    service_factory, runtime_identity_factory
):
    service, clock = service_factory()
    running = _approve_and_start(service, _experiment(service))
    replacement = ExperimentService(
        ExperimentStore(engine=service.store.engine),
        hmac_secret=HMAC_SECRET,
        traffic_provenance="production_authenticated",
        strategy_evidence_resolver=_strategy_evidence,
        clock=clock,
        production_evidence_ready=True,
        baseline_runtime_overrides=BASELINE_RUNTIME,
        runtime_identity=runtime_identity_factory("rotated-runtime"),
    )
    try:
        assert replacement.resolve_assignment(
            TENANT,
            "runtime-rotation-user",
            "rag_chat",
            "runtime-rotation-turn",
            "runtime-rotation-trace",
        ) is None
    finally:
        replacement.close()
    stopped = service.get_experiment(TENANT, running["id"])
    assert stopped["status"] == "safety_paused"
    assert stopped["pause_reason"] == "runtime_environment_integrity_failed"


def test_runtime_identity_provider_rechecks_manifest_and_fails_closed(
    tmp_path: Path,
    runtime_identity_factory,
):
    original_identity = runtime_identity_factory("provider-original")
    identity_box = [original_identity]
    service = ExperimentService(
        ExperimentStore(
            f"sqlite+pysqlite:///{(tmp_path / 'provider-drift.db').as_posix()}"
        ),
        hmac_secret=HMAC_SECRET,
        traffic_provenance="production_authenticated",
        strategy_evidence_resolver=_strategy_evidence,
        production_evidence_ready=True,
        baseline_runtime_overrides=BASELINE_RUNTIME,
        runtime_identity=original_identity,
        runtime_identity_provider=lambda: identity_box[0],
    )
    try:
        running = _approve_and_start(service, _experiment(service))
        identity_box[0] = runtime_identity_factory("provider-rotated")

        assert _allocation(service, "provider-drift", 7301) is None
        stopped = service.get_experiment(TENANT, running["id"])
        assert stopped["status"] == "safety_paused"
        assert stopped["pause_reason"] == "runtime_environment_integrity_failed"
        readiness = service.readiness()
        assert readiness["can_route"] is False
        assert "runtime_component_manifest_unverified" in readiness["blockers"]
        assert readiness["runtime_environment_fingerprint_configured"] is False
        assert readiness["runtime_identity_error_code"] == "runtime_identity_drift"
    finally:
        service.close()


def test_assignment_is_sticky_private_and_does_not_count_as_exposure(service_factory):
    service, _clock = service_factory()
    running = _approve_and_start(service, _experiment(service))
    raw_user_id = "raw-user-must-not-be-persisted"

    first = _allocation(service, raw_user_id, 1)
    second = _allocation(service, raw_user_id, 2)
    before_exposure = service.analyze_experiment(TENANT, running["id"])

    assert first is not None
    assert second is not None
    assert second["assignment_id"] == first["assignment_id"]
    assert second["arm"] == first["arm"]
    assert second["bucket"] == first["bucket"]
    assert before_exposure["real_exposure_count"] == 0
    assert before_exposure["analysis_blinded"] is True
    assert before_exposure["sample"]["total_exposed_unique_users"] == 0
    assert before_exposure["sample"]["control_unique_users"] is None
    assert before_exposure["sample"]["candidate_unique_users"] is None
    assert before_exposure["srm"]["status"] == "insufficient"

    columns = {
        item["name"]
        for item in sqlalchemy_inspect(service.store.engine).get_columns(
            "experiment_assignments"
        )
    }
    assert "user_id" not in columns
    assert raw_user_id not in repr(service.store.evidence(TENANT, running["id"]))
    assert len(first["subject_digest"]) == 64
    assert first["subject_digest"] != raw_user_id

    exposure = service.begin_exposure(first)
    assert len(exposure["runtime_strategy_checksum"]) == 64
    assert exposure["runtime_overrides"].get("rag", {}).get("top_k") in {3, 7}
    after_begin = service.analyze_experiment(TENANT, running["id"])
    assert after_begin["real_exposure_count"] == 1
    assert after_begin["sample"]["total_exposed_unique_users"] == 1
    duplicate = service.begin_exposure(first)
    assert exposure["status"] == "started"
    assert exposure["idempotent"] is False
    assert duplicate["exposure_id"] == exposure["exposure_id"]
    assert duplicate["idempotent"] is True
    conflicting_turn = {**first, "trace_id": "different-trace"}
    with pytest.raises(IdempotencyConflictError):
        service.begin_exposure(conflicting_turn)

    service.finish_exposure(exposure, status="completed", latency_ms=12.5)
    after_exposure = service.analyze_experiment(TENANT, running["id"])
    assert after_exposure["real_exposure_count"] == 1
    assert after_exposure["sample"]["total_exposed_unique_users"] == 1


def test_turn_id_cannot_be_reused_by_another_assignment(service_factory):
    service, _clock = service_factory()
    _approve_and_start(service, _experiment(service))
    first = service.resolve_assignment(
        TENANT,
        "turn-owner-a",
        "rag_chat",
        "shared-turn-id",
        "shared-trace-id",
        identity_context={
            "id": "turn-owner-a",
            "tenant_id": TENANT,
            "roles": ["participant"],
            "identity_provenance": "operator_provisioned",
            "experiment_eligible": True,
            "created_at": ELIGIBLE_ACCOUNT_CREATED_AT,
        },
    )
    second = service.resolve_assignment(
        TENANT,
        "turn-owner-b",
        "rag_chat",
        "shared-turn-id",
        "shared-trace-id",
        identity_context={
            "id": "turn-owner-b",
            "tenant_id": TENANT,
            "roles": ["participant"],
            "identity_provenance": "operator_provisioned",
            "experiment_eligible": True,
            "created_at": ELIGIBLE_ACCOUNT_CREATED_AT,
        },
    )
    first = {
        **first,
        "request_fingerprint": hashlib.sha256(b"owner-a-request").hexdigest(),
    }
    second = {
        **second,
        "request_fingerprint": hashlib.sha256(b"owner-b-request").hexdigest(),
    }
    first_exposure = service.begin_exposure(first)

    with pytest.raises(IdempotencyConflictError):
        service.begin_exposure(second)
    assert service.store.get_exposure(
        TENANT,
        first_exposure["exposure_id"],
    )["assignment_id"] == first["assignment_id"]


def test_exposure_rejects_assignment_from_a_different_experiment(service_factory):
    service, _clock = service_factory()
    first_running = _approve_and_start(
        service,
        _experiment(service, name="第一实验", idempotency_key="create-first"),
    )
    allocation = _allocation(service, "assignment-owner", 70)
    paused = service.pause_experiment(
        TENANT,
        first_running["id"],
        actor="experiment-admin",
        expected_generation=first_running["generation"],
        idempotency_key="pause-first-for-second",
    )
    assert paused["status"] == "paused"
    second_running = _approve_and_start(
        service,
        _experiment(service, name="第二实验", idempotency_key="create-second"),
    )
    forged = {
        **allocation,
        "experiment_id": second_running["id"],
        "turn_id": "forged-cross-experiment-turn",
        "trace_id": "forged-cross-experiment-trace",
    }

    with pytest.raises((ExperimentConflictError, ValueError)):
        service.begin_exposure(forged)


def test_feedback_is_owner_bound_arm_derived_idempotent_and_windowed(service_factory):
    service, clock = service_factory()
    running = _approve_and_start(service, _experiment(service))
    user_id = "feedback-owner"
    allocation = _allocation(service, user_id, 10)
    exposure = service.begin_exposure(allocation)
    service.finish_exposure(exposure, status="completed", latency_ms=10)

    assert "arm" not in inspect.signature(service.record_feedback).parameters
    with pytest.raises(LookupError):
        service.record_feedback(
            TENANT,
            "different-user",
            exposure["exposure_id"],
            "feedback-event",
            1,
        )
    created = service.record_feedback(
        TENANT,
        user_id,
        exposure["exposure_id"],
        "feedback-event",
        1,
    )
    duplicate = service.record_feedback(
        TENANT,
        user_id,
        exposure["exposure_id"],
        "feedback-event",
        1,
    )
    assert created["metric"] == "positive_feedback"
    assert created["value"] == 1.0
    assert created["included"] is True
    assert duplicate["id"] == created["id"]
    assert duplicate["idempotent"] is True
    with pytest.raises(IdempotencyConflictError):
        service.record_feedback(
            TENANT,
            user_id,
            exposure["exposure_id"],
            "feedback-event",
            -1,
        )
    with pytest.raises(LookupError):
        service.record_feedback(
            TENANT,
            "different-user",
            exposure["exposure_id"],
            "feedback-event",
            1,
        )

    late_allocation = _allocation(service, user_id, 11)
    late_exposure = service.begin_exposure(late_allocation)
    service.finish_exposure(late_exposure, status="completed", latency_ms=10)
    clock.advance(hours=169)
    late = service.record_feedback(
        TENANT,
        user_id,
        late_exposure["exposure_id"],
        "late-feedback",
        1,
    )
    assert late["included"] is False
    assert late["exclusion_reason"] == "outside_attribution_window"
    with pytest.raises(LookupError):
        service.record_feedback(
            OTHER_TENANT,
            user_id,
            exposure["exposure_id"],
            "cross-tenant-feedback",
            1,
        )


@pytest.mark.parametrize("terminal_status", ["error", "cancelled"])
def test_error_and_cancelled_exposures_count_for_srm_but_not_primary_outcome(
    service_factory,
    terminal_status,
):
    service, _clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            name=f"{terminal_status} exposure",
            idempotency_key=f"create-{terminal_status}-exposure",
        ),
    )
    user_id = f"{terminal_status}-owner"
    exposure = service.begin_exposure(_allocation(service, user_id, 75))
    service.finish_exposure(
        exposure,
        status=terminal_status,
        latency_ms=2,
    )

    analysis = service.analyze_experiment(TENANT, running["id"])
    assert analysis["real_exposure_count"] == 1
    assert analysis["sample"]["total_exposed_unique_users"] == 1
    outcome = service.record_feedback(
        TENANT,
        user_id,
        exposure["exposure_id"],
        f"feedback-{terminal_status}",
        1,
    )
    assert outcome["included"] is False
    assert outcome["exclusion_reason"] == f"exposure_status_{terminal_status}"
    after_feedback = service.analyze_experiment(TENANT, running["id"])
    assert after_feedback["sample"]["total_feedback_responders"] == 0
    assert after_feedback["sample"]["control_outcomes"] is None
    assert after_feedback["sample"]["candidate_outcomes"] is None


@pytest.mark.parametrize("severity", ["S0", "S1"])
def test_trusted_s0_s1_atomically_pause_and_stop_new_assignments(
    service_factory,
    severity,
):
    service, _clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            name=f"安全门禁 {severity}",
            idempotency_key=f"create-safety-{severity}",
        ),
    )
    exposure = service.begin_exposure(_allocation(service, f"unsafe-{severity}", 20))

    finished = service.finish_exposure(
        exposure,
        status="completed",
        latency_ms=3,
        safety_events=[
            {
                "severity": severity,
                "rule_id": f"trusted-{severity}",
                "trusted": True,
                "source": "server_guardrail",
                "evidence": {"trace_ref": "redacted-trace"},
            }
        ],
    )

    assert finished["safety_paused"] is True
    assert finished["experiment"]["status"] == "safety_paused"
    assert _allocation(service, "after-safety-pause", 21) is None
    audit = service.list_audit_events(TENANT, experiment_id=running["id"])
    safety_audit = next(item for item in audit if item["action"] == "auto_safety_pause")
    assert safety_audit["actor"] == "system:safety_monitor"
    assert safety_audit["created_at"] is not None


def test_trusted_safety_finish_failure_uses_independent_emergency_outbox(
    service_factory, monkeypatch
):
    service, _clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            name="安全写失败",
            idempotency_key="create-emergency-safety",
        ),
    )
    exposure = service.begin_exposure(_allocation(service, "emergency-owner", 7200))

    def fail_finish(*_args, **_kwargs):
        raise RuntimeError("injected terminal write failure")

    monkeypatch.setattr(service.store, "finish_exposure", fail_finish)
    with pytest.raises(RuntimeError, match="injected terminal write failure"):
        service.finish_exposure(
            exposure,
            status="completed",
            latency_ms=2,
            safety_events=[
                {
                    "severity": "S1",
                    "rule_id": "emergency-outbox-test",
                    "trusted": True,
                    "source": "server_evaluator",
                }
            ],
        )

    stopped = service.get_experiment(TENANT, running["id"])
    assert stopped["status"] == "safety_paused"
    assert stopped["paused_by"] == "system:safety_monitor"
    assert service.store.get_exposure(
        TENANT, exposure["exposure_id"]
    )["status"] == "started"
    with service.store.engine.connect() as connection:
        outbox = connection.execute(
            text(
                "SELECT status, exposure_id, failure_code "
                "FROM experiment_safety_outbox"
            )
        ).mappings().one()
    assert outbox == {
        "status": "pending",
        "exposure_id": exposure["exposure_id"],
        "failure_code": "RuntimeError",
    }
    assert any(
        item["action"] == "emergency_safety_pause"
        for item in service.list_audit_events(TENANT, experiment_id=running["id"])
    )


def test_duplicate_finish_cannot_add_late_safety_signal(service_factory):
    service, _clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            name="终态不可补写安全事件",
            idempotency_key="create-terminal-safety-replay",
        ),
    )
    exposure = service.begin_exposure(_allocation(service, "terminal-owner", 7201))
    service.finish_exposure(exposure, status="completed", latency_ms=2)
    replay = service.finish_exposure(
        exposure,
        status="completed",
        latency_ms=2,
        safety_events=[
            {
                "severity": "S0",
                "rule_id": "late-signal",
                "trusted": True,
                "source": "server_guardrail",
            }
        ],
    )
    assert replay["safety_paused"] is False
    assert service.get_experiment(TENANT, running["id"])["status"] == "running"
    assert not any(
        item["action"] in {"auto_safety_pause", "emergency_safety_pause"}
        for item in service.list_audit_events(TENANT, experiment_id=running["id"])
    )


def test_stale_started_exposure_blocks_inference_and_snapshots_are_deduplicated(
    service_factory,
):
    service, clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            name="陈旧曝光阻断",
            idempotency_key="create-stale-exposure",
            attribution_window_hours=1,
        ),
    )
    service.begin_exposure(_allocation(service, "stale-owner", 7202))
    clock.advance(hours=2)

    first = service.analyze_experiment(TENANT, running["id"])
    second = service.analyze_experiment(TENANT, running["id"])
    assert first == second
    assert "stale_started_exposure" in first["claim_blockers"]
    assert first["sample"]["stale_started_exposures"] == 1
    assert first["sample"]["sufficient"] is False
    assert first["can_claim_effect"] is False
    with service.store.engine.connect() as connection:
        snapshot_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM experiment_monitor_snapshots "
                "WHERE experiment_id=:experiment_id"
            ),
            {"experiment_id": running["id"]},
        ).scalar_one()
    assert snapshot_count == 1


@pytest.mark.parametrize(
    "event",
    [
        {
            "severity": "S2",
            "rule_id": "non-blocking-s2",
            "trusted": True,
            "source": "server_evaluator",
        },
        {
            "severity": "S0",
            "rule_id": "untrusted-client-claim",
            "trusted": False,
            "source": "server_guardrail",
        },
    ],
)
def test_s2_or_untrusted_safety_event_does_not_auto_pause(service_factory, event):
    service, _clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            name=f"非阻断安全 {event['rule_id']}",
            idempotency_key=f"create-{event['rule_id']}",
        ),
    )
    exposure = service.begin_exposure(_allocation(service, event["rule_id"], 30))
    finished = service.finish_exposure(
        exposure,
        status="completed",
        latency_ms=3,
        safety_events=[event],
    )

    assert finished["safety_paused"] is False
    assert finished["experiment"]["status"] == "running"
    assert service.get_experiment(TENANT, running["id"])["status"] == "running"


def test_service_analysis_cannot_claim_without_real_complete_sufficient_evidence(
    service_factory,
):
    service, clock = service_factory()
    running = _approve_and_start(
        service,
        _experiment(
            service,
            minimum_detectable_effect=0.60,
            attribution_window_hours=1,
        ),
    )

    empty = service.analyze_experiment(TENANT, running["id"])
    assert empty["real_exposure_count"] == 0
    assert empty["can_claim_effect"] is False
    assert empty["outcome"]["effect"] is None
    assert empty["outcome"]["ci_low"] is None
    assert empty["outcome"]["ci_high"] is None
    assert empty["outcome"]["p_value"] is None
    assert empty["winner"] is None

    users = {"control": [], "candidate": []}
    index = 0
    target = int(running["required_sample_per_arm"])
    while min(len(users["control"]), len(users["candidate"])) < target:
        user_id = f"analysis-user-{index}"
        arm = assign_variant(
            HMAC_SECRET,
            TENANT,
            running["id"],
            user_id,
            5000,
        ).arm
        if len(users[arm]) < target:
            users[arm].append(user_id)
        index += 1

    turn = 100
    for arm in ("control", "candidate"):
        for user_id in users[arm]:
            allocation = _allocation(service, user_id, turn)
            assert allocation["arm"] == arm
            exposure = service.begin_exposure(allocation)
            service.finish_exposure(exposure, status="completed", latency_ms=5)
            service.record_feedback(
                TENANT,
                user_id,
                exposure["exposure_id"],
                f"outcome-{turn}",
                1 if arm == "candidate" else -1,
            )
            turn += 1

    incomplete = service.analyze_experiment(TENANT, running["id"])
    assert incomplete["sample"]["sufficient"] is False
    assert incomplete["can_claim_effect"] is False
    assert incomplete["outcome"]["effect"] is None
    assert incomplete["winner"] is None

    clock.advance(hours=2)
    assert _allocation(service, "sample-stop-trigger", 9999) is None
    stopped = service.get_experiment(TENANT, running["id"])
    assert stopped["paused_by"] == "system:sample_target"
    assert any(
        item["action"] == "auto_sample_target_pause"
        and item["actor"] == "system:sample_target"
        for item in service.list_audit_events(TENANT, experiment_id=running["id"])
    )
    completed = service.complete_experiment(
        TENANT,
        running["id"],
        actor="experiment-admin",
        expected_generation=stopped["generation"],
        idempotency_key="complete-analysis",
    )
    assert completed["status"] == "completed"
    analysis = service.analyze_experiment(TENANT, running["id"])
    assert analysis["traffic_provenance"] == "production_authenticated"
    assert analysis["sample"]["sufficient"] is True
    assert analysis["srm"]["status"] == "pass"
    assert analysis["can_claim_effect"] is True
    assert analysis["winner"] == "candidate"
    assert analysis["outcome"]["effect"] == pytest.approx(1.0)
    assert analysis["outcome"]["ci_low"] > 0.0
    assert analysis["outcome"]["p_value"] < 0.05


def test_internal_traffic_never_produces_effect_numbers_even_if_completed(
    service_factory,
):
    service, clock = service_factory(provenance="internal")
    running = _approve_and_start(service, _experiment(service))
    for index, rating in enumerate((-1, 1)):
        allocation = _allocation(service, f"internal-{index}", 500 + index)
        exposure = service.begin_exposure(allocation)
        service.finish_exposure(exposure, status="completed", latency_ms=1)
        service.record_feedback(
            TENANT,
            f"internal-{index}",
            exposure["exposure_id"],
            f"internal-feedback-{index}",
            rating,
        )
    clock.advance(hours=25)
    assert _allocation(service, "internal-deadline", 9998) is None
    stopped = service.get_experiment(TENANT, running["id"])
    service.complete_experiment(
        TENANT,
        running["id"],
        actor="experiment-admin",
        expected_generation=stopped["generation"],
        idempotency_key="complete-internal",
    )

    analysis = service.analyze_experiment(TENANT, running["id"])
    assert analysis["traffic_provenance"] == "internal"
    assert analysis["has_real_traffic"] is False
    assert analysis["real_exposure_count"] == 0
    assert analysis["can_claim_effect"] is False
    assert analysis["outcome"]["effect"] is None
    assert analysis["outcome"]["ci_low"] is None
    assert analysis["outcome"]["ci_high"] is None
    assert analysis["outcome"]["p_value"] is None
    assert analysis["winner"] is None
