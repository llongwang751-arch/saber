"""Add deterministic lookup and lifecycle indexes.

Revision ID: 0004_evaluation_indexes
Revises: 0003_evaluation_badcases
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0004_evaluation_indexes"
down_revision: Union[str, Sequence[str], None] = "0003_evaluation_badcases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dataset checksum/version uniqueness is created in 0001.  These indexes
    # serve ordered replay and the hot filters used by the evaluation service.
    op.create_index(
        "ix_dataset_versions_dataset_created",
        "dataset_versions",
        ["dataset_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_eval_cases_version_position",
        "eval_cases",
        ["dataset_version_id", "position"],
        unique=False,
    )
    op.create_index(
        "ix_eval_runs_version_created",
        "eval_runs",
        ["dataset_version_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_eval_runs_status_created",
        "eval_runs",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_case_runs_run_status",
        "case_runs",
        ["eval_run_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_badcases_status_severity",
        "badcases",
        ["status", "severity"],
        unique=False,
    )
    op.create_index(
        "ix_badcases_run_created",
        "badcases",
        ["eval_run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_human_annotations_case_run",
        "human_annotations",
        ["case_run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_human_annotations_badcase",
        "human_annotations",
        ["badcase_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_human_annotations_badcase", table_name="human_annotations")
    op.drop_index("ix_human_annotations_case_run", table_name="human_annotations")
    op.drop_index("ix_badcases_run_created", table_name="badcases")
    op.drop_index("ix_badcases_status_severity", table_name="badcases")
    op.drop_index("ix_case_runs_run_status", table_name="case_runs")
    op.drop_index("ix_eval_runs_status_created", table_name="eval_runs")
    op.drop_index("ix_eval_runs_version_created", table_name="eval_runs")
    op.drop_index("ix_eval_cases_version_position", table_name="eval_cases")
    op.drop_index("ix_dataset_versions_dataset_created", table_name="dataset_versions")
