"""Add multi-tenant smart-farm records and reports.

Revision ID: 0006_smart_farm
Revises: 0005_application_identity_skills
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_smart_farm"
down_revision: Union[str, Sequence[str], None] = "0005_application_identity_skills"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "farm_production_records",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=False),
        sa.Column("farm_name", sa.String(200), nullable=False),
        sa.Column("barn_name", sa.String(200), nullable=False),
        sa.Column("batch_no", sa.String(200), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("opening_head", sa.Integer(), nullable=False),
        sa.Column("average_head", sa.Float(), nullable=False),
        sa.Column("transfers_in", sa.Integer(), nullable=False),
        sa.Column("transfers_out", sa.Integer(), nullable=False),
        sa.Column("deaths", sa.Integer(), nullable=False),
        sa.Column("culled", sa.Integer(), nullable=False),
        sa.Column("total_born", sa.Integer(), nullable=False),
        sa.Column("live_born", sa.Integer(), nullable=False),
        sa.Column("weaned", sa.Integer(), nullable=False),
        sa.Column("feed_kg", sa.Float(), nullable=False),
        sa.Column("weight_gain_kg", sa.Float(), nullable=False),
        sa.Column("scheduled_minutes", sa.Float(), nullable=False),
        sa.Column("downtime_minutes", sa.Float(), nullable=False),
        sa.Column("design_rate_kg_min", sa.Float(), nullable=False),
        sa.Column("actual_output_kg", sa.Float(), nullable=False),
        sa.Column("qualified_output_kg", sa.Float(), nullable=False),
        sa.Column("source_name", sa.String(300), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "source_hash", "source_row", name="uq_farm_source_row"),
    )
    op.create_index("ix_farm_records_user_date", "farm_production_records", ["user_id", "record_date"])
    op.create_index(
        "ix_farm_records_user_farm_stage",
        "farm_production_records",
        ["user_id", "farm_name", "stage", "record_date"],
    )
    op.create_table(
        "farm_reports",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("report_type", sa.String(32), nullable=False),
        sa.Column("farm_name", sa.String(200), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("anomalies", sa.JSON(), nullable=False),
        sa.Column("data_quality", sa.JSON(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_farm_reports_user_created", "farm_reports", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_farm_reports_user_created", table_name="farm_reports")
    op.drop_table("farm_reports")
    op.drop_index("ix_farm_records_user_farm_stage", table_name="farm_production_records")
    op.drop_index("ix_farm_records_user_date", table_name="farm_production_records")
    op.drop_table("farm_production_records")

