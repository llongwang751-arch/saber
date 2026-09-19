"""persist memory supersession timestamp provenance

Revision ID: 0017_memory_supersession_provenance
Revises: 0016_verified_runtime_identity
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017_memory_supersession_provenance"
down_revision: Union[str, Sequence[str], None] = "0016_verified_runtime_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_long_term_memory") as batch:
        batch.add_column(sa.Column("superseded_at", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_long_term_memory") as batch:
        batch.drop_column("superseded_at")
