"""Add durable local Agent state and transactional memory outbox.

Revision ID: 0007_local_agent_state
Revises: 0006_smart_farm
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_local_agent_state"
down_revision: Union[str, Sequence[str], None] = "0006_smart_farm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_preferences",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_chat_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_chat_user_id", "agent_chat_history", ["user_id", "id"])
    op.create_table(
        "agent_task_snapshots",
        sa.Column("task_id", sa.String(100), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_snapshot_user_created", "agent_task_snapshots", ["user_id", "created_at"])
    op.create_table(
        "agent_long_term_memory",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_accessed", sa.Float(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("slot_hint", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=False),
    )
    op.create_index("ix_agent_ltm_user_status_id", "agent_long_term_memory", ["user_id", "status", "id"])
    op.create_index("ix_agent_ltm_user_category", "agent_long_term_memory", ["user_id", "category"])
    op.create_table(
        "agent_documents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_documents_user_updated", "agent_documents", ["user_id", "updated_at"])
    op.create_table(
        "agent_document_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), sa.ForeignKey("agent_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "version", name="uq_agent_document_version"),
    )
    op.create_index(
        "ix_agent_document_versions_document_version",
        "agent_document_versions",
        ["document_id", "version"],
    )
    op.create_table(
        "memory_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_outbox_pending", "memory_outbox", ["processed_at", "next_attempt_at", "id"])
    op.create_index("ix_memory_outbox_user_created", "memory_outbox", ["user_id", "created_at"])
    op.create_table(
        "agent_rag_chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("doc_hash", sa.String(64), nullable=False),
        sa.Column("chunk_idx", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("parent_content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("section", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "doc_hash", "chunk_idx", name="uq_agent_rag_user_chunk"),
    )
    op.create_index("ix_agent_rag_user_document", "agent_rag_chunks", ["user_id", "document_id"])
    op.create_index("ix_agent_rag_user_hash", "agent_rag_chunks", ["user_id", "doc_hash"])


def downgrade() -> None:
    op.drop_index("ix_agent_rag_user_hash", table_name="agent_rag_chunks")
    op.drop_index("ix_agent_rag_user_document", table_name="agent_rag_chunks")
    op.drop_table("agent_rag_chunks")
    op.drop_index("ix_memory_outbox_user_created", table_name="memory_outbox")
    op.drop_index("ix_memory_outbox_pending", table_name="memory_outbox")
    op.drop_table("memory_outbox")
    op.drop_index("ix_agent_document_versions_document_version", table_name="agent_document_versions")
    op.drop_table("agent_document_versions")
    op.drop_index("ix_agent_documents_user_updated", table_name="agent_documents")
    op.drop_table("agent_documents")
    op.drop_index("ix_agent_ltm_user_category", table_name="agent_long_term_memory")
    op.drop_index("ix_agent_ltm_user_status_id", table_name="agent_long_term_memory")
    op.drop_table("agent_long_term_memory")
    op.drop_index("ix_agent_snapshot_user_created", table_name="agent_task_snapshots")
    op.drop_table("agent_task_snapshots")
    op.drop_index("ix_agent_chat_user_id", table_name="agent_chat_history")
    op.drop_table("agent_chat_history")
    op.drop_table("agent_preferences")
