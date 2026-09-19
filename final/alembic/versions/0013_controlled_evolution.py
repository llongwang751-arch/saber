"""Add immutable, human-reviewed offline RAG strategy suggestions.

Revision ID: 0013_controlled_evolution
Revises: 0012_online_experiments
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013_controlled_evolution"
down_revision: Union[str, Sequence[str], None] = "0012_online_experiments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evolution_suggestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_run_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_strategy_version_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("audit_event_count", sa.Integer(), nullable=False),
        sa.Column("audit_head_checksum", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("evidence_checksum", sa.String(length=64), nullable=False),
        sa.Column("suggestion_manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_canonical_json", sa.Text(), nullable=False),
        sa.Column("manifest_checksum", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.JSON(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("truth", sa.JSON(), nullable=False),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("no_auto_apply", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("reviewed_by", sa.String(length=200), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("materialized_strategy_version_id", sa.String(length=36), nullable=True),
        sa.Column("materialized_by", sa.String(length=200), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed','accepted','rejected')",
            name="ck_evolution_suggestions_status",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_evolution_suggestions_generation"
        ),
        sa.CheckConstraint(
            "audit_event_count >= 0",
            name="ck_evolution_suggestions_audit_event_count",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["eval_runs.id"],
            name="fk_evolution_suggestions_source_run_id_eval_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["dataset_versions.id"],
            name="fk_evolution_suggestions_dataset_version_id_dataset_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_strategy_version_id"],
            ["strategy_versions.id"],
            name="fk_evolution_suggestions_source_strategy_id_strategy_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["materialized_strategy_version_id"],
            ["strategy_versions.id"],
            name="fk_evolution_suggestions_materialized_strategy_id_strategy_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evolution_suggestions"),
        sa.UniqueConstraint(
            "source_run_id",
            "rule_version",
            name="uq_evolution_suggestions_run_rule",
        ),
    )
    op.create_index(
        "ix_evolution_suggestions_status_created",
        "evolution_suggestions",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_evolution_suggestions_run_created",
        "evolution_suggestions",
        ["source_run_id", "created_at"],
    )

    op.create_table(
        "evolution_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_checksum", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_event_checksum", sa.String(length=64), nullable=False),
        sa.Column("event_checksum", sa.String(length=64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["suggestion_id"],
            ["evolution_suggestions.id"],
            name="fk_evolution_audit_suggestion_id_evolution_suggestions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evolution_audit_events"),
        sa.UniqueConstraint(
            "action",
            "idempotency_key",
            name="uq_evolution_audit_action_key",
        ),
        sa.UniqueConstraint(
            "suggestion_id",
            "sequence",
            name="uq_evolution_audit_suggestion_sequence",
        ),
        sa.UniqueConstraint(
            "event_checksum",
            name="uq_evolution_audit_events_event_checksum",
        ),
    )
    op.create_index(
        "ix_evolution_audit_suggestion_created",
        "evolution_audit_events",
        ["suggestion_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evolution_audit_suggestion_created", table_name="evolution_audit_events"
    )
    op.drop_table("evolution_audit_events")
    op.drop_index(
        "ix_evolution_suggestions_run_created", table_name="evolution_suggestions"
    )
    op.drop_index(
        "ix_evolution_suggestions_status_created", table_name="evolution_suggestions"
    )
    op.drop_table("evolution_suggestions")
