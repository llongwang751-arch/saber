from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest

from internal.application.store import ApplicationStore
from internal.experimentation.audience import (
    AUDIENCE_POLICY_VERSION,
    attest_audience_snapshot,
    production_audience_decision,
    verify_audience_attestation,
)
from internal.evaluation.strategy import manifest_sha256
from internal.experimentation.service import ExperimentService
from internal.experimentation.store import ExperimentStore
from scripts.provision_experiment_user import provision_user


NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
SECRET = b"audience-attestation-secret-long-enough"


def _identity(**updates):
    value = {
        "tenant_id": "farm-a",
        "roles": ["participant"],
        "identity_provenance": "operator_provisioned",
        "experiment_eligible": True,
        "created_at": NOW - timedelta(days=30),
    }
    value.update(updates)
    return value


def _experiment():
    return {
        "id": "experiment-1",
        "submitted_at": NOW - timedelta(days=7),
        "audience_policy_version": AUDIENCE_POLICY_VERSION,
    }


def test_only_preexisting_operator_business_identity_is_eligible():
    eligible = production_audience_decision(
        _identity(), _experiment(), tenant_id="farm-a"
    )
    assert eligible["audience_eligible"] is True
    assert eligible["audience_exclusion_reasons"] == []

    cases = [
        _identity(identity_provenance="self_service"),
        _identity(experiment_eligible=False),
        _identity(roles=["participant", "experiment_admin"]),
        _identity(created_at=NOW),
        _identity(tenant_id="farm-b"),
    ]
    for identity in cases:
        decision = production_audience_decision(
            identity, _experiment(), tenant_id="farm-a"
        )
        assert decision["audience_eligible"] is False
        assert decision["audience_exclusion_reasons"]


def test_audience_attestation_binds_pseudonymous_assignment_and_snapshot():
    snapshot = production_audience_decision(
        _identity(), _experiment(), tenant_id="farm-a"
    )
    signature = attest_audience_snapshot(
        SECRET,
        snapshot,
        tenant_id="farm-a",
        experiment_id="experiment-1",
        assignment_id="assignment-1",
        subject_digest="a" * 64,
    )
    assert verify_audience_attestation(
        SECRET,
        snapshot,
        signature,
        tenant_id="farm-a",
        experiment_id="experiment-1",
        assignment_id="assignment-1",
        subject_digest="a" * 64,
    )
    assert not verify_audience_attestation(
        SECRET,
        {**snapshot, "audience_eligible": False},
        signature,
        tenant_id="farm-a",
        experiment_id="experiment-1",
        assignment_id="assignment-1",
        subject_digest="a" * 64,
    )
    assert not verify_audience_attestation(
        SECRET,
        snapshot,
        signature,
        tenant_id="farm-a",
        experiment_id="experiment-1",
        assignment_id="assignment-2",
        subject_digest="a" * 64,
    )


def test_application_identity_defaults_fail_closed_and_tenant_move_revokes(
    tmp_path: Path,
):
    store = ApplicationStore(
        f"sqlite+pysqlite:///{(tmp_path / 'identity.db').as_posix()}"
    )
    try:
        normal = store.create_user("normal", "hash", tenant_id="farm-a")
        assert normal["identity_provenance"] == "self_service"
        assert normal["experiment_eligible"] is False

        trusted = store.create_user(
            "business",
            "hash",
            tenant_id="farm-a",
            identity_provenance="operator_provisioned",
            experiment_eligible=True,
        )
        assert trusted["experiment_eligible"] is True
        moved = store.set_user_identity(trusted["id"], tenant_id="farm-b")
        assert moved["tenant_id"] == "farm-b"
        assert moved["experiment_eligible"] is False

        with pytest.raises(ValueError, match="管理员和审批人"):
            store.create_user(
                "admin",
                "hash",
                tenant_id="farm-a",
                roles=["experiment_admin"],
                identity_provenance="operator_provisioned",
                experiment_eligible=True,
            )
    finally:
        store.close()


def test_operator_provisioning_is_explicit_and_never_emits_a_password(
    tmp_path: Path,
):
    store = ApplicationStore(
        f"sqlite+pysqlite:///{(tmp_path / 'provision.db').as_posix()}"
    )
    try:
        answers = iter(["correct-horse-battery", "correct-horse-battery"])
        result = provision_user(
            store,
            username="farmworker",
            tenant_id="farm-a",
            experiment_eligible=True,
            create=True,
            password_reader=lambda _prompt: next(answers),
        )
        assert result["created"] is True
        assert result["roles"] == ["participant"]
        assert result["identity_provenance"] == "operator_provisioned"
        assert result["experiment_eligible"] is True
        assert "password" not in result

        moved = provision_user(
            store,
            username="farmworker",
            tenant_id="farm-b",
            experiment_eligible=False,
            create=False,
        )
        assert moved["created"] is False
        assert moved["tenant_id"] == "farm-b"
        assert moved["experiment_eligible"] is False
    finally:
        store.close()


def _strategy_evidence(tenant_id: str, proposal_id: str):
    manifest = {
        "runtime_overrides": {
            "rag": {"top_k": 5, "no_answer_threshold": 0.4}
        }
    }
    _, checksum = manifest_sha256(manifest)
    return {
        "tenant_id": tenant_id,
        "proposal": {
            "id": proposal_id,
            "status": "approved",
            "candidate_strategy_version_id": "strategy-audience",
        },
        "strategy": {
            "id": "strategy-audience",
            "source": "offline_eval",
            "manifest": manifest,
            "manifest_checksum": checksum,
        },
    }


def _active_service(tmp_path: Path, suffix: str, runtime_identity):
    service = ExperimentService(
        ExperimentStore(
            f"sqlite+pysqlite:///{(tmp_path / f'audience-{suffix}.db').as_posix()}"
        ),
        hmac_secret=SECRET,
        traffic_provenance="production_authenticated",
        strategy_evidence_resolver=_strategy_evidence,
        production_evidence_ready=True,
        baseline_runtime_overrides={
            "rag": {"top_k": 3, "no_answer_threshold": 0.3}
        },
        runtime_identity=runtime_identity,
        clock=lambda: NOW,
    )
    deployment = service.create_deployment(
        "farm-a",
        "proposal-audience",
        actor="admin",
        idempotency_key=f"deploy-{suffix}",
    )
    experiment = service.create_experiment(
        "farm-a",
        name=f"真实受众-{suffix}",
        candidate_deployment_id=deployment["id"],
        baseline_rate=0.1,
        minimum_detectable_effect=0.8,
        enrollment_bps=10_000,
        min_duration_hours=1,
        max_duration_hours=24,
        creator="creator",
        idempotency_key=f"create-{suffix}",
    )
    submitted = service.submit_experiment(
        "farm-a",
        experiment["id"],
        actor="admin",
        expected_generation=experiment["generation"],
        idempotency_key=f"submit-{suffix}",
    )
    approved = service.review_experiment(
        "farm-a",
        experiment["id"],
        decision="approve",
        actor="approver",
        expected_generation=submitted["generation"],
        idempotency_key=f"approve-{suffix}",
    )
    active = service.start_experiment(
        "farm-a",
        experiment["id"],
        actor="admin",
        expected_generation=approved["generation"],
        idempotency_key=f"start-{suffix}",
        target_status="running",
    )
    return service, active


def test_production_routing_excludes_untrusted_privileged_and_late_accounts(
    tmp_path: Path,
    runtime_identity_factory,
):
    service, experiment = _active_service(
        tmp_path,
        "eligibility",
        runtime_identity_factory("audience-eligibility"),
    )
    try:
        invalid_identities = [
            None,
            _identity(identity_provenance="self_service"),
            _identity(roles=["participant", "experiment_approver"]),
            _identity(created_at=NOW + timedelta(seconds=1)),
        ]
        for index, identity in enumerate(invalid_identities):
            assert service.resolve_assignment(
                "farm-a",
                f"invalid-{index}",
                "rag_chat",
                f"invalid-turn-{index}",
                f"invalid-trace-{index}",
                identity_context=identity,
            ) is None

        allocation = service.resolve_assignment(
            "farm-a",
            "business-user",
            "rag_chat",
            "eligible-turn",
            "eligible-trace",
            identity_context=_identity(),
        )
        assert allocation is not None
        assert allocation["audience_eligible"] is True
        exposure = service.begin_exposure(
            {
                **allocation,
                "request_fingerprint": hashlib.sha256(b"eligible").hexdigest(),
            }
        )
        service.finish_exposure(
            exposure,
            status="completed",
            latency_ms=12,
        )
        analysis = service.analyze_experiment("farm-a", experiment["id"])
        assert analysis["integrity"]["valid"] is True
        assert analysis["real_exposure_count"] == 1
    finally:
        service.close()


def test_tampered_audience_snapshot_fails_closed_and_safety_pauses(
    tmp_path: Path,
    runtime_identity_factory,
):
    service, experiment = _active_service(
        tmp_path,
        "tamper",
        runtime_identity_factory("audience-tamper"),
    )
    try:
        allocation = service.resolve_assignment(
            "farm-a",
            "business-user",
            "rag_chat",
            "tamper-turn",
            "tamper-trace",
            identity_context=_identity(),
        )
        assert allocation is not None
        forged = {
            **allocation,
            "audience_provenance": "self_service",
            "request_fingerprint": hashlib.sha256(b"tamper").hexdigest(),
        }
        with pytest.raises(ValueError, match="audience evidence"):
            service.begin_exposure(forged)
        stored = service.get_experiment("farm-a", experiment["id"])
        assert stored["status"] == "safety_paused"
        assert stored["pause_reason"] == "audience_evidence_integrity_failed"
    finally:
        service.close()
