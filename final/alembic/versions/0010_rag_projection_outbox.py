"""add transactional RAG projection outbox

Revision ID: 0010_rag_projection_outbox
Revises: 0009_agent_traces
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_rag_projection_outbox"
down_revision: Union[str, Sequence[str], None] = "0009_agent_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rag_projection_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False, server_default="upsert"),
        sa.Column("pg_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dedupe_key", name="uq_rag_projection_outbox_dedupe"),
    )
    op.create_index("ix_rag_projection_outbox_pending", "rag_projection_outbox", ["status", "next_attempt_at", "id"])
    op.create_index("ix_rag_projection_outbox_user_created", "rag_projection_outbox", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_rag_projection_outbox_user_created", table_name="rag_projection_outbox")
    op.drop_index("ix_rag_projection_outbox_pending", table_name="rag_projection_outbox")
    op.drop_table("rag_projection_outbox")
