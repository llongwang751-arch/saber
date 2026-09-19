"""Persistence records for the tenant-scoped online experiment control plane.

These tables deliberately keep raw chat text and raw user identifiers out of
assignment/exposure data.  ``subject_digest`` is derived with the experiment
HMAC key by the service layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from internal.evaluation.store import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


EXPERIMENT_STATUSES = frozenset({
    "draft",
    "pending_review",
    "approved",
    "canary",
    "running",
    "paused",
    "safety_paused",
    "completed",
    "rolled_back",
    "rejected",
})
ACTIVE_EXPERIMENT_STATUSES = frozenset({"canary", "running"})
TRAFFIC_PROVENANCE_VALUES = frozenset({
    "disabled",
    "internal",
    "production_authenticated",
})


class OnlineStrategyDeploymentRecord(Base):
    __tablename__ = "online_strategy_deployments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_strategy_version_id",
            "source_manifest_checksum",
            name="uq_online_deployments_tenant_strategy_checksum",
        ),
        Index("ix_online_deployments_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_proposal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_strategy_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    compiled_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(32), nullable=False)
    compiled_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    record_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class OnlineExperimentRecord(Base):
    __tablename__ = "online_experiments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_online_experiments_tenant_name"),
        CheckConstraint("surface = 'rag_chat'", name="ck_online_experiments_surface"),
        CheckConstraint(
            "status IN ('draft','pending_review','approved','canary','running','paused',"
            "'safety_paused','completed','rolled_back','rejected')",
            name="ck_online_experiments_status",
        ),
        CheckConstraint(
            "candidate_allocation_bps > 0 AND candidate_allocation_bps < 10000",
            name="ck_online_experiments_allocation",
        ),
        CheckConstraint(
            "initial_enrollment_bps > 0 AND initial_enrollment_bps <= 10000 AND "
            "enrollment_bps > 0 AND enrollment_bps <= 10000",
            name="ck_online_experiments_enrollment",
        ),
        CheckConstraint("generation >= 0", name="ck_online_experiments_generation"),
        CheckConstraint("required_sample_per_arm > 0", name="ck_online_experiments_sample"),
        CheckConstraint(
            "max_duration_hours >= min_duration_hours AND min_duration_hours > 0",
            name="ck_online_experiments_duration",
        ),
        CheckConstraint(
            "attribution_window_hours > 0 AND attribution_window_hours <= 720",
            name="ck_online_experiments_attribution_window",
        ),
        Index("ix_online_experiments_tenant_surface_status", "tenant_id", "surface", "status"),
        # The read-before-write check in the store provides a useful error,
        # while this partial unique index is the authoritative PostgreSQL/
        # SQLite guard against two concurrent starts on one surface.
        Index(
            "uq_online_experiments_active_surface",
            "tenant_id",
            "surface",
            unique=True,
            postgresql_where=text("status IN ('canary','running')"),
            sqlite_where=text("status IN ('canary','running')"),
        ),
        Index("ix_online_experiments_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    surface: Mapped[str] = mapped_column(String(32), nullable=False, default="rag_chat")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    candidate_deployment_id: Mapped[str] = mapped_column(
        ForeignKey("online_strategy_deployments.id", ondelete="RESTRICT"), nullable=False
    )
    control_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    baseline_runtime_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    runtime_environment_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    runtime_identity_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    candidate_allocation_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_enrollment_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    enrollment_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    primary_metric: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregation_rule: Mapped[str] = mapped_column(
        String(64), nullable=False, default="first_feedback_per_exposed_user_v1"
    )
    baseline_rate: Mapped[float] = mapped_column(Float, nullable=False)
    minimum_detectable_effect: Mapped[float] = mapped_column(Float, nullable=False)
    alpha: Mapped[float] = mapped_column(Float, nullable=False)
    power: Mapped[float] = mapped_column(Float, nullable=False)
    required_sample_per_arm: Mapped[int] = mapped_column(Integer, nullable=False)
    min_duration_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    max_duration_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    attribution_window_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=168)
    traffic_provenance: Mapped[str] = mapped_column(String(40), nullable=False)
    audience_policy_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="production_authenticated_v1"
    )
    hmac_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preregistration_checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    review_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    pause_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    paused_from: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class ExperimentAssignmentRecord(Base):
    __tablename__ = "experiment_assignments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "experiment_id", "subject_digest",
            name="uq_experiment_assignments_tenant_experiment_subject",
        ),
        CheckConstraint(
            "enrollment_bucket >= 0 AND enrollment_bucket < 10000",
            name="ck_experiment_assignments_enrollment_bucket",
        ),
        CheckConstraint(
            "variant_bucket >= 0 AND variant_bucket < 10000",
            name="ck_experiment_assignments_variant_bucket",
        ),
        CheckConstraint("arm IN ('control','candidate')", name="ck_experiment_assignments_arm"),
        Index("ix_experiment_assignments_experiment_arm", "tenant_id", "experiment_id", "arm"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("online_experiments.id", ondelete="CASCADE"), nullable=False
    )
    subject_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    enrollment_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
    variant_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
    arm: Mapped[str] = mapped_column(String(16), nullable=False)
    allocation_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    experiment_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ExperimentExposureRecord(Base):
    __tablename__ = "experiment_exposures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "turn_id", name="uq_experiment_exposures_tenant_turn"),
        UniqueConstraint("tenant_id", "trace_id", name="uq_experiment_exposures_tenant_trace"),
        CheckConstraint("arm IN ('control','candidate')", name="ck_experiment_exposures_arm"),
        CheckConstraint(
            "status IN ('started','completed','error','cancelled')",
            name="ck_experiment_exposures_status",
        ),
        Index("ix_experiment_exposures_experiment_arm", "tenant_id", "experiment_id", "arm"),
        Index("ix_experiment_exposures_experiment_started", "experiment_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("online_experiments.id", ondelete="CASCADE"), nullable=False
    )
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("experiment_assignments.id", ondelete="RESTRICT"), nullable=False
    )
    turn_id: Mapped[str] = mapped_column(String(100), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    arm: Mapped[str] = mapped_column(String(16), nullable=False)
    deployment_id: Mapped[str | None] = mapped_column(
        ForeignKey("online_strategy_deployments.id", ondelete="RESTRICT"), nullable=True
    )
    runtime_strategy_checksum: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    traffic_provenance: Mapped[str] = mapped_column(String(40), nullable=False)
    # These fields are a server-created audience snapshot.  No raw user id is
    # persisted in the experiment ledger; the HMAC attestation binds the
    # eligibility decision to the pseudonymous assignment.
    audience_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    audience_provenance: Mapped[str] = mapped_column(String(40), nullable=False)
    audience_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    audience_account_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    audience_attestation: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="started")
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    safety_events: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentOutcomeRecord(Base):
    __tablename__ = "experiment_outcomes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", name="uq_experiment_outcomes_tenant_event"),
        Index("ix_experiment_outcomes_experiment_metric", "tenant_id", "experiment_id", "metric"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("online_experiments.id", ondelete="CASCADE"), nullable=False
    )
    exposure_id: Mapped[str] = mapped_column(
        ForeignKey("experiment_exposures.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    metric: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    exclusion_reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ExperimentMonitorSnapshotRecord(Base):
    __tablename__ = "experiment_monitor_snapshots"
    __table_args__ = (
        Index("ix_experiment_monitor_experiment_created", "experiment_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("online_experiments.id", ondelete="CASCADE"), nullable=False
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ExperimentSafetyOutboxRecord(Base):
    """Durable severe-safety signal written independently of exposure finish."""

    __tablename__ = "experiment_safety_outbox"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "exposure_id", "signal_checksum",
            name="uq_experiment_safety_outbox_signal",
        ),
        CheckConstraint(
            "status IN ('pending','processed')",
            name="ck_experiment_safety_outbox_status",
        ),
        Index(
            "ix_experiment_safety_outbox_pending",
            "tenant_id", "status", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("online_experiments.id", ondelete="CASCADE"), nullable=False
    )
    exposure_id: Mapped[str] = mapped_column(
        ForeignKey("experiment_exposures.id", ondelete="CASCADE"), nullable=False
    )
    signal_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_code: Mapped[str] = mapped_column(String(100), nullable=False)
    safety_events: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ExperimentAuditEventRecord(Base):
    __tablename__ = "experiment_audit_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "action", "idempotency_key",
            name="uq_experiment_audit_tenant_action_idempotency",
        ),
        UniqueConstraint(
            "tenant_id", "experiment_id", "chain_position",
            name="uq_experiment_audit_chain_position",
        ),
        Index("ix_experiment_audit_experiment_created", "experiment_id", "created_at"),
        Index("ix_experiment_audit_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str | None] = mapped_column(
        ForeignKey("online_experiments.id", ondelete="SET NULL"), nullable=True
    )
    deployment_id: Mapped[str | None] = mapped_column(
        ForeignKey("online_strategy_deployments.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    to_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Lifecycle events form an append-only, per-experiment SHA-256 chain.  A
    # deployment-only audit event has position zero and an empty predecessor;
    # experiment events start at one and cannot be reordered unnoticed.
    chain_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    previous_event_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


__all__ = [
    "ACTIVE_EXPERIMENT_STATUSES",
    "EXPERIMENT_STATUSES",
    "TRAFFIC_PROVENANCE_VALUES",
    "ExperimentAssignmentRecord",
    "ExperimentAuditEventRecord",
    "ExperimentExposureRecord",
    "ExperimentMonitorSnapshotRecord",
    "ExperimentOutcomeRecord",
    "ExperimentSafetyOutboxRecord",
    "OnlineExperimentRecord",
    "OnlineStrategyDeploymentRecord",
]
