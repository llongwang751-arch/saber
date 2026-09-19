"""Add immutable offline strategy review and activation lifecycle.

Revision ID: 0011_offline_strategy_review
Revises: 0010_rag_projection_outbox
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011_offline_strategy_review"
down_revision: Union[str, Sequence[str], None] = "0010_rag_projection_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_canonical_json", sa.Text(), nullable=False),
        sa.Column("manifest_checksum", sa.String(length=64), nullable=False),
        sa.Column("creator", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_strategy_versions_positive_version"),
        sa.CheckConstraint("source = 'offline_eval'", name="ck_strategy_versions_offline_source"),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_versions"),
        sa.UniqueConstraint(
            "manifest_checksum", name="uq_strategy_versions_manifest_checksum"
        ),
    )
    op.create_index(
        "ix_strategy_versions_created",
        "strategy_versions",
        ["created_at", "id"],
    )

    with op.batch_alter_table("eval_runs") as batch:
        batch.add_column(sa.Column("strategy_version_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_eval_runs_strategy_version_id_strategy_versions",
            "strategy_versions",
            ["strategy_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_eval_runs_strategy_created",
            ["strategy_version_id", "created_at"],
        )

    op.create_table(
        "promotion_proposals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("baseline_run_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_run_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_strategy_version_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("comparison", sa.JSON(), nullable=False),
        sa.Column("statistics", sa.JSON(), nullable=False),
        sa.Column("release_gate", sa.JSON(), nullable=False),
        sa.Column("safety", sa.JSON(), nullable=False),
        sa.Column("blocked_reasons", sa.JSON(), nullable=False),
        sa.Column("evidence_checksum", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("reviewed_by", sa.String(length=200), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by", sa.String(length=200), nullable=False),
        sa.Column("activation_note", sa.Text(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "baseline_run_id <> candidate_run_id",
            name="ck_promotion_proposals_distinct_runs",
        ),
        sa.CheckConstraint(
            "status IN ('proposed','blocked','approved','rejected','activated')",
            name="ck_promotion_proposals_status",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_run_id"],
            ["eval_runs.id"],
            name="fk_promotion_proposals_baseline_run_id_eval_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_run_id"],
            ["eval_runs.id"],
            name="fk_promotion_proposals_candidate_run_id_eval_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_strategy_version_id"],
            ["strategy_versions.id"],
            name="fk_promotion_proposals_candidate_strategy_id_strategy_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_promotion_proposals"),
        sa.UniqueConstraint(
            "baseline_run_id",
            "candidate_run_id",
            "candidate_strategy_version_id",
            name="uq_promotion_proposals_run_pair_strategy",
        ),
    )
    op.create_index(
        "ix_promotion_proposals_status_created",
        "promotion_proposals",
        ["status", "created_at"],
    )

    op.create_table(
        "strategy_pointers",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("current_strategy_version_id", sa.String(length=36), nullable=True),
        sa.Column("previous_strategy_version_id", sa.String(length=36), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=200), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["current_strategy_version_id"],
            ["strategy_versions.id"],
            name="fk_strategy_pointers_current_strategy_id_strategy_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_strategy_version_id"],
            ["strategy_versions.id"],
            name="fk_strategy_pointers_previous_strategy_id_strategy_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("scope", name="pk_strategy_pointers"),
    )

    op.create_table(
        "strategy_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=True),
        sa.Column("strategy_version_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["promotion_proposals.id"],
            name="fk_strategy_audit_events_proposal_id_promotion_proposals",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
            name="fk_strategy_audit_events_strategy_id_strategy_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_audit_events"),
        sa.UniqueConstraint(
            "action", "idempotency_key", name="uq_strategy_audit_action_key"
        ),
    )
    op.create_index(
        "ix_strategy_audit_proposal_created",
        "strategy_audit_events",
        ["proposal_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_audit_proposal_created", table_name="strategy_audit_events"
    )
    op.drop_table("strategy_audit_events")
    op.drop_table("strategy_pointers")
    op.drop_index(
        "ix_promotion_proposals_status_created", table_name="promotion_proposals"
    )
    op.drop_table("promotion_proposals")
    with op.batch_alter_table("eval_runs") as batch:
        batch.drop_index("ix_eval_runs_strategy_created")
        batch.drop_constraint(
            "fk_eval_runs_strategy_version_id_strategy_versions", type_="foreignkey"
        )
        batch.drop_column("strategy_version_id")
    op.drop_index("ix_strategy_versions_created", table_name="strategy_versions")
    op.drop_table("strategy_versions")
