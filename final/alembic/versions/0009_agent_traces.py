"""persist privacy-redacted agent traces

Revision ID: 0009_agent_traces
Revises: 0008_memory_consistency
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_agent_traces"
down_revision: Union[str, Sequence[str], None] = "0008_memory_consistency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_traces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("query_redacted", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
        sa.Column("trace", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_traces_user_created", "agent_traces", ["user_id", "created_at"])
    op.create_index("ix_agent_traces_user_mode", "agent_traces", ["user_id", "mode"])


def downgrade() -> None:
    op.drop_index("ix_agent_traces_user_mode", table_name="agent_traces")
    op.drop_index("ix_agent_traces_user_created", table_name="agent_traces")
    op.drop_table("agent_traces")
