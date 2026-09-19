"""Isolate chat history by authenticated user and conversation."""
from alembic import op
import sqlalchemy as sa

revision = "0018_conversation_history"
down_revision = "0017_memory_supersession_provenance"
branch_labels = depends_on = None

def upgrade():
    op.create_table("agent_action_journal",
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("action_id", sa.String(200), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    with op.batch_alter_table("agent_chat_history") as batch:
        batch.add_column(sa.Column("conversation_id", sa.String(128), nullable=False, server_default=""))
        batch.create_index("ix_agent_chat_conversation", ["user_id", "conversation_id", "id"])

def downgrade():
    op.drop_table("agent_action_journal")
    with op.batch_alter_table("agent_chat_history") as batch:
        batch.drop_index("ix_agent_chat_conversation")
        batch.drop_column("conversation_id")
