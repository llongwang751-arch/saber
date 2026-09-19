"""Add tenant identity and the trustworthy online experiment control plane.

Revision ID: 0012_online_experiments
Revises: 0011_offline_strategy_review
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_online_experiments"
down_revision: Union[str, Sequence[str], None] = "0011_offline_strategy_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("roles", sa.JSON(), nullable=True))
    users = sa.table(
        "users",
        sa.column("id", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("roles", sa.JSON()),
    )
    bind = op.get_bind()
    legacy_tenant_id = str(os.getenv("AGI_LEGACY_TENANT_ID", "") or "").strip()
    if legacy_tenant_id and (
        len(legacy_tenant_id) > 64 or "\x00" in legacy_tenant_id
    ):
        raise RuntimeError("AGI_LEGACY_TENANT_ID must contain 1 to 64 valid characters")
    # Isolation is the safe upgrade default.  Operators of a verified former
    # single-tenant install can explicitly place legacy accounts into one
    # shared tenant so creator/approver workflows keep working after upgrade.
    bind.execute(
        users.update().values(
            tenant_id=legacy_tenant_id or users.c.id,
            roles=["participant"],
        )
    )
    with op.batch_alter_table("users") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column("roles", existing_type=sa.JSON(), nullable=False)
        batch.create_index("ix_users_tenant_username", ["tenant_id", "username"])

    op.create_table(
        "online_strategy_deployments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("source_proposal_id", sa.String(length=36), nullable=False),
        sa.Column("source_strategy_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_manifest_checksum", sa.String(length=64), nullable=False),
        sa.Column("compiled_overrides", sa.JSON(), nullable=False),
        sa.Column("compiler_version", sa.String(length=32), nullable=False),
        sa.Column("compiled_checksum", sa.String(length=64), nullable=False),
        sa.Column("record_checksum", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_online_strategy_deployments"),
        sa.UniqueConstraint(
            "tenant_id", "source_strategy_version_id", "source_manifest_checksum",
            name="uq_online_deployments_tenant_strategy_checksum",
        ),
    )
    op.create_index(
        "ix_online_deployments_tenant_created",
        "online_strategy_deployments",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "online_experiments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("surface", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("candidate_deployment_id", sa.String(length=36), nullable=False),
        sa.Column("control_overrides", sa.JSON(), nullable=False),
        sa.Column("baseline_runtime_overrides", sa.JSON(), nullable=False),
        sa.Column("runtime_environment_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("candidate_allocation_bps", sa.Integer(), nullable=False),
        sa.Column("initial_enrollment_bps", sa.Integer(), nullable=False),
        sa.Column("enrollment_bps", sa.Integer(), nullable=False),
        sa.Column("primary_metric", sa.String(length=100), nullable=False),
        sa.Column("aggregation_rule", sa.String(length=64), nullable=False),
        sa.Column("baseline_rate", sa.Float(), nullable=False),
        sa.Column("minimum_detectable_effect", sa.Float(), nullable=False),
        sa.Column("alpha", sa.Float(), nullable=False),
        sa.Column("power", sa.Float(), nullable=False),
        sa.Column("required_sample_per_arm", sa.Integer(), nullable=False),
        sa.Column("min_duration_hours", sa.Integer(), nullable=False),
        sa.Column("max_duration_hours", sa.Integer(), nullable=False),
        sa.Column("attribution_window_hours", sa.Integer(), nullable=False),
        sa.Column("traffic_provenance", sa.String(length=40), nullable=False),
        sa.Column("hmac_key_id", sa.String(length=64), nullable=False),
        sa.Column("preregistration_checksum", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("reviewed_by", sa.String(length=200), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_by", sa.String(length=200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_by", sa.String(length=200), nullable=False),
        sa.Column("pause_reason", sa.Text(), nullable=False),
        sa.Column("paused_from", sa.String(length=32), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.String(length=200), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("surface = 'rag_chat'", name="ck_online_experiments_surface"),
        sa.CheckConstraint(
            "status IN ('draft','pending_review','approved','canary','running','paused',"
            "'safety_paused','completed','rolled_back','rejected')",
            name="ck_online_experiments_status",
        ),
        sa.CheckConstraint(
            "candidate_allocation_bps > 0 AND candidate_allocation_bps < 10000",
            name="ck_online_experiments_allocation",
        ),
        sa.CheckConstraint(
            "initial_enrollment_bps > 0 AND initial_enrollment_bps <= 10000 AND "
            "enrollment_bps > 0 AND enrollment_bps <= 10000",
            name="ck_online_experiments_enrollment",
        ),
        sa.CheckConstraint("generation >= 0", name="ck_online_experiments_generation"),
        sa.CheckConstraint("required_sample_per_arm > 0", name="ck_online_experiments_sample"),
        sa.CheckConstraint(
            "max_duration_hours >= min_duration_hours AND min_duration_hours > 0",
            name="ck_online_experiments_duration",
        ),
        sa.CheckConstraint(
            "attribution_window_hours > 0 AND attribution_window_hours <= 720",
            name="ck_online_experiments_attribution_window",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_deployment_id"], ["online_strategy_deployments.id"],
            name="fk_online_experiments_candidate_deployment", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_online_experiments"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_online_experiments_tenant_name"),
    )
    op.create_index(
        "ix_online_experiments_tenant_surface_status", "online_experiments",
        ["tenant_id", "surface", "status"],
    )
    op.create_index(
        "uq_online_experiments_active_surface",
        "online_experiments",
        ["tenant_id", "surface"],
        unique=True,
        postgresql_where=sa.text("status IN ('canary','running')"),
        sqlite_where=sa.text("status IN ('canary','running')"),
    )
    op.create_index(
        "ix_online_experiments_tenant_created", "online_experiments",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "experiment_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("subject_digest", sa.String(length=64), nullable=False),
        sa.Column("enrollment_bucket", sa.Integer(), nullable=False),
        sa.Column("variant_bucket", sa.Integer(), nullable=False),
        sa.Column("arm", sa.String(length=16), nullable=False),
        sa.Column("allocation_bps", sa.Integer(), nullable=False),
        sa.Column("experiment_generation", sa.Integer(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "enrollment_bucket >= 0 AND enrollment_bucket < 10000",
            name="ck_experiment_assignments_enrollment_bucket",
        ),
        sa.CheckConstraint(
            "variant_bucket >= 0 AND variant_bucket < 10000",
            name="ck_experiment_assignments_variant_bucket",
        ),
        sa.CheckConstraint("arm IN ('control','candidate')", name="ck_experiment_assignments_arm"),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["online_experiments.id"],
            name="fk_experiment_assignments_experiment", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiment_assignments"),
        sa.UniqueConstraint(
            "tenant_id", "experiment_id", "subject_digest",
            name="uq_experiment_assignments_tenant_experiment_subject",
        ),
    )
    op.create_index(
        "ix_experiment_assignments_experiment_arm", "experiment_assignments",
        ["tenant_id", "experiment_id", "arm"],
    )

    op.create_table(
        "experiment_exposures",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("assignment_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=100), nullable=False),
        sa.Column("trace_id", sa.String(length=100), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("arm", sa.String(length=16), nullable=False),
        sa.Column("deployment_id", sa.String(length=36), nullable=True),
        sa.Column("runtime_strategy_checksum", sa.String(length=64), nullable=False),
        sa.Column("traffic_provenance", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("safety_events", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("arm IN ('control','candidate')", name="ck_experiment_exposures_arm"),
        sa.CheckConstraint(
            "status IN ('started','completed','error','cancelled')",
            name="ck_experiment_exposures_status",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["online_experiments.id"],
            name="fk_experiment_exposures_experiment", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["experiment_assignments.id"],
            name="fk_experiment_exposures_assignment", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["online_strategy_deployments.id"],
            name="fk_experiment_exposures_deployment", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiment_exposures"),
        sa.UniqueConstraint("tenant_id", "turn_id", name="uq_experiment_exposures_tenant_turn"),
        sa.UniqueConstraint("tenant_id", "trace_id", name="uq_experiment_exposures_tenant_trace"),
    )
    op.create_index(
        "ix_experiment_exposures_experiment_arm", "experiment_exposures",
        ["tenant_id", "experiment_id", "arm"],
    )
    op.create_index(
        "ix_experiment_exposures_experiment_started", "experiment_exposures",
        ["experiment_id", "started_at"],
    )

    op.create_table(
        "experiment_outcomes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("exposure_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column("metric", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("exclusion_reason", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["online_experiments.id"],
            name="fk_experiment_outcomes_experiment", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["exposure_id"], ["experiment_exposures.id"],
            name="fk_experiment_outcomes_exposure", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiment_outcomes"),
        sa.UniqueConstraint("tenant_id", "event_id", name="uq_experiment_outcomes_tenant_event"),
    )
    op.create_index(
        "ix_experiment_outcomes_experiment_metric", "experiment_outcomes",
        ["tenant_id", "experiment_id", "metric"],
    )

    op.create_table(
        "experiment_monitor_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["online_experiments.id"],
            name="fk_experiment_monitor_experiment", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiment_monitor_snapshots"),
    )
    op.create_index(
        "ix_experiment_monitor_experiment_created", "experiment_monitor_snapshots",
        ["experiment_id", "created_at"],
    )

    op.create_table(
        "experiment_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=True),
        sa.Column("deployment_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["online_experiments.id"],
            name="fk_experiment_audit_experiment", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["online_strategy_deployments.id"],
            name="fk_experiment_audit_deployment", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiment_audit_events"),
        sa.UniqueConstraint(
            "tenant_id", "action", "idempotency_key",
            name="uq_experiment_audit_tenant_action_idempotency",
        ),
    )
    op.create_index(
        "ix_experiment_audit_experiment_created", "experiment_audit_events",
        ["experiment_id", "created_at"],
    )
    op.create_index(
        "ix_experiment_audit_tenant_created", "experiment_audit_events",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_experiment_audit_tenant_created", table_name="experiment_audit_events")
    op.drop_index("ix_experiment_audit_experiment_created", table_name="experiment_audit_events")
    op.drop_table("experiment_audit_events")
    op.drop_index("ix_experiment_monitor_experiment_created", table_name="experiment_monitor_snapshots")
    op.drop_table("experiment_monitor_snapshots")
    op.drop_index("ix_experiment_outcomes_experiment_metric", table_name="experiment_outcomes")
    op.drop_table("experiment_outcomes")
    op.drop_index("ix_experiment_exposures_experiment_started", table_name="experiment_exposures")
    op.drop_index("ix_experiment_exposures_experiment_arm", table_name="experiment_exposures")
    op.drop_table("experiment_exposures")
    op.drop_index("ix_experiment_assignments_experiment_arm", table_name="experiment_assignments")
    op.drop_table("experiment_assignments")
    op.drop_index("ix_online_experiments_tenant_created", table_name="online_experiments")
    op.drop_index("uq_online_experiments_active_surface", table_name="online_experiments")
    op.drop_index("ix_online_experiments_tenant_surface_status", table_name="online_experiments")
    op.drop_table("online_experiments")
    op.drop_index("ix_online_deployments_tenant_created", table_name="online_strategy_deployments")
    op.drop_table("online_strategy_deployments")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_tenant_username")
        batch.drop_column("roles")
        batch.drop_column("tenant_id")
