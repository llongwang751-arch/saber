"""versioned memory records, targeted outbox, and projection state

Revision ID: 0008_memory_consistency
Revises: 0007_local_agent_state
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_memory_consistency"
down_revision: Union[str, Sequence[str], None] = "0007_local_agent_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_long_term_memory") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("content_hash", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("updated_at", sa.Float(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("deleted_at", sa.Float(), nullable=True))
        batch.add_column(sa.Column("embedding_model", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("embedding_revision", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("supersedes", sa.JSON(), nullable=False, server_default="[]"))

    with op.batch_alter_table("memory_outbox") as batch:
        batch.drop_index("ix_memory_outbox_pending")
        batch.add_column(sa.Column("event_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("aggregate_version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("target", sa.String(32), nullable=True))
        batch.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("lease_owner", sa.String(100), nullable=False, server_default=""))
        batch.add_column(sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    outbox = sa.table(
        "memory_outbox",
        sa.column("id", sa.Integer()),
        sa.column("event_id", sa.String()),
        sa.column("target", sa.String()),
    )
    bind = op.get_bind()
    for row_id in bind.execute(sa.select(outbox.c.id).where(outbox.c.event_id.is_(None))).scalars():
        bind.execute(
            outbox.update().where(outbox.c.id == row_id).values(event_id=str(uuid.uuid4()), target="ltm_cache")
        )
    bind.execute(outbox.update().where(outbox.c.target.is_(None)).values(target="ltm_cache"))
    with op.batch_alter_table("memory_outbox") as batch:
        batch.alter_column("event_id", nullable=False)
        batch.alter_column("target", nullable=False)
        batch.create_unique_constraint("uq_memory_outbox_event_id", ["event_id"])
        batch.create_index("ix_memory_outbox_pending", ["status", "target", "next_attempt_at", "id"])

    op.create_table(
        "memory_projection_state",
        sa.Column("target", sa.String(32), primary_key=True),
        sa.Column("aggregate_id", sa.String(100), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("memory_projection_state")
    with op.batch_alter_table("memory_outbox") as batch:
        batch.drop_index("ix_memory_outbox_pending")
        batch.drop_constraint("uq_memory_outbox_event_id", type_="unique")
        for column in ("lease_until", "lease_owner", "dead_at", "status", "target", "aggregate_version", "event_id"):
            batch.drop_column(column)
        batch.create_index("ix_memory_outbox_pending", ["processed_at", "next_attempt_at", "id"])
    with op.batch_alter_table("agent_long_term_memory") as batch:
        for column in ("supersedes", "embedding_revision", "embedding_model", "deleted_at", "updated_at", "content_hash", "version"):
            batch.drop_column(column)
