"""SQLAlchemy records for product features that are not evaluation-specific."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from internal.evaluation.store import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_tenant_username", "tenant_id", "username"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=lambda: ["participant"])
    # Experiment eligibility is an operator-controlled server-side identity
    # attribute.  A normal self-service account must never become production
    # A/B evidence merely by presenting a valid JWT.
    identity_provenance: Mapped[str] = mapped_column(
        String(40), nullable=False, default="self_service"
    )
    experiment_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InstalledSkillRecord(Base):
    __tablename__ = "installed_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "skill_id", name="uq_installed_skills_user_skill"),
        Index("ix_installed_skills_user_enabled", "user_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(300), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="office")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invocation: Mapped[str] = mapped_column(String(32), nullable=False, default="prompt")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parameters: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class FarmProductionRecord(Base):
    __tablename__ = "farm_production_records"
    __table_args__ = (
        UniqueConstraint("user_id", "source_hash", "source_row", name="uq_farm_source_row"),
        Index("ix_farm_records_user_date", "user_id", "record_date"),
        Index("ix_farm_records_user_farm_stage", "user_id", "farm_name", "stage", "record_date"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    record_date: Mapped[date] = mapped_column(Date, nullable=False)
    farm_name: Mapped[str] = mapped_column(String(200), nullable=False)
    barn_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    batch_no: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    opening_head: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_head: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    transfers_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transfers_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deaths: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    culled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_born: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    live_born: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weaned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    feed_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    weight_gain_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    scheduled_minutes: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    downtime_minutes: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    design_rate_kg_min: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    actual_output_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    qualified_output_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    source_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class FarmReportRecord(Base):
    __tablename__ = "farm_reports"
    __table_args__ = (Index("ix_farm_reports_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    report_type: Mapped[str] = mapped_column(String(32), nullable=False)
    farm_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    metrics: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    anomalies: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AgentPreferenceRecord(Base):
    __tablename__ = "agent_preferences"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class AgentActionRecord(Base):
    __tablename__ = "agent_action_journal"
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AgentChatHistoryRecord(Base):
    __tablename__ = "agent_chat_history"
    __table_args__ = (Index("ix_agent_chat_user_id", "user_id", "id"),
                     Index("ix_agent_chat_conversation", "user_id", "conversation_id", "id"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AgentTaskSnapshotRecord(Base):
    __tablename__ = "agent_task_snapshots"
    __table_args__ = (Index("ix_agent_snapshot_user_created", "user_id", "created_at"),)

    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AgentLongTermMemoryRecord(Base):
    __tablename__ = "agent_long_term_memory"
    __table_args__ = (
        Index("ix_agent_ltm_user_status_id", "user_id", "status", "id"),
        Index("ix_agent_ltm_user_category", "user_id", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_accessed: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    slot_hint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    superseded_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    quarantine_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    deleted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    embedding_revision: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    supersedes: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)


class AgentDocumentRecord(Base):
    __tablename__ = "agent_documents"
    __table_args__ = (Index("ix_agent_documents_user_updated", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AgentDocumentVersionRecord(Base):
    __tablename__ = "agent_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_agent_document_version"),
        Index("ix_agent_document_versions_document_version", "document_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("agent_documents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MemoryOutboxRecord(Base):
    __tablename__ = "memory_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_memory_outbox_event_id"),
        Index("ix_memory_outbox_pending", "status", "target", "next_attempt_at", "id"),
        Index("ix_memory_outbox_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MemoryProjectionRecord(Base):
    """Durable local stand-in for rebuildable Milvus/Neo4j projection state."""

    __tablename__ = "memory_projection_state"

    target: Mapped[str] = mapped_column(String(32), primary_key=True)
    aggregate_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AgentRagChunkRecord(Base):
    __tablename__ = "agent_rag_chunks"
    __table_args__ = (
        UniqueConstraint("user_id", "doc_hash", "chunk_idx", name="uq_agent_rag_user_chunk"),
        Index("ix_agent_rag_user_document", "user_id", "document_id"),
        Index("ix_agent_rag_user_hash", "user_id", "doc_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    doc_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    parent_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False, default=list)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    version_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    section: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AgentTraceRecord(Base):
    __tablename__ = "agent_traces"
    __table_args__ = (
        Index("ix_agent_traces_user_created", "user_id", "created_at"),
        Index("ix_agent_traces_user_mode", "user_id", "mode"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    query_redacted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    trace: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class RagProjectionJobRecord(Base):
    """Transactional outbox for rebuildable ES/Milvus RAG projections."""

    __tablename__ = "rag_projection_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_rag_projection_outbox_dedupe"),
        Index("ix_rag_projection_outbox_pending", "status", "next_attempt_at", "id"),
        Index("ix_rag_projection_outbox_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False, default="upsert")
    pg_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
