"""Persist verified runtime component identity in experiment preregistration.

Revision ID: 0016_verified_runtime_identity
Revises: 0015_experiment_lifecycle_integrity
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016_verified_runtime_identity"
down_revision: Union[str, Sequence[str], None] = (
    "0015_experiment_lifecycle_integrity"
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("online_experiments") as batch:
        batch.add_column(
            sa.Column("runtime_identity_evidence", sa.JSON(), nullable=True)
        )
    experiments = sa.table(
        "online_experiments",
        sa.column("runtime_identity_evidence", sa.JSON()),
    )
    op.get_bind().execute(
        experiments.update().values(runtime_identity_evidence={})
    )
    with op.batch_alter_table("online_experiments") as batch:
        batch.alter_column(
            "runtime_identity_evidence", existing_type=sa.JSON(), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("online_experiments") as batch:
        batch.drop_column("runtime_identity_evidence")
