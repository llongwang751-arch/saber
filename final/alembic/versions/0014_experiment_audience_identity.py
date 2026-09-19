"""Add operator-controlled experiment audience identity evidence.

Revision ID: 0014_experiment_audience_identity
Revises: 0013_controlled_evolution
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014_experiment_audience_identity"
down_revision: Union[str, Sequence[str], None] = "0013_controlled_evolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing accounts and exposures are deliberately untrusted.  An
    # operator must explicitly provision eligible business identities after
    # the upgrade; silently grandfathering legacy/self-service accounts would
    # turn cheap Sybil accounts into production evidence.
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("identity_provenance", sa.String(length=40), nullable=True)
        )
        batch.add_column(
            sa.Column("experiment_eligible", sa.Boolean(), nullable=True)
        )
    op.execute(
        "UPDATE users SET identity_provenance = 'legacy_unverified', "
        "experiment_eligible = FALSE"
    )
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "identity_provenance", existing_type=sa.String(length=40), nullable=False
        )
        batch.alter_column(
            "experiment_eligible", existing_type=sa.Boolean(), nullable=False
        )

    with op.batch_alter_table("online_experiments") as batch:
        batch.add_column(
            sa.Column("audience_policy_version", sa.String(length=64), nullable=True)
        )
    op.execute(
        "UPDATE online_experiments SET audience_policy_version = 'legacy_unverified'"
    )
    with op.batch_alter_table("online_experiments") as batch:
        batch.alter_column(
            "audience_policy_version",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    with op.batch_alter_table("experiment_exposures") as batch:
        batch.add_column(
            sa.Column("audience_policy_version", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("audience_provenance", sa.String(length=40), nullable=True)
        )
        batch.add_column(sa.Column("audience_eligible", sa.Boolean(), nullable=True))
        batch.add_column(
            sa.Column("audience_account_created_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("audience_attestation", sa.String(length=64), nullable=True)
        )
    op.execute(
        "UPDATE experiment_exposures SET "
        "audience_policy_version = 'legacy_unverified', "
        "audience_provenance = 'legacy_unverified', "
        "audience_eligible = FALSE, audience_attestation = ''"
    )
    with op.batch_alter_table("experiment_exposures") as batch:
        batch.alter_column(
            "audience_policy_version",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.alter_column(
            "audience_provenance", existing_type=sa.String(length=40), nullable=False
        )
        batch.alter_column(
            "audience_eligible", existing_type=sa.Boolean(), nullable=False
        )
        batch.alter_column(
            "audience_attestation", existing_type=sa.String(length=64), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("experiment_exposures") as batch:
        batch.drop_column("audience_attestation")
        batch.drop_column("audience_account_created_at")
        batch.drop_column("audience_eligible")
        batch.drop_column("audience_provenance")
        batch.drop_column("audience_policy_version")
    with op.batch_alter_table("online_experiments") as batch:
        batch.drop_column("audience_policy_version")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("experiment_eligible")
        batch.drop_column("identity_provenance")
