"""Create Badcase lifecycle and human annotation tables.

Revision ID: 0003_evaluation_badcases
Revises: 0002_evaluation_runs
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_evaluation_badcases"
down_revision: Union[str, Sequence[str], None] = "0002_evaluation_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "badcases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("eval_run_id", sa.String(length=36), nullable=False),
        sa.Column("case_run_id", sa.String(length=36), nullable=False),
        sa.Column("eval_case_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_run_id"],
            ["case_runs.id"],
            name="fk_badcases_case_run_id_case_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["eval_case_id"],
            ["eval_cases.id"],
            name="fk_badcases_eval_case_id_eval_cases",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["eval_run_id"],
            ["eval_runs.id"],
            name="fk_badcases_eval_run_id_eval_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_badcases"),
        sa.UniqueConstraint("case_run_id", name="uq_badcases_case_run"),
    )
    op.create_table(
        "human_annotations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("case_run_id", sa.String(length=36), nullable=True),
        sa.Column("badcase_id", sa.String(length=36), nullable=True),
        sa.Column("eval_case_id", sa.String(length=36), nullable=True),
        sa.Column("annotator", sa.String(length=200), nullable=False),
        sa.Column("annotation", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "case_run_id IS NOT NULL OR badcase_id IS NOT NULL OR eval_case_id IS NOT NULL",
            name="ck_human_annotations_has_target",
        ),
        sa.ForeignKeyConstraint(
            ["badcase_id"],
            ["badcases.id"],
            name="fk_human_annotations_badcase_id_badcases",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["case_run_id"],
            ["case_runs.id"],
            name="fk_human_annotations_case_run_id_case_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["eval_case_id"],
            ["eval_cases.id"],
            name="fk_human_annotations_eval_case_id_eval_cases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_human_annotations"),
    )


def downgrade() -> None:
    op.drop_table("human_annotations")
    op.drop_table("badcases")
