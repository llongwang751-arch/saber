"""SQLAlchemy persistence for the Agent evaluation workflow.

The store deliberately persists evaluator inputs and outputs as JSON.  The
evaluation schemas can therefore evolve without a database migration for every
new metric or trace event, while lifecycle fields remain queryable columns.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from sqlalchemy import (
    Boolean,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect as sa_inspect,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


RUN_STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled"})
CASE_RUN_STATUSES = frozenset({"pending", "running", "passed", "failed", "error", "skipped"})
BADCASE_STATUSES = frozenset({"open", "triaged", "investigating", "resolved", "closed"})
BADCASE_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
TERMINAL_CASE_RUN_STATUSES = frozenset({"passed", "failed", "error", "skipped"})
PROMOTION_STATUSES = frozenset({"proposed", "blocked", "approved", "rejected", "activated"})
EVOLUTION_STATUSES = frozenset({"proposed", "accepted", "rejected"})
STRATEGY_SOURCE = "offline_eval"
_EVOLUTION_AUDIT_GENESIS = "0" * 64


class EvaluationStoreError(RuntimeError):
    """Base error raised by the evaluation persistence layer."""


class ImmutableDatasetVersionError(EvaluationStoreError):
    """Raised when code attempts to mutate an imported dataset version."""


class ImmutableEvolutionSuggestionError(EvaluationStoreError):
    """Raised when immutable evidence or a suggested manifest is changed."""


class ImmutableEvaluationEvidenceError(EvaluationStoreError, ValueError):
    """Raised when a terminal run or persisted case result would be rewritten."""


class Base(DeclarativeBase):
    pass


class DatasetRecord(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())


class DatasetVersionRecord(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_dataset_versions_dataset_version"),
        UniqueConstraint("dataset_id", "checksum", name="uq_dataset_versions_dataset_checksum"),
        CheckConstraint("version > 0", name="ck_dataset_versions_positive_version"),
        Index("ix_dataset_versions_dataset_created", "dataset_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())


class EvalCaseRecord(Base):
    __tablename__ = "eval_cases"
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "case_key", name="uq_eval_cases_version_case"),
        UniqueConstraint("dataset_version_id", "position", name="uq_eval_cases_version_position"),
        CheckConstraint("position >= 0", name="ck_eval_cases_nonnegative_position"),
        Index("ix_eval_cases_version_position", "dataset_version_id", "position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"), nullable=False
    )
    case_key: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())


class StrategyVersionRecord(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("manifest_checksum", name="uq_strategy_versions_manifest_checksum"),
        CheckConstraint("version > 0", name="ck_strategy_versions_positive_version"),
        CheckConstraint("source = 'offline_eval'", name="ck_strategy_versions_offline_source"),
        Index("ix_strategy_versions_created", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    creator: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default=STRATEGY_SOURCE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())


class EvalRunRecord(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_eval_runs_version_created", "dataset_version_id", "created_at"),
        Index("ix_eval_runs_strategy_created", "strategy_version_id", "created_at"),
        Index("ix_eval_runs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False
    )
    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: _utcnow(), onupdate=lambda: _utcnow()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromotionProposalRecord(Base):
    __tablename__ = "promotion_proposals"
    __table_args__ = (
        UniqueConstraint(
            "baseline_run_id",
            "candidate_run_id",
            "candidate_strategy_version_id",
            name="uq_promotion_proposals_run_pair_strategy",
        ),
        CheckConstraint(
            "baseline_run_id <> candidate_run_id",
            name="ck_promotion_proposals_distinct_runs",
        ),
        CheckConstraint(
            "status IN ('proposed','blocked','approved','rejected','activated')",
            name="ck_promotion_proposals_status",
        ),
        Index("ix_promotion_proposals_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    baseline_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_strategy_version_id: Mapped[str] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    comparison: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    statistics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    release_gate: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    safety: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    blocked_reasons: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    evidence_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    review_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    activation_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: _utcnow(), onupdate=lambda: _utcnow()
    )


class StrategyPointerRecord(Base):
    __tablename__ = "strategy_pointers"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    current_strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    previous_strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())


class StrategyAuditEventRecord(Base):
    __tablename__ = "strategy_audit_events"
    __table_args__ = (
        UniqueConstraint("action", "idempotency_key", name="uq_strategy_audit_action_key"),
        Index("ix_strategy_audit_proposal_created", "proposal_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(
        ForeignKey("promotion_proposals.id", ondelete="RESTRICT"), nullable=True
    )
    strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())


class EvolutionSuggestionRecord(Base):
    """Immutable evidence/manifest plus a small human-review state machine."""

    __tablename__ = "evolution_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "source_run_id",
            "rule_version",
            name="uq_evolution_suggestions_run_rule",
        ),
        CheckConstraint(
            "status IN ('proposed','accepted','rejected')",
            name="ck_evolution_suggestions_status",
        ),
        CheckConstraint("generation >= 0", name="ck_evolution_suggestions_generation"),
        CheckConstraint(
            "audit_event_count >= 0",
            name="ck_evolution_suggestions_audit_event_count",
        ),
        Index("ix_evolution_suggestions_status_created", "status", "created_at"),
        Index("ix_evolution_suggestions_run_created", "source_run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="RESTRICT"), nullable=False
    )
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False
    )
    source_strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audit_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audit_head_checksum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    suggestion_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    limitations: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    truth: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    no_auto_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    review_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    materialized_strategy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    materialized_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: _utcnow()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: _utcnow(), onupdate=lambda: _utcnow()
    )


class EvolutionAuditEventRecord(Base):
    __tablename__ = "evolution_audit_events"
    __table_args__ = (
        UniqueConstraint("action", "idempotency_key", name="uq_evolution_audit_action_key"),
        UniqueConstraint(
            "suggestion_id",
            "sequence",
            name="uq_evolution_audit_suggestion_sequence",
        ),
        Index("ix_evolution_audit_suggestion_created", "suggestion_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    suggestion_id: Mapped[str] = mapped_column(
        ForeignKey("evolution_suggestions.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_event_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    event_checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: _utcnow()
    )


class CaseRunRecord(Base):
    __tablename__ = "case_runs"
    __table_args__ = (
        UniqueConstraint("eval_run_id", "eval_case_id", name="uq_case_runs_run_case"),
        Index("ix_case_runs_run_status", "eval_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    eval_case_id: Mapped[str] = mapped_column(
        ForeignKey("eval_cases.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    metrics: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    trace: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: _utcnow(), onupdate=lambda: _utcnow()
    )


class BadcaseRecord(Base):
    __tablename__ = "badcases"
    __table_args__ = (
        UniqueConstraint("case_run_id", name="uq_badcases_case_run"),
        Index("ix_badcases_status_severity", "status", "severity"),
        Index("ix_badcases_run_created", "eval_run_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    case_run_id: Mapped[str] = mapped_column(
        ForeignKey("case_runs.id", ondelete="CASCADE"), nullable=False
    )
    eval_case_id: Mapped[str] = mapped_column(
        ForeignKey("eval_cases.id", ondelete="RESTRICT"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="unclassified")
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    owner: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    details: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    resolution: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: _utcnow(), onupdate=lambda: _utcnow()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HumanAnnotationRecord(Base):
    __tablename__ = "human_annotations"
    __table_args__ = (
        CheckConstraint(
            "case_run_id IS NOT NULL OR badcase_id IS NOT NULL OR eval_case_id IS NOT NULL",
            name="ck_human_annotations_has_target",
        ),
        Index("ix_human_annotations_case_run", "case_run_id", "created_at"),
        Index("ix_human_annotations_badcase", "badcase_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("case_runs.id", ondelete="CASCADE"), nullable=True
    )
    badcase_id: Mapped[str | None] = mapped_column(
        ForeignKey("badcases.id", ondelete="CASCADE"), nullable=True
    )
    eval_case_id: Mapped[str | None] = mapped_column(
        ForeignKey("eval_cases.id", ondelete="CASCADE"), nullable=True
    )
    annotator: Mapped[str] = mapped_column(String(200), nullable=False)
    annotation: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: _utcnow())


@event.listens_for(DatasetVersionRecord, "before_update")
@event.listens_for(DatasetVersionRecord, "before_delete")
@event.listens_for(EvalCaseRecord, "before_update")
@event.listens_for(EvalCaseRecord, "before_delete")
@event.listens_for(StrategyVersionRecord, "before_update")
@event.listens_for(StrategyVersionRecord, "before_delete")
def _reject_dataset_version_mutation(*_: Any) -> None:
    raise ImmutableDatasetVersionError("dataset and strategy versions are immutable")


@event.listens_for(EvolutionSuggestionRecord, "before_update")
def _reject_evolution_evidence_mutation(_: Any, __: Any, target: EvolutionSuggestionRecord) -> None:
    protected = {
        "source_run_id",
        "dataset_version_id",
        "source_strategy_version_id",
        "rule_version",
        "evidence",
        "evidence_checksum",
        "suggestion_manifest",
        "manifest_canonical_json",
        "manifest_checksum",
        "rationale",
        "limitations",
        "truth",
        "diagnostics",
        "no_auto_apply",
        "created_by",
        "created_at",
    }
    state = sa_inspect(target)
    if any(state.attrs[name].history.has_changes() for name in protected):
        raise ImmutableEvolutionSuggestionError(
            "evolution evidence and suggestion manifests are immutable"
        )


@event.listens_for(EvolutionSuggestionRecord, "before_delete")
def _reject_evolution_suggestion_delete(*_: Any) -> None:
    raise ImmutableEvolutionSuggestionError("evolution suggestions are immutable audit records")


@event.listens_for(EvolutionAuditEventRecord, "before_update")
@event.listens_for(EvolutionAuditEventRecord, "before_delete")
def _reject_evolution_audit_mutation(*_: Any) -> None:
    raise ImmutableEvolutionSuggestionError("evolution audit events are immutable")


class EvaluationStore:
    """Synchronous SQLAlchemy repository used by evaluation services.

    ``database_url`` defaults to ``AGI_EVAL_DATABASE_URL`` and then to
    ``final/runtime/evaluation.db``.  Tests and callers can inject any
    SQLAlchemy 2 compatible URL.
    """

    def __init__(self, database_url: str | None = None, *, create_schema: bool = True):
        self.database_url = database_url or os.getenv("AGI_EVAL_DATABASE_URL") or _default_database_url()
        self.engine = _create_engine(self.database_url)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)
        if create_schema:
            self.create_schema()

    def close(self) -> None:
        self.engine.dispose()

    def create_schema(self) -> None:
        _upgrade_schema(self.engine, self.database_url)

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_dataset(
        self,
        name: str,
        *,
        description: str = "",
        metadata: Mapping[str, Any] | None = None,
        dataset_id: str | None = None,
    ) -> dict[str, Any]:
        name = _required_text(name, "name", max_length=200)
        record = DatasetRecord(
            id=dataset_id or _new_id(),
            name=name,
            description=(description or "").strip(),
            metadata_json=_jsonable(metadata or {}),
        )
        try:
            with self._transaction() as session:
                session.add(record)
                session.flush()
        except IntegrityError as exc:
            raise EvaluationStoreError(f"dataset already exists: {name}") from exc
        return _dataset_dict(record)

    def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(DatasetRecord, _required_text(dataset_id, "dataset_id"))
            if record is None:
                raise LookupError(f"dataset not found: {dataset_id}")
            return _dataset_dict(record)

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            records = session.scalars(
                select(DatasetRecord).order_by(DatasetRecord.created_at.desc(), DatasetRecord.id)
            ).all()
            return [_dataset_dict(record) for record in records]

    def import_dataset_version(
        self,
        dataset_id: str,
        cases: Iterable[Mapping[str, Any] | Any],
        *,
        checksum: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically import an immutable version and all of its cases.

        Re-importing the same checksum is idempotent and returns the existing
        version with ``created=False``.  The checksum is calculated from a
        canonical JSON representation when the caller does not provide one.
        """

        dataset_id = _required_text(dataset_id, "dataset_id")
        payloads = [_normalise_case_payload(case) for case in cases]
        if not payloads:
            raise ValueError("at least one evaluation case is required")
        case_keys = [_case_key(payload, index) for index, payload in enumerate(payloads)]
        if len(case_keys) != len(set(case_keys)):
            raise ValueError("case_id values must be unique within a dataset version")
        checksum_value = (checksum or _payload_checksum(payloads)).strip().lower()
        if not checksum_value or len(checksum_value) > 128:
            raise ValueError("checksum must contain 1 to 128 characters")

        try:
            with self._transaction() as session:
                if session.get(DatasetRecord, dataset_id) is None:
                    raise LookupError(f"dataset not found: {dataset_id}")
                duplicate = session.scalar(
                    select(DatasetVersionRecord).where(
                        DatasetVersionRecord.dataset_id == dataset_id,
                        DatasetVersionRecord.checksum == checksum_value,
                    )
                )
                if duplicate is not None:
                    result = _version_dict(duplicate)
                    result.update(
                        created=False,
                        case_count=_version_case_count(session, duplicate.id),
                    )
                    return result

                latest_version = session.scalar(
                    select(func.max(DatasetVersionRecord.version)).where(
                        DatasetVersionRecord.dataset_id == dataset_id
                    )
                )
                version = DatasetVersionRecord(
                    id=version_id or _new_id(),
                    dataset_id=dataset_id,
                    version=int(latest_version or 0) + 1,
                    checksum=checksum_value,
                    metadata_json=_jsonable(metadata or {}),
                )
                session.add(version)
                session.flush()
                for position, (case_key, payload) in enumerate(zip(case_keys, payloads)):
                    session.add(
                        EvalCaseRecord(
                            id=_new_id(),
                            dataset_version_id=version.id,
                            case_key=case_key,
                            position=position,
                            payload=payload,
                        )
                    )
                session.flush()
                result = _version_dict(version)
                result.update(created=True, case_count=len(payloads))
                return result
        except IntegrityError as exc:
            # A concurrent importer may win the checksum unique constraint.
            with self._session_factory() as session:
                duplicate = session.scalar(
                    select(DatasetVersionRecord).where(
                        DatasetVersionRecord.dataset_id == dataset_id,
                        DatasetVersionRecord.checksum == checksum_value,
                    )
                )
                if duplicate is not None:
                    result = _version_dict(duplicate)
                    result.update(
                        created=False,
                        case_count=_version_case_count(session, duplicate.id),
                    )
                    return result
            raise EvaluationStoreError("dataset version import conflicted") from exc

    def get_dataset_version(self, version_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(DatasetVersionRecord, _required_text(version_id, "version_id"))
            if record is None:
                raise LookupError(f"dataset version not found: {version_id}")
            result = _version_dict(record)
            result["case_count"] = _version_case_count(session, record.id)
            return result

    def list_dataset_versions(self, dataset_id: str) -> list[dict[str, Any]]:
        dataset_id = _required_text(dataset_id, "dataset_id")
        with self._session_factory() as session:
            records = session.scalars(
                select(DatasetVersionRecord)
                .where(DatasetVersionRecord.dataset_id == dataset_id)
                .order_by(DatasetVersionRecord.version.desc())
            ).all()
            result = []
            for record in records:
                item = _version_dict(record)
                item["case_count"] = _version_case_count(session, record.id)
                result.append(item)
            return result

    def get_version_cases(self, version_id: str) -> list[dict[str, Any]]:
        version_id = _required_text(version_id, "version_id")
        with self._session_factory() as session:
            if session.get(DatasetVersionRecord, version_id) is None:
                raise LookupError(f"dataset version not found: {version_id}")
            records = session.scalars(
                select(EvalCaseRecord)
                .where(EvalCaseRecord.dataset_version_id == version_id)
                .order_by(EvalCaseRecord.position)
            ).all()
            return [_case_dict(record) for record in records]

    read_version_cases = get_version_cases

    def create_strategy_version(
        self,
        name: str,
        manifest: Mapping[str, Any],
        *,
        creator: str,
        strategy_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Create an immutable, content-addressed offline strategy snapshot."""

        from .strategy import manifest_sha256

        name = _required_text(name, "name", max_length=200)
        creator = _required_text(creator, "creator", max_length=200)
        canonical, checksum = manifest_sha256(manifest)
        manifest_value = json.loads(canonical)
        try:
            with self._transaction() as session:
                duplicate = session.scalar(
                    select(StrategyVersionRecord).where(
                        StrategyVersionRecord.manifest_checksum == checksum
                    )
                )
                if duplicate is not None:
                    result = _strategy_version_dict(duplicate)
                    result["created"] = False
                    return result
                latest_version = session.scalar(select(func.max(StrategyVersionRecord.version)))
                record = StrategyVersionRecord(
                    id=strategy_version_id or _new_id(),
                    version=int(latest_version or 0) + 1,
                    name=name,
                    manifest=manifest_value,
                    manifest_canonical_json=canonical,
                    manifest_checksum=checksum,
                    creator=creator,
                    source=STRATEGY_SOURCE,
                )
                session.add(record)
                session.flush()
                result = _strategy_version_dict(record)
                result["created"] = True
                return result
        except IntegrityError as exc:
            # Concurrent creation of the same content is idempotent.
            with self._session_factory() as session:
                duplicate = session.scalar(
                    select(StrategyVersionRecord).where(
                        StrategyVersionRecord.manifest_checksum == checksum
                    )
                )
                if duplicate is not None:
                    result = _strategy_version_dict(duplicate)
                    result["created"] = False
                    return result
            raise EvaluationStoreError("strategy version creation conflicted") from exc

    def get_strategy_version(self, strategy_version_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(
                StrategyVersionRecord,
                _required_text(strategy_version_id, "strategy_version_id"),
            )
            if record is None:
                raise LookupError(f"strategy version not found: {strategy_version_id}")
            return _strategy_version_dict(record)

    def list_strategy_versions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = _validated_limit(limit, maximum=1000)
        with self._session_factory() as session:
            records = session.scalars(
                select(StrategyVersionRecord)
                .order_by(StrategyVersionRecord.version.desc(), StrategyVersionRecord.id)
                .limit(limit)
            ).all()
            return [_strategy_version_dict(record) for record in records]

    def create_run(
        self,
        dataset_version_id: str,
        *,
        name: str = "",
        status: str = "pending",
        config: Any = None,
        metadata: Mapping[str, Any] | None = None,
        strategy_version_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        dataset_version_id = _required_text(dataset_version_id, "dataset_version_id")
        strategy_version_id = (
            _required_text(strategy_version_id, "strategy_version_id")
            if strategy_version_id is not None else None
        )
        status = _validated_choice(status, "status", RUN_STATUSES)
        now = _utcnow()
        record = EvalRunRecord(
            id=run_id or _new_id(),
            dataset_version_id=dataset_version_id,
            strategy_version_id=strategy_version_id,
            name=(name or "").strip(),
            status=status,
            config=_jsonable({} if config is None else config),
            summary={},
            metadata_json=_jsonable(metadata or {}),
            started_at=now if status == "running" else None,
            completed_at=now if status in TERMINAL_RUN_STATUSES else None,
        )
        with self._transaction() as session:
            if session.get(DatasetVersionRecord, dataset_version_id) is None:
                raise LookupError(f"dataset version not found: {dataset_version_id}")
            if (
                strategy_version_id is not None
                and session.get(StrategyVersionRecord, strategy_version_id) is None
            ):
                raise LookupError(f"strategy version not found: {strategy_version_id}")
            session.add(record)
            session.flush()
        return _run_dict(record)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(EvalRunRecord, _required_text(run_id, "run_id"))
            if record is None:
                raise LookupError(f"evaluation run not found: {run_id}")
            return _run_dict(record)

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        summary: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = _required_text(run_id, "run_id")
        with self._transaction() as session:
            record = session.get(EvalRunRecord, run_id)
            if record is None:
                raise LookupError(f"evaluation run not found: {run_id}")
            if record.status in TERMINAL_RUN_STATUSES:
                raise ImmutableEvaluationEvidenceError(
                    f"terminal evaluation run {run_id} is immutable"
                )
            now = _utcnow()
            if status is not None:
                record.status = _validated_choice(status, "status", RUN_STATUSES)
                if record.status == "running" and record.started_at is None:
                    record.started_at = now
                if record.status in TERMINAL_RUN_STATUSES:
                    record.completed_at = now
                elif record.status in {"pending", "running"}:
                    record.completed_at = None
            if summary is not None:
                record.summary = _jsonable(summary)
            if metadata is not None:
                record.metadata_json = _jsonable(metadata)
            record.updated_at = now
            session.flush()
            return _run_dict(record)

    def claim_run_execution(self, run_id: str) -> dict[str, Any]:
        """Atomically claim a pending run across workers and service instances.

        A process-local lock cannot prevent two application replicas (or two
        actor-bound services sharing one tenant database) from both starting
        the same Agent run.  The conditional UPDATE is the execution fence.
        """

        run_id = _required_text(run_id, "run_id")
        now = _utcnow()
        with self._transaction() as session:
            result = session.execute(
                update(EvalRunRecord)
                .where(
                    EvalRunRecord.id == run_id,
                    EvalRunRecord.status == "pending",
                )
                .values(status="running", started_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            record = session.get(EvalRunRecord, run_id)
            if record is None:
                raise LookupError(f"evaluation run not found: {run_id}")
            claimed = int(result.rowcount or 0) == 1
            if not claimed and record.status != "running":
                raise ImmutableEvaluationEvidenceError(
                    f"terminal evaluation run {run_id} is immutable and cannot execute "
                    f"from status "
                    f"{record.status!r}"
                )
            payload = _run_dict(record)
            payload["execution_claimed"] = claimed
            return payload

    def cancel_run_execution(self, run_id: str) -> dict[str, Any]:
        """Atomically cancel only a non-terminal run."""

        run_id = _required_text(run_id, "run_id")
        now = _utcnow()
        with self._transaction() as session:
            result = session.execute(
                update(EvalRunRecord)
                .where(
                    EvalRunRecord.id == run_id,
                    EvalRunRecord.status.in_(("pending", "running")),
                )
                .values(status="cancelled", completed_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            record = session.get(EvalRunRecord, run_id)
            if record is None:
                raise LookupError(f"evaluation run not found: {run_id}")
            if int(result.rowcount or 0) != 1:
                raise ImmutableEvaluationEvidenceError(
                    f"terminal evaluation run {run_id} is immutable and cannot be cancelled"
                )
            return _run_dict(record)

    def list_runs(
        self,
        *,
        dataset_version_id: str | None = None,
        dataset_id: str | None = None,
        strategy_version_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = _validated_limit(limit)
        stmt = select(EvalRunRecord)
        if dataset_version_id:
            stmt = stmt.where(EvalRunRecord.dataset_version_id == dataset_version_id)
        if dataset_id:
            stmt = stmt.join(
                DatasetVersionRecord,
                DatasetVersionRecord.id == EvalRunRecord.dataset_version_id,
            ).where(DatasetVersionRecord.dataset_id == dataset_id)
        if strategy_version_id:
            stmt = stmt.where(EvalRunRecord.strategy_version_id == strategy_version_id)
        if status:
            stmt = stmt.where(
                EvalRunRecord.status == _validated_choice(status, "status", RUN_STATUSES)
            )
        stmt = stmt.order_by(EvalRunRecord.created_at.desc(), EvalRunRecord.id).limit(limit)
        with self._session_factory() as session:
            return [_run_dict(record) for record in session.scalars(stmt).all()]

    def save_case_result(
        self,
        run_id: str,
        case_id: str,
        *,
        status: str,
        output: Any = None,
        metrics: Any = None,
        trace: Any = None,
        error: Any = None,
        passed: bool | None = None,
        auto_badcase: bool = True,
        badcase_category: str = "unclassified",
        badcase_severity: str = "medium",
        badcase_details: Any = None,
    ) -> dict[str, Any]:
        run_id = _required_text(run_id, "run_id")
        case_id = _required_text(case_id, "case_id")
        status = _validated_choice(status, "status", CASE_RUN_STATUSES)
        with self._transaction() as session:
            run = session.get(EvalRunRecord, run_id)
            if run is None:
                raise LookupError(f"evaluation run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                raise ImmutableEvaluationEvidenceError(
                    f"case results for terminal evaluation run {run_id} are immutable"
                )
            case = session.scalar(
                select(EvalCaseRecord).where(
                    EvalCaseRecord.dataset_version_id == run.dataset_version_id,
                    or_(EvalCaseRecord.id == case_id, EvalCaseRecord.case_key == case_id),
                )
            )
            if case is None:
                raise LookupError(f"evaluation case not found in run dataset version: {case_id}")
            record = session.scalar(
                select(CaseRunRecord).where(
                    CaseRunRecord.eval_run_id == run_id,
                    CaseRunRecord.eval_case_id == case.id,
                )
            )
            now = _utcnow()
            if record is None:
                record = CaseRunRecord(
                    id=_new_id(),
                    eval_run_id=run_id,
                    eval_case_id=case.id,
                    status=status,
                    output=_jsonable({} if output is None else output),
                    metrics=_jsonable({} if metrics is None else metrics),
                    trace=_jsonable([] if trace is None else trace),
                    error=_jsonable({} if error is None else error),
                )
                session.add(record)
            else:
                next_output = _jsonable({} if output is None else output)
                next_metrics = _jsonable({} if metrics is None else metrics)
                next_trace = _jsonable([] if trace is None else trace)
                next_error = _jsonable({} if error is None else error)
                if record.status in TERMINAL_CASE_RUN_STATUSES:
                    if (
                        record.status == status
                        and _jsonable(record.output) == next_output
                        and _jsonable(record.metrics) == next_metrics
                        and _jsonable(record.trace) == next_trace
                        and _jsonable(record.error) == next_error
                    ):
                        badcase = session.scalar(
                            select(BadcaseRecord).where(
                                BadcaseRecord.case_run_id == record.id
                            )
                        )
                        result = _case_run_dict(record, case.case_key)
                        result["badcase"] = (
                            _badcase_dict(badcase) if badcase is not None else None
                        )
                        result["idempotent"] = True
                        return result
                    raise ImmutableEvaluationEvidenceError(
                        f"terminal case result for run {run_id} and case "
                        f"{case.case_key} is immutable"
                    )
                record.status = status
                record.output = next_output
                record.metrics = next_metrics
                record.trace = next_trace
                record.error = next_error
                record.updated_at = now
            session.flush()

            badcase = None
            is_badcase = _is_bad_result(record.status, record.metrics, passed)
            if auto_badcase and is_badcase:
                badcase = _create_or_get_badcase(
                    session,
                    record,
                    category=badcase_category,
                    severity=badcase_severity,
                    details={} if badcase_details is None else badcase_details,
                )
            result = _case_run_dict(record, case.case_key)
            result["badcase"] = _badcase_dict(badcase) if badcase is not None else None
            return result

    def get_case_result(self, case_run_id: str) -> dict[str, Any]:
        case_run_id = _required_text(case_run_id, "case_run_id")
        with self._session_factory() as session:
            row = session.execute(
                select(CaseRunRecord, EvalCaseRecord.case_key)
                .join(EvalCaseRecord, EvalCaseRecord.id == CaseRunRecord.eval_case_id)
                .where(CaseRunRecord.id == case_run_id)
            ).one_or_none()
            if row is None:
                raise LookupError(f"case result not found: {case_run_id}")
            return _case_run_dict(row[0], row[1])

    def list_case_results(self, run_id: str, *, limit: int = 10000) -> list[dict[str, Any]]:
        run_id = _required_text(run_id, "run_id")
        limit = _validated_limit(limit, maximum=100000)
        with self._session_factory() as session:
            rows = session.execute(
                select(CaseRunRecord, EvalCaseRecord.case_key)
                .join(EvalCaseRecord, EvalCaseRecord.id == CaseRunRecord.eval_case_id)
                .where(CaseRunRecord.eval_run_id == run_id)
                .order_by(EvalCaseRecord.position)
                .limit(limit)
            ).all()
            return [_case_run_dict(record, case_key) for record, case_key in rows]

    list_results = list_case_results

    def create_badcase(
        self,
        case_run_id: str,
        *,
        category: str = "unclassified",
        severity: str = "medium",
        details: Any = None,
    ) -> dict[str, Any]:
        case_run_id = _required_text(case_run_id, "case_run_id")
        with self._transaction() as session:
            case_run = session.get(CaseRunRecord, case_run_id)
            if case_run is None:
                raise LookupError(f"case result not found: {case_run_id}")
            record = _create_or_get_badcase(
                session,
                case_run,
                category=category,
                severity=severity,
                details={} if details is None else details,
            )
            return _badcase_dict(record)

    def auto_create_badcase(
        self,
        case_run_id: str,
        *,
        category: str = "unclassified",
        severity: str = "medium",
        details: Any = None,
    ) -> dict[str, Any] | None:
        """Create a badcase only when the stored result represents a failure."""

        case_run_id = _required_text(case_run_id, "case_run_id")
        with self._transaction() as session:
            case_run = session.get(CaseRunRecord, case_run_id)
            if case_run is None:
                raise LookupError(f"case result not found: {case_run_id}")
            if not _is_bad_result(case_run.status, case_run.metrics, None):
                return None
            return _badcase_dict(
                _create_or_get_badcase(
                    session,
                    case_run,
                    category=category,
                    severity=severity,
                    details={} if details is None else details,
                )
            )

    def list_badcases(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        owner: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = _validated_limit(limit, maximum=10000)
        stmt = select(BadcaseRecord)
        if run_id:
            stmt = stmt.where(BadcaseRecord.eval_run_id == run_id)
        if status:
            stmt = stmt.where(
                BadcaseRecord.status == _validated_choice(status, "status", BADCASE_STATUSES)
            )
        if category:
            stmt = stmt.where(BadcaseRecord.category == category)
        if severity:
            stmt = stmt.where(
                BadcaseRecord.severity
                == _validated_choice(severity, "severity", BADCASE_SEVERITIES)
            )
        if owner is not None:
            stmt = stmt.where(BadcaseRecord.owner == owner)
        stmt = stmt.order_by(BadcaseRecord.created_at.desc(), BadcaseRecord.id).limit(limit)
        with self._session_factory() as session:
            return [_badcase_dict(record) for record in session.scalars(stmt).all()]

    def get_badcase(self, badcase_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(BadcaseRecord, _required_text(badcase_id, "badcase_id"))
            if record is None:
                raise LookupError(f"badcase not found: {badcase_id}")
            return _badcase_dict(record)

    def triage_badcase(
        self,
        badcase_id: str,
        *,
        category: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        resolution: Any = None,
    ) -> dict[str, Any]:
        badcase_id = _required_text(badcase_id, "badcase_id")
        with self._transaction() as session:
            record = session.get(BadcaseRecord, badcase_id)
            if record is None:
                raise LookupError(f"badcase not found: {badcase_id}")
            if category is not None:
                record.category = _required_text(category, "category", max_length=100)
            if severity is not None:
                record.severity = _validated_choice(
                    severity, "severity", BADCASE_SEVERITIES
                )
            if status is not None:
                next_status = _validated_choice(status, "status", BADCASE_STATUSES)
                if next_status in {"resolved", "closed"}:
                    raise ValueError(
                        "triage cannot resolve or close a Badcase; use regression verification"
                    )
                record.status = next_status
                record.resolved_at = None
            if owner is not None:
                record.owner = owner.strip()
            if resolution is not None:
                record.resolution = _jsonable(resolution)
            now = _utcnow()
            record.updated_at = now
            session.flush()
            return _badcase_dict(record)

    def apply_badcase_verification(
        self,
        badcase_id: str,
        *,
        passed: bool,
        resolution: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply the terminal transition reserved for verified candidate runs."""

        if not isinstance(passed, bool):
            raise ValueError("passed must be boolean")
        badcase_id = _required_text(badcase_id, "badcase_id")
        with self._transaction() as session:
            record = session.get(BadcaseRecord, badcase_id)
            if record is None:
                raise LookupError(f"badcase not found: {badcase_id}")
            now = _utcnow()
            record.status = "resolved" if passed else "investigating"
            record.resolution = _jsonable(resolution)
            record.resolved_at = now if passed else None
            record.updated_at = now
            session.flush()
            return _badcase_dict(record)

    def create_annotation(
        self,
        *,
        annotator: str,
        annotation: Any,
        case_run_id: str | None = None,
        badcase_id: str | None = None,
        eval_case_id: str | None = None,
        annotation_id: str | None = None,
    ) -> dict[str, Any]:
        if not any((case_run_id, badcase_id, eval_case_id)):
            raise ValueError("an annotation must target a case result, badcase, or evaluation case")
        annotator = _required_text(annotator, "annotator", max_length=200)
        with self._transaction() as session:
            _require_existing_target(session, CaseRunRecord, case_run_id, "case result")
            _require_existing_target(session, BadcaseRecord, badcase_id, "badcase")
            _require_existing_target(session, EvalCaseRecord, eval_case_id, "evaluation case")
            record = HumanAnnotationRecord(
                id=annotation_id or _new_id(),
                case_run_id=case_run_id,
                badcase_id=badcase_id,
                eval_case_id=eval_case_id,
                annotator=annotator,
                annotation=_jsonable(annotation),
            )
            session.add(record)
            session.flush()
            return _annotation_dict(record)

    add_annotation = create_annotation
    save_annotation = create_annotation

    def list_annotations(
        self,
        *,
        case_run_id: str | None = None,
        badcase_id: str | None = None,
        eval_case_id: str | None = None,
        annotator: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        limit = _validated_limit(limit, maximum=10000)
        stmt = select(HumanAnnotationRecord)
        if case_run_id:
            stmt = stmt.where(HumanAnnotationRecord.case_run_id == case_run_id)
        if badcase_id:
            stmt = stmt.where(HumanAnnotationRecord.badcase_id == badcase_id)
        if eval_case_id:
            stmt = stmt.where(HumanAnnotationRecord.eval_case_id == eval_case_id)
        if annotator:
            stmt = stmt.where(HumanAnnotationRecord.annotator == annotator)
        stmt = stmt.order_by(
            HumanAnnotationRecord.created_at.desc(), HumanAnnotationRecord.id
        ).limit(limit)
        with self._session_factory() as session:
            return [_annotation_dict(record) for record in session.scalars(stmt).all()]

    def create_promotion_proposal(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        *,
        candidate_strategy_version_id: str,
        status: str,
        comparison: Mapping[str, Any],
        statistics: Mapping[str, Any],
        release_gate: Mapping[str, Any],
        safety: Mapping[str, Any],
        blocked_reasons: Sequence[str],
        created_by: str,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist an immutable evidence snapshot and a review state."""

        baseline_run_id = _required_text(baseline_run_id, "baseline_run_id")
        candidate_run_id = _required_text(candidate_run_id, "candidate_run_id")
        if baseline_run_id == candidate_run_id:
            raise ValueError("baseline and candidate runs must be different")
        candidate_strategy_version_id = _required_text(
            candidate_strategy_version_id, "candidate_strategy_version_id"
        )
        created_by = _required_text(created_by, "created_by", max_length=200)
        status = _validated_choice(status, "status", PROMOTION_STATUSES)
        if status not in {"proposed", "blocked"}:
            raise ValueError("a new promotion proposal must be proposed or blocked")
        reasons = [_required_text(str(item), "blocked_reason", max_length=1000) for item in blocked_reasons]
        if status == "blocked" and not reasons:
            raise ValueError("a blocked proposal requires at least one reason")
        if status == "proposed" and reasons:
            raise ValueError("a proposed review cannot contain blocked reasons")

        comparison_value = _jsonable(comparison)
        statistics_value = _jsonable(statistics)
        gate_value = _jsonable(release_gate)
        safety_value = _jsonable(safety)
        evidence_payload = {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
            "candidate_strategy_version_id": candidate_strategy_version_id,
            "comparison": comparison_value,
            "statistics": statistics_value,
            "release_gate": gate_value,
            "safety": safety_value,
        }
        evidence_checksum = _canonical_checksum(evidence_payload)

        try:
            with self._transaction() as session:
                baseline = session.get(EvalRunRecord, baseline_run_id)
                candidate = session.get(EvalRunRecord, candidate_run_id)
                if baseline is None:
                    raise LookupError(f"evaluation run not found: {baseline_run_id}")
                if candidate is None:
                    raise LookupError(f"evaluation run not found: {candidate_run_id}")
                if session.get(StrategyVersionRecord, candidate_strategy_version_id) is None:
                    raise LookupError(
                        f"strategy version not found: {candidate_strategy_version_id}"
                    )
                if candidate.strategy_version_id != candidate_strategy_version_id:
                    raise ValueError("candidate run is not bound to the proposed strategy version")
                existing = session.scalar(
                    select(PromotionProposalRecord).where(
                        PromotionProposalRecord.baseline_run_id == baseline_run_id,
                        PromotionProposalRecord.candidate_run_id == candidate_run_id,
                        PromotionProposalRecord.candidate_strategy_version_id
                        == candidate_strategy_version_id,
                    )
                )
                if existing is not None:
                    result = _promotion_proposal_dict(existing)
                    result["created"] = False
                    return result
                record = PromotionProposalRecord(
                    id=proposal_id or _new_id(),
                    baseline_run_id=baseline_run_id,
                    candidate_run_id=candidate_run_id,
                    candidate_strategy_version_id=candidate_strategy_version_id,
                    status=status,
                    comparison=comparison_value,
                    statistics=statistics_value,
                    release_gate=gate_value,
                    safety=safety_value,
                    blocked_reasons=reasons,
                    evidence_checksum=evidence_checksum,
                    created_by=created_by,
                )
                session.add(record)
                session.flush()
                creation_details = {
                    "status": status,
                    "evidence_checksum": evidence_checksum,
                }
                _add_strategy_audit(
                    session,
                    action="proposal_created",
                    actor=created_by,
                    proposal_id=record.id,
                    strategy_version_id=candidate_strategy_version_id,
                    idempotency_key=promotion_audit_integrity_key(
                        proposal_id=record.id,
                        strategy_version_id=candidate_strategy_version_id,
                        evidence_checksum=evidence_checksum,
                        action="proposal_created",
                        actor=created_by,
                        details=creation_details,
                    ),
                    details=creation_details,
                )
                result = _promotion_proposal_dict(record)
                result["created"] = True
                return result
        except IntegrityError as exc:
            with self._session_factory() as session:
                existing = session.scalar(
                    select(PromotionProposalRecord).where(
                        PromotionProposalRecord.baseline_run_id == baseline_run_id,
                        PromotionProposalRecord.candidate_run_id == candidate_run_id,
                        PromotionProposalRecord.candidate_strategy_version_id
                        == candidate_strategy_version_id,
                    )
                )
                if existing is not None:
                    result = _promotion_proposal_dict(existing)
                    result["created"] = False
                    return result
            raise EvaluationStoreError("promotion proposal creation conflicted") from exc

    def get_promotion_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            record = session.get(
                PromotionProposalRecord,
                _required_text(proposal_id, "proposal_id"),
            )
            if record is None:
                raise LookupError(f"promotion proposal not found: {proposal_id}")
            return _promotion_proposal_dict(record)

    def list_promotion_proposals(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = _validated_limit(limit, maximum=1000)
        stmt = select(PromotionProposalRecord)
        if status is not None:
            stmt = stmt.where(
                PromotionProposalRecord.status
                == _validated_choice(status, "status", PROMOTION_STATUSES)
            )
        stmt = stmt.order_by(
            PromotionProposalRecord.created_at.desc(), PromotionProposalRecord.id
        ).limit(limit)
        with self._session_factory() as session:
            return [_promotion_proposal_dict(record) for record in session.scalars(stmt).all()]

    def review_promotion_proposal(
        self,
        proposal_id: str,
        *,
        decision: str,
        reviewer: str,
        note: str = "",
    ) -> dict[str, Any]:
        proposal_id = _required_text(proposal_id, "proposal_id")
        decision = _validated_choice(decision, "decision", {"approve", "reject"})
        reviewer = _required_text(reviewer, "reviewer", max_length=200)
        note = _bounded_optional_text(note, "note", max_length=5000)
        target_status = "approved" if decision == "approve" else "rejected"
        with self._transaction() as session:
            record = session.scalar(
                select(PromotionProposalRecord)
                .where(PromotionProposalRecord.id == proposal_id)
                .with_for_update()
            )
            if record is None:
                raise LookupError(f"promotion proposal not found: {proposal_id}")
            if reviewer == record.created_by:
                raise ValueError("proposal creator cannot review their own proposal")
            if record.status == target_status and record.reviewed_by == reviewer:
                return _promotion_proposal_dict(record)
            if record.status != "proposed":
                raise ValueError(f"proposal in status {record.status!r} cannot be reviewed")
            now = _utcnow()
            record.status = target_status
            record.reviewed_by = reviewer
            record.review_note = note
            record.reviewed_at = now
            record.updated_at = now
            session.flush()
            review_details = {
                "note": note,
                "from_status": "proposed",
                "to_status": target_status,
            }
            _add_strategy_audit(
                session,
                action=decision,
                actor=reviewer,
                proposal_id=record.id,
                strategy_version_id=record.candidate_strategy_version_id,
                idempotency_key=promotion_audit_integrity_key(
                    proposal_id=record.id,
                    strategy_version_id=record.candidate_strategy_version_id,
                    evidence_checksum=record.evidence_checksum,
                    action=decision,
                    actor=reviewer,
                    details=review_details,
                ),
                details=review_details,
            )
            return _promotion_proposal_dict(record)

    def activate_promotion_proposal(
        self,
        proposal_id: str,
        *,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        proposal_id = _required_text(proposal_id, "proposal_id")
        actor = _required_text(actor, "actor", max_length=200)
        note = _bounded_optional_text(note, "note", max_length=5000)
        with self._transaction() as session:
            proposal = session.scalar(
                select(PromotionProposalRecord)
                .where(PromotionProposalRecord.id == proposal_id)
                .with_for_update()
            )
            if proposal is None:
                raise LookupError(f"promotion proposal not found: {proposal_id}")
            pointer = session.scalar(
                select(StrategyPointerRecord)
                .where(StrategyPointerRecord.scope == STRATEGY_SOURCE)
                .with_for_update()
            )
            if proposal.status == "activated":
                if pointer is None or pointer.current_strategy_version_id != proposal.candidate_strategy_version_id:
                    raise EvaluationStoreError("activated proposal and strategy pointer disagree")
                audit = session.scalar(
                    select(StrategyAuditEventRecord)
                    .where(
                        StrategyAuditEventRecord.proposal_id == proposal.id,
                        StrategyAuditEventRecord.action == "activate",
                    )
                    .order_by(StrategyAuditEventRecord.created_at.desc())
                )
                return {
                    "proposal": _promotion_proposal_dict(proposal),
                    "pointer": _strategy_pointer_dict(session, pointer),
                    "audit": _strategy_audit_dict(audit) if audit is not None else None,
                    "idempotent": True,
                }
            if proposal.status != "approved":
                raise ValueError("only an approved proposal can be explicitly activated")
            if pointer is None:
                pointer = StrategyPointerRecord(scope=STRATEGY_SOURCE)
                session.add(pointer)
            previous_current = pointer.current_strategy_version_id
            if previous_current != proposal.candidate_strategy_version_id:
                pointer.previous_strategy_version_id = previous_current
                pointer.current_strategy_version_id = proposal.candidate_strategy_version_id
                pointer.generation = int(pointer.generation or 0) + 1
            now = _utcnow()
            pointer.updated_by = actor
            pointer.updated_at = now
            proposal.status = "activated"
            proposal.activated_by = actor
            proposal.activation_note = note
            proposal.activated_at = now
            proposal.updated_at = now
            session.flush()
            activation_details = {
                "note": note,
                "previous_strategy_version_id": previous_current,
                "current_strategy_version_id": proposal.candidate_strategy_version_id,
            }
            audit = _add_strategy_audit(
                session,
                action="activate",
                actor=actor,
                proposal_id=proposal.id,
                strategy_version_id=proposal.candidate_strategy_version_id,
                idempotency_key=promotion_audit_integrity_key(
                    proposal_id=proposal.id,
                    strategy_version_id=proposal.candidate_strategy_version_id,
                    evidence_checksum=proposal.evidence_checksum,
                    action="activate",
                    actor=actor,
                    details=activation_details,
                ),
                details=activation_details,
            )
            return {
                "proposal": _promotion_proposal_dict(proposal),
                "pointer": _strategy_pointer_dict(session, pointer),
                "audit": _strategy_audit_dict(audit),
                "idempotent": False,
            }

    def get_current_strategy(self) -> dict[str, Any]:
        with self._session_factory() as session:
            pointer = session.get(StrategyPointerRecord, STRATEGY_SOURCE)
            return _strategy_pointer_dict(session, pointer)

    def rollback_strategy(
        self,
        *,
        actor: str,
        idempotency_key: str,
        reason: str = "",
        expected_current_strategy_version_id: str | None = None,
    ) -> dict[str, Any]:
        actor = _required_text(actor, "actor", max_length=200)
        idempotency_key = _required_text(
            idempotency_key, "idempotency_key", max_length=200
        )
        reason = _bounded_optional_text(reason, "reason", max_length=5000)
        expected = (
            _required_text(
                expected_current_strategy_version_id,
                "expected_current_strategy_version_id",
            )
            if expected_current_strategy_version_id is not None else None
        )
        try:
            with self._transaction() as session:
                existing = session.scalar(
                    select(StrategyAuditEventRecord).where(
                        StrategyAuditEventRecord.action == "rollback",
                        StrategyAuditEventRecord.idempotency_key == idempotency_key,
                    )
                )
                pointer = session.scalar(
                    select(StrategyPointerRecord)
                    .where(StrategyPointerRecord.scope == STRATEGY_SOURCE)
                    .with_for_update()
                )
                if existing is not None:
                    return {
                        "pointer": _strategy_pointer_dict(session, pointer),
                        "audit": _strategy_audit_dict(existing),
                        "idempotent": True,
                    }
                if pointer is None or not pointer.current_strategy_version_id:
                    raise ValueError("there is no active offline strategy to roll back")
                if expected is not None and pointer.current_strategy_version_id != expected:
                    raise ValueError("active strategy changed; refresh before rollback")
                if not pointer.previous_strategy_version_id:
                    raise ValueError("there is no previous offline strategy to restore")
                before_current = pointer.current_strategy_version_id
                before_previous = pointer.previous_strategy_version_id
                pointer.current_strategy_version_id = before_previous
                pointer.previous_strategy_version_id = before_current
                pointer.generation = int(pointer.generation or 0) + 1
                pointer.updated_by = actor
                pointer.updated_at = _utcnow()
                session.flush()
                details = {
                    "reason": reason,
                    "before_current": before_current,
                    "before_previous": before_previous,
                    "after_current": pointer.current_strategy_version_id,
                    "after_previous": pointer.previous_strategy_version_id,
                }
                audit = _add_strategy_audit(
                    session,
                    action="rollback",
                    actor=actor,
                    strategy_version_id=pointer.current_strategy_version_id,
                    idempotency_key=idempotency_key,
                    details=details,
                )
                return {
                    "pointer": _strategy_pointer_dict(session, pointer),
                    "audit": _strategy_audit_dict(audit),
                    "idempotent": False,
                }
        except IntegrityError:
            # A concurrent request with the same idempotency key won.  The
            # losing transaction was rolled back, including its pointer swap.
            with self._session_factory() as session:
                existing = session.scalar(
                    select(StrategyAuditEventRecord).where(
                        StrategyAuditEventRecord.action == "rollback",
                        StrategyAuditEventRecord.idempotency_key == idempotency_key,
                    )
                )
                pointer = session.get(StrategyPointerRecord, STRATEGY_SOURCE)
                if existing is None:
                    raise
                return {
                    "pointer": _strategy_pointer_dict(session, pointer),
                    "audit": _strategy_audit_dict(existing),
                    "idempotent": True,
                }

    def list_strategy_audit_events(
        self,
        *,
        proposal_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        limit = _validated_limit(limit, maximum=5000)
        stmt = select(StrategyAuditEventRecord)
        if proposal_id is not None:
            stmt = stmt.where(
                StrategyAuditEventRecord.proposal_id
                == _required_text(proposal_id, "proposal_id")
            )
        stmt = stmt.order_by(
            StrategyAuditEventRecord.created_at.desc(), StrategyAuditEventRecord.id
        ).limit(limit)
        with self._session_factory() as session:
            return [_strategy_audit_dict(record) for record in session.scalars(stmt).all()]

    # -- controlled strategy suggestions ---------------------------------

    def create_evolution_suggestion(
        self,
        source_run_id: str,
        *,
        dataset_version_id: str,
        source_strategy_version_id: str | None,
        rule_version: str,
        evidence: Mapping[str, Any],
        suggestion_manifest: Mapping[str, Any],
        rationale: Sequence[str],
        limitations: Sequence[str],
        truth: Mapping[str, Any],
        diagnostics: Mapping[str, Any],
        actor: str,
        idempotency_key: str,
        suggestion_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a content-addressed hypothesis without creating a strategy."""

        from .evolution import (
            canonical_checksum,
            validate_suggestion_manifest,
            verify_evidence_checksums,
        )
        from .strategy import canonical_manifest_json

        source_run_id = _required_text(source_run_id, "source_run_id")
        dataset_version_id = _required_text(dataset_version_id, "dataset_version_id")
        source_strategy_version_id = (
            _required_text(source_strategy_version_id, "source_strategy_version_id")
            if source_strategy_version_id is not None
            else None
        )
        rule_version = _required_text(rule_version, "rule_version", max_length=64)
        actor = _required_text(actor, "actor", max_length=200)
        idempotency_key = _required_text(
            idempotency_key, "idempotency_key", max_length=200
        )
        evidence_value = _jsonable(evidence)
        if not isinstance(evidence_value, dict):
            raise TypeError("evidence must be an object")
        verify_evidence_checksums(evidence_value)
        evidence_checksum = canonical_checksum(evidence_value)
        manifest_value = validate_suggestion_manifest(suggestion_manifest)
        manifest_canonical_json = canonical_manifest_json(manifest_value)
        manifest_checksum = hashlib.sha256(
            manifest_canonical_json.encode("utf-8")
        ).hexdigest()
        rationale_value = [
            _required_text(str(item), "rationale", max_length=5000)
            for item in rationale
        ]
        limitations_value = [
            _required_text(str(item), "limitation", max_length=5000)
            for item in limitations
        ]
        if not rationale_value:
            raise ValueError("at least one evidence-based rationale is required")
        if not limitations_value:
            raise ValueError("at least one limitation is required")
        truth_value = _jsonable(truth)
        if not isinstance(truth_value, dict):
            raise TypeError("truth must be an object")
        if truth_value.get("no_auto_apply") is not True:
            raise ValueError("truth.no_auto_apply must be true")
        if truth_value.get("can_claim_online_improvement") is not False:
            raise ValueError("an evolution suggestion cannot claim online improvement")
        diagnostics_value = _jsonable(diagnostics)
        request_payload = {
            "source_run_id": source_run_id,
            "dataset_version_id": dataset_version_id,
            "source_strategy_version_id": source_strategy_version_id,
            "rule_version": rule_version,
            "evidence_checksum": evidence_checksum,
            "manifest_checksum": manifest_checksum,
            "actor": actor,
        }
        request_checksum = canonical_checksum(request_payload)

        try:
            with self._transaction() as session:
                replay = _evolution_idempotency_replay(
                    session,
                    action="evolution_create",
                    idempotency_key=idempotency_key,
                    request_checksum=request_checksum,
                )
                if replay is not None:
                    record = session.get(EvolutionSuggestionRecord, replay.suggestion_id)
                    if record is None:
                        raise EvaluationStoreError("evolution audit references a missing suggestion")
                    _verify_evolution_record(session, record)
                    result = _evolution_suggestion_dict(record)
                    result.update(created=False, idempotent=True)
                    return result

                run = session.get(EvalRunRecord, source_run_id)
                if run is None:
                    raise LookupError(f"evaluation run not found: {source_run_id}")
                if run.status != "completed":
                    raise ValueError("source evaluation run must be completed")
                if run.dataset_version_id != dataset_version_id:
                    raise ValueError("source run and evidence dataset version do not match")
                if run.strategy_version_id != source_strategy_version_id:
                    raise ValueError("source run and evidence strategy version do not match")
                existing = session.scalar(
                    select(EvolutionSuggestionRecord).where(
                        EvolutionSuggestionRecord.source_run_id == source_run_id,
                        EvolutionSuggestionRecord.rule_version == rule_version,
                    ).with_for_update()
                )
                if existing is not None:
                    _verify_evolution_record(session, existing)
                    if (
                        existing.evidence_checksum != evidence_checksum
                        or existing.manifest_checksum != manifest_checksum
                    ):
                        raise ValueError(
                            "source evidence changed after the deterministic suggestion was created"
                        )
                    audit = _add_evolution_audit(
                        session,
                        suggestion=existing,
                        action="evolution_create",
                        actor=actor,
                        idempotency_key=idempotency_key,
                        request_checksum=request_checksum,
                        details={
                            "created": False,
                            "deterministic_duplicate": True,
                            "request": request_payload,
                            "from_status": existing.status,
                            "to_status": existing.status,
                            "from_generation": existing.generation,
                            "to_generation": existing.generation,
                        },
                    )
                    _verify_evolution_record(session, existing)
                    result = _evolution_suggestion_dict(existing)
                    result.update(
                        created=False,
                        idempotent=False,
                        audit=_evolution_audit_dict(audit),
                    )
                    return result

                record = EvolutionSuggestionRecord(
                    id=suggestion_id or _new_id(),
                    source_run_id=source_run_id,
                    dataset_version_id=dataset_version_id,
                    source_strategy_version_id=source_strategy_version_id,
                    status="proposed",
                    generation=0,
                    audit_event_count=0,
                    audit_head_checksum="",
                    rule_version=rule_version,
                    evidence=evidence_value,
                    evidence_checksum=evidence_checksum,
                    suggestion_manifest=manifest_value,
                    manifest_canonical_json=manifest_canonical_json,
                    manifest_checksum=manifest_checksum,
                    rationale=rationale_value,
                    limitations=limitations_value,
                    truth=truth_value,
                    diagnostics=diagnostics_value,
                    no_auto_apply=True,
                    created_by=actor,
                )
                session.add(record)
                session.flush()
                audit = _add_evolution_audit(
                    session,
                    suggestion=record,
                    action="evolution_create",
                    actor=actor,
                    idempotency_key=idempotency_key,
                    request_checksum=request_checksum,
                    details={
                        "created": True,
                        "request": request_payload,
                        "from_status": None,
                        "to_status": "proposed",
                        "from_generation": -1,
                        "to_generation": 0,
                        "rule_version": rule_version,
                        "evidence_checksum": evidence_checksum,
                        "manifest_checksum": manifest_checksum,
                    },
                )
                _verify_evolution_record(session, record)
                result = _evolution_suggestion_dict(record)
                result.update(
                    created=True,
                    idempotent=False,
                    audit=_evolution_audit_dict(audit),
                )
                return result
        except IntegrityError as exc:
            raise EvaluationStoreError("evolution suggestion creation conflicted") from exc

    def get_evolution_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        suggestion_id = _required_text(suggestion_id, "suggestion_id")
        with self._session_factory() as session:
            record = session.get(EvolutionSuggestionRecord, suggestion_id)
            if record is None:
                raise LookupError(f"evolution suggestion not found: {suggestion_id}")
            _verify_evolution_record(session, record)
            return _evolution_suggestion_dict(record)

    def list_evolution_suggestions(
        self,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = _validated_limit(limit, maximum=1000)
        stmt = select(EvolutionSuggestionRecord)
        if status is not None:
            stmt = stmt.where(
                EvolutionSuggestionRecord.status
                == _validated_choice(status, "status", EVOLUTION_STATUSES)
            )
        stmt = stmt.order_by(
            EvolutionSuggestionRecord.created_at.desc(), EvolutionSuggestionRecord.id
        ).limit(limit)
        with self._session_factory() as session:
            records = session.scalars(stmt).all()
            for record in records:
                _verify_evolution_record(session, record)
            return [_evolution_suggestion_dict(record) for record in records]

    def review_evolution_suggestion(
        self,
        suggestion_id: str,
        *,
        decision: str,
        reviewer: str,
        note: str,
        expected_generation: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from .evolution import canonical_checksum

        suggestion_id = _required_text(suggestion_id, "suggestion_id")
        decision = _validated_choice(decision, "decision", {"accept", "reject"})
        reviewer = _required_text(reviewer, "reviewer", max_length=200)
        note = _bounded_optional_text(note, "note", max_length=5000)
        generation = _nonnegative_int(expected_generation, "expected_generation")
        idempotency_key = _required_text(
            idempotency_key, "idempotency_key", max_length=200
        )
        request_payload = {
            "suggestion_id": suggestion_id,
            "decision": decision,
            "reviewer": reviewer,
            "note": note,
            "expected_generation": generation,
        }
        request_checksum = canonical_checksum(request_payload)
        with self._transaction() as session:
            replay = _evolution_idempotency_replay(
                session,
                action="evolution_review",
                idempotency_key=idempotency_key,
                request_checksum=request_checksum,
            )
            if replay is not None:
                record = session.get(EvolutionSuggestionRecord, replay.suggestion_id)
                if record is None:
                    raise EvaluationStoreError("evolution audit references a missing suggestion")
                _verify_evolution_record(session, record)
                return {
                    "suggestion": _evolution_suggestion_dict(record),
                    "audit": _evolution_audit_dict(replay),
                    "idempotent": True,
                }
            record = session.scalar(
                select(EvolutionSuggestionRecord)
                .where(EvolutionSuggestionRecord.id == suggestion_id)
                .with_for_update()
            )
            if record is None:
                raise LookupError(f"evolution suggestion not found: {suggestion_id}")
            _verify_evolution_record(session, record)
            if reviewer == record.created_by:
                raise ValueError("suggestion creator cannot review their own suggestion")
            if record.generation != generation:
                raise ValueError(
                    f"generation conflict: expected {generation}, current {record.generation}"
                )
            if record.status != "proposed":
                raise ValueError(f"suggestion in status {record.status!r} cannot be reviewed")
            now = _utcnow()
            previous = record.status
            record.status = "accepted" if decision == "accept" else "rejected"
            record.generation += 1
            record.reviewed_by = reviewer
            record.review_note = note
            record.reviewed_at = now
            record.updated_at = now
            session.flush()
            audit = _add_evolution_audit(
                session,
                suggestion=record,
                action="evolution_review",
                actor=reviewer,
                idempotency_key=idempotency_key,
                request_checksum=request_checksum,
                details={
                    "request": request_payload,
                    "decision": decision,
                    "note": note,
                    "from_status": previous,
                    "to_status": record.status,
                    "from_generation": generation,
                    "to_generation": record.generation,
                },
            )
            _verify_evolution_record(session, record)
            return {
                "suggestion": _evolution_suggestion_dict(record),
                "audit": _evolution_audit_dict(audit),
                "idempotent": False,
            }

    def materialize_evolution_suggestion(
        self,
        suggestion_id: str,
        *,
        name: str,
        actor: str,
        expected_generation: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Explicitly copy an accepted hypothesis into an immutable P2 version."""

        from .evolution import canonical_checksum

        suggestion_id = _required_text(suggestion_id, "suggestion_id")
        name = _required_text(name, "name", max_length=200)
        actor = _required_text(actor, "actor", max_length=200)
        generation = _nonnegative_int(expected_generation, "expected_generation")
        idempotency_key = _required_text(
            idempotency_key, "idempotency_key", max_length=200
        )
        request_payload = {
            "suggestion_id": suggestion_id,
            "name": name,
            "actor": actor,
            "expected_generation": generation,
        }
        request_checksum = canonical_checksum(request_payload)
        with self._transaction() as session:
            replay = _evolution_idempotency_replay(
                session,
                action="evolution_materialize",
                idempotency_key=idempotency_key,
                request_checksum=request_checksum,
            )
            if replay is not None:
                record = session.get(EvolutionSuggestionRecord, replay.suggestion_id)
                if record is None or not record.materialized_strategy_version_id:
                    raise EvaluationStoreError("materialization audit is inconsistent")
                strategy = session.get(
                    StrategyVersionRecord, record.materialized_strategy_version_id
                )
                if strategy is None:
                    raise EvaluationStoreError("materialized strategy version is missing")
                _verify_evolution_record(session, record)
                return {
                    "suggestion": _evolution_suggestion_dict(record),
                    "strategy": _strategy_version_dict(strategy),
                    "audit": _evolution_audit_dict(replay),
                    "idempotent": True,
                    "created_strategy": False,
                    **_evolution_next_steps(),
                }
            record = session.scalar(
                select(EvolutionSuggestionRecord)
                .where(EvolutionSuggestionRecord.id == suggestion_id)
                .with_for_update()
            )
            if record is None:
                raise LookupError(f"evolution suggestion not found: {suggestion_id}")
            _verify_evolution_record(session, record)
            if record.generation != generation:
                raise ValueError(
                    f"generation conflict: expected {generation}, current {record.generation}"
                )
            if record.status != "accepted":
                raise ValueError("only an accepted suggestion can be materialized")
            if record.materialized_strategy_version_id:
                raise ValueError("suggestion has already been materialized")

            strategy = session.scalar(
                select(StrategyVersionRecord).where(
                    StrategyVersionRecord.manifest_checksum == record.manifest_checksum
                )
            )
            created_strategy = strategy is None
            if strategy is None:
                latest_version = session.scalar(select(func.max(StrategyVersionRecord.version)))
                strategy = StrategyVersionRecord(
                    id=_new_id(),
                    version=int(latest_version or 0) + 1,
                    name=name,
                    manifest=_jsonable(record.suggestion_manifest),
                    manifest_canonical_json=record.manifest_canonical_json,
                    manifest_checksum=record.manifest_checksum,
                    creator=actor,
                    source=STRATEGY_SOURCE,
                )
                session.add(strategy)
                session.flush()
            now = _utcnow()
            record.materialized_strategy_version_id = strategy.id
            record.materialized_by = actor
            record.materialized_at = now
            record.generation += 1
            record.updated_at = now
            session.flush()
            audit = _add_evolution_audit(
                session,
                suggestion=record,
                action="evolution_materialize",
                actor=actor,
                idempotency_key=idempotency_key,
                request_checksum=request_checksum,
                details={
                    "request": request_payload,
                    "strategy_version_id": strategy.id,
                    "created_strategy": created_strategy,
                    "from_status": "accepted",
                    "to_status": "accepted",
                    "from_generation": generation,
                    "to_generation": record.generation,
                    "auto_promotion": False,
                    "auto_activation": False,
                    "auto_deployment": False,
                },
            )
            _verify_evolution_record(session, record)
            return {
                "suggestion": _evolution_suggestion_dict(record),
                "strategy": _strategy_version_dict(strategy),
                "audit": _evolution_audit_dict(audit),
                "idempotent": False,
                "created_strategy": created_strategy,
                **_evolution_next_steps(),
            }

    def list_evolution_audit_events(
        self,
        *,
        suggestion_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        limit = _validated_limit(limit, maximum=5000)
        stmt = select(EvolutionAuditEventRecord)
        if suggestion_id is not None:
            stmt = stmt.where(
                EvolutionAuditEventRecord.suggestion_id
                == _required_text(suggestion_id, "suggestion_id")
            )
        stmt = stmt.order_by(
            EvolutionAuditEventRecord.created_at.desc(), EvolutionAuditEventRecord.id
        ).limit(limit)
        with self._session_factory() as session:
            records = session.scalars(stmt).all()
            suggestion_ids = {record.suggestion_id for record in records}
            for current_suggestion_id in suggestion_ids:
                suggestion = session.get(EvolutionSuggestionRecord, current_suggestion_id)
                if suggestion is None:
                    raise ImmutableEvolutionSuggestionError(
                        "evolution audit references a missing suggestion"
                    )
                _verify_evolution_record(session, suggestion)
            return [_evolution_audit_dict(record) for record in records]


Store = EvaluationStore


def _create_engine(database_url: str) -> Engine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            # Share one in-memory connection across request/test threads.
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    return engine


def _upgrade_schema(engine: Engine, database_url: str) -> None:
    """Upgrade file/remote databases with Alembic; keep in-memory tests simple."""

    if database_url in {"sqlite://", "sqlite:///:memory:"}:
        Base.metadata.create_all(engine)
        return
    final_root = Path(__file__).resolve().parents[2]
    alembic_ini = final_root / "alembic.ini"
    if not alembic_ini.is_file():
        Base.metadata.create_all(engine)
        return
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        Base.metadata.create_all(engine)
        return

    existing_tables = set(sa_inspect(engine).get_table_names())
    business_tables = set(Base.metadata.tables)
    config = Config(str(alembic_ini))
    config.attributes["database_url_override"] = database_url
    if business_tables <= existing_tables and "alembic_version" not in existing_tables:
        # Adopt databases created by the pre-Alembic evaluation prototype.
        command.stamp(config, "head")
        return
    if existing_tables - {"alembic_version"} and "alembic_version" not in existing_tables:
        missing = sorted(business_tables - existing_tables)
        raise EvaluationStoreError(
            "evaluation schema is partial and has no Alembic revision; "
            f"missing tables: {', '.join(missing)}"
        )
    command.upgrade(config, "head")


def _default_database_url() -> str:
    final_root = Path(__file__).resolve().parents[2]
    runtime = final_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{(runtime / 'evaluation.db').as_posix()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _required_text(value: str, field: str, *, max_length: int | None = None) -> str:
    text_value = (value or "").strip()
    if not text_value:
        raise ValueError(f"{field} is required")
    if max_length is not None and len(text_value) > max_length:
        raise ValueError(f"{field} must not exceed {max_length} characters")
    return text_value


def _bounded_optional_text(value: str | None, field: str, *, max_length: int) -> str:
    text_value = str(value or "").strip()
    if len(text_value) > max_length:
        raise ValueError(f"{field} must not exceed {max_length} characters")
    return text_value


def _validated_choice(value: str, field: str, choices: Sequence[str] | frozenset[str]) -> str:
    text_value = _required_text(value, field).lower()
    if text_value not in choices:
        raise ValueError(f"unsupported {field}: {value}")
    return text_value


def _validated_limit(limit: int, *, maximum: int = 1000) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return limit


def _nonnegative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _normalise_case_payload(case: Mapping[str, Any] | Any) -> dict[str, Any]:
    if hasattr(case, "model_dump"):
        case = case.model_dump(mode="json")
    if not isinstance(case, Mapping):
        raise TypeError("evaluation cases must be mappings or Pydantic models")
    value = _jsonable(dict(case))
    if not isinstance(value, dict):
        raise TypeError("evaluation case payload must be a JSON object")
    return value


def _case_key(payload: Mapping[str, Any], position: int) -> str:
    raw = payload.get("case_id") or payload.get("id") or f"case-{position + 1}"
    return _required_text(str(raw), "case_id", max_length=200)


def _payload_checksum(payloads: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        payloads,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_checksum(value: Any) -> str:
    canonical = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def promotion_audit_integrity_key(
    *,
    proposal_id: str,
    strategy_version_id: str,
    evidence_checksum: str,
    action: str,
    actor: str,
    details: Mapping[str, Any],
) -> str:
    """Content-address one promotion lifecycle fact without a schema change."""

    checksum = _canonical_checksum(
        {
            "schema_version": "promotion-lifecycle-audit-v1",
            "proposal_id": proposal_id,
            "strategy_version_id": strategy_version_id,
            "evidence_checksum": evidence_checksum,
            "action": action,
            "actor": actor,
            "details": details,
        }
    )
    return f"promotion-integrity-v1:{checksum}"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Path)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _version_case_count(session: Session, version_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(EvalCaseRecord.id)).where(
                EvalCaseRecord.dataset_version_id == version_id
            )
        )
        or 0
    )


def _dataset_dict(record: DatasetRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "metadata": _jsonable(record.metadata_json),
        "created_at": _iso(record.created_at),
    }


def _version_dict(record: DatasetVersionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "dataset_id": record.dataset_id,
        "version": record.version,
        "checksum": record.checksum,
        "metadata": _jsonable(record.metadata_json),
        "created_at": _iso(record.created_at),
    }


def _strategy_version_dict(record: StrategyVersionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "version": record.version,
        "name": record.name,
        "manifest": _jsonable(record.manifest),
        "manifest_canonical_json": record.manifest_canonical_json,
        "manifest_checksum": record.manifest_checksum,
        "creator": record.creator,
        "source": record.source,
        "created_at": _iso(record.created_at),
    }


def _case_dict(record: EvalCaseRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "dataset_version_id": record.dataset_version_id,
        "case_id": record.case_key,
        "position": record.position,
        "payload": _jsonable(record.payload),
        "created_at": _iso(record.created_at),
    }


def _run_dict(record: EvalRunRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "dataset_version_id": record.dataset_version_id,
        "strategy_version_id": record.strategy_version_id,
        "name": record.name,
        "status": record.status,
        "config": _jsonable(record.config),
        "summary": _jsonable(record.summary),
        "metadata": _jsonable(record.metadata_json),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "started_at": _iso(record.started_at),
        "completed_at": _iso(record.completed_at),
    }


def _case_run_dict(record: CaseRunRecord, case_key: str) -> dict[str, Any]:
    return {
        "id": record.id,
        "run_id": record.eval_run_id,
        "eval_case_id": record.eval_case_id,
        "case_id": case_key,
        "status": record.status,
        "output": _jsonable(record.output),
        "metrics": _jsonable(record.metrics),
        "trace": _jsonable(record.trace),
        "error": _jsonable(record.error),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }


def _badcase_dict(record: BadcaseRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "run_id": record.eval_run_id,
        "case_run_id": record.case_run_id,
        "eval_case_id": record.eval_case_id,
        "category": record.category,
        "severity": record.severity,
        "status": record.status,
        "owner": record.owner,
        "details": _jsonable(record.details),
        "resolution": _jsonable(record.resolution),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "resolved_at": _iso(record.resolved_at),
    }


def _annotation_dict(record: HumanAnnotationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "case_run_id": record.case_run_id,
        "badcase_id": record.badcase_id,
        "eval_case_id": record.eval_case_id,
        "annotator": record.annotator,
        "annotation": _jsonable(record.annotation),
        "created_at": _iso(record.created_at),
    }


def _promotion_proposal_dict(record: PromotionProposalRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "baseline_run_id": record.baseline_run_id,
        "candidate_run_id": record.candidate_run_id,
        "candidate_strategy_version_id": record.candidate_strategy_version_id,
        "status": record.status,
        "ready_for_review": record.status == "proposed",
        "comparison": _jsonable(record.comparison),
        "statistics": _jsonable(record.statistics),
        "release_gate": _jsonable(record.release_gate),
        "safety": _jsonable(record.safety),
        "blocked_reasons": _jsonable(record.blocked_reasons),
        "evidence_checksum": record.evidence_checksum,
        "created_by": record.created_by,
        "reviewed_by": record.reviewed_by,
        "review_note": record.review_note,
        "reviewed_at": _iso(record.reviewed_at),
        "activated_by": record.activated_by,
        "activation_note": record.activation_note,
        "activated_at": _iso(record.activated_at),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "scope": "offline_evaluation",
        "auto_activate": False,
    }


def _strategy_pointer_dict(
    session: Session,
    pointer: StrategyPointerRecord | None,
) -> dict[str, Any]:
    current_id = pointer.current_strategy_version_id if pointer is not None else None
    previous_id = pointer.previous_strategy_version_id if pointer is not None else None
    current = session.get(StrategyVersionRecord, current_id) if current_id else None
    previous = session.get(StrategyVersionRecord, previous_id) if previous_id else None
    return {
        "scope": "offline_evaluation",
        "current_strategy_version_id": current_id,
        "previous_strategy_version_id": previous_id,
        "current_strategy": _strategy_version_dict(current) if current is not None else None,
        "previous_strategy": _strategy_version_dict(previous) if previous is not None else None,
        "generation": int(pointer.generation or 0) if pointer is not None else 0,
        "updated_by": pointer.updated_by if pointer is not None else "",
        "updated_at": _iso(pointer.updated_at) if pointer is not None else None,
    }


def _strategy_audit_dict(record: StrategyAuditEventRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "action": record.action,
        "actor": record.actor,
        "proposal_id": record.proposal_id,
        "strategy_version_id": record.strategy_version_id,
        "idempotency_key": record.idempotency_key,
        "details": _jsonable(record.details),
        "created_at": _iso(record.created_at),
    }


def _evolution_suggestion_dict(record: EvolutionSuggestionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "source_run_id": record.source_run_id,
        "dataset_version_id": record.dataset_version_id,
        "source_strategy_version_id": record.source_strategy_version_id,
        "status": record.status,
        "generation": int(record.generation),
        "audit_event_count": int(record.audit_event_count),
        "audit_head_checksum": record.audit_head_checksum,
        "rule_version": record.rule_version,
        "evidence": _jsonable(record.evidence),
        "evidence_checksum": record.evidence_checksum,
        "suggestion_manifest": _jsonable(record.suggestion_manifest),
        "manifest_canonical_json": record.manifest_canonical_json,
        "manifest_checksum": record.manifest_checksum,
        "rationale": _jsonable(record.rationale),
        "limitations": _jsonable(record.limitations),
        "truth": _jsonable(record.truth),
        "diagnostics": _jsonable(record.diagnostics),
        "no_auto_apply": bool(record.no_auto_apply),
        "created_by": record.created_by,
        "reviewed_by": record.reviewed_by,
        "review_note": record.review_note,
        "reviewed_at": _iso(record.reviewed_at),
        "materialized_strategy_version_id": record.materialized_strategy_version_id,
        "materialized_by": record.materialized_by,
        "materialized_at": _iso(record.materialized_at),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "scope": "offline_evaluation",
        "kind": "experiment_hypothesis",
        "auto_apply": False,
        "auto_promotion": False,
        "auto_activation": False,
        "auto_deployment": False,
    }


def _evolution_audit_dict(record: EvolutionAuditEventRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "suggestion_id": record.suggestion_id,
        "action": record.action,
        "actor": record.actor,
        "idempotency_key": record.idempotency_key,
        "request_checksum": record.request_checksum,
        "sequence": int(record.sequence),
        "previous_event_checksum": record.previous_event_checksum,
        "event_checksum": record.event_checksum,
        "details": _jsonable(record.details),
        "created_at": _iso(record.created_at),
    }


from .evolution_integrity import (
    verify_evolution_record as _verify_evolution_record,
)

def _empty_evolution_state() -> dict[str, Any]:
    return {
        "status": "proposed",
        "generation": 0,
        "reviewed_by": "",
        "review_note": "",
        "reviewed_at": None,
        "materialized_strategy_version_id": None,
        "materialized_by": "",
        "materialized_at": None,
    }


def _evolution_state_snapshot(record: EvolutionSuggestionRecord) -> dict[str, Any]:
    return {
        "status": record.status,
        "generation": int(record.generation),
        "reviewed_by": record.reviewed_by,
        "review_note": record.review_note,
        "reviewed_at": _canonical_audit_time(record.reviewed_at),
        "materialized_strategy_version_id": record.materialized_strategy_version_id,
        "materialized_by": record.materialized_by,
        "materialized_at": _canonical_audit_time(record.materialized_at),
    }


def _canonical_audit_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    normalized = normalized.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _evolution_event_checksum(record: EvolutionAuditEventRecord) -> str:
    from .evolution import canonical_checksum

    return canonical_checksum(
        {
            "id": record.id,
            "suggestion_id": record.suggestion_id,
            "action": record.action,
            "actor": record.actor,
            "idempotency_key": record.idempotency_key,
            "request_checksum": record.request_checksum,
            "sequence": int(record.sequence),
            "previous_event_checksum": record.previous_event_checksum,
            "details": _jsonable(record.details),
            "created_at": _canonical_audit_time(record.created_at),
        }
    )


def _evolution_idempotency_replay(
    session: Session,
    *,
    action: str,
    idempotency_key: str,
    request_checksum: str,
) -> EvolutionAuditEventRecord | None:
    record = session.scalar(
        select(EvolutionAuditEventRecord).where(
            EvolutionAuditEventRecord.action == action,
            EvolutionAuditEventRecord.idempotency_key == idempotency_key,
        )
    )
    if record is None:
        return None
    if record.request_checksum != request_checksum:
        raise ValueError("idempotency key was already used with a different request")
    return record


def _add_evolution_audit(
    session: Session,
    *,
    suggestion: EvolutionSuggestionRecord,
    action: str,
    actor: str,
    idempotency_key: str,
    request_checksum: str,
    details: Mapping[str, Any] | None = None,
) -> EvolutionAuditEventRecord:
    sequence = int(suggestion.audit_event_count) + 1
    previous_event_checksum = (
        suggestion.audit_head_checksum or _EVOLUTION_AUDIT_GENESIS
    )
    details_value = _jsonable(details or {})
    details_value["state"] = _evolution_state_snapshot(suggestion)
    created_at = _utcnow()
    record = EvolutionAuditEventRecord(
        id=_new_id(),
        suggestion_id=_required_text(suggestion.id, "suggestion_id"),
        action=_required_text(action, "action", max_length=40),
        actor=_required_text(actor, "actor", max_length=200),
        idempotency_key=_required_text(
            idempotency_key, "idempotency_key", max_length=200
        ),
        request_checksum=_required_text(
            request_checksum, "request_checksum", max_length=64
        ),
        sequence=sequence,
        previous_event_checksum=previous_event_checksum,
        event_checksum="",
        details=details_value,
        created_at=created_at,
    )
    # Compute over every security-relevant event field.  The hash is then both
    # linked by the next event and committed by the suggestion row's head.
    record.event_checksum = _evolution_event_checksum(record)
    session.add(record)
    suggestion.audit_event_count = sequence
    suggestion.audit_head_checksum = record.event_checksum
    session.flush()
    return record


def _evolution_next_steps() -> dict[str, Any]:
    return {
        "next_required_steps": [
            "create_full_candidate_eval_run_on_same_dataset_version",
            "compare_complete_baseline_and_candidate_runs",
            "pass_p2_statistics_release_and_safety_gates",
            "obtain_human_promotion_approval",
            "use_p3_controlled_online_experiment_before_any_claim",
        ],
        "promotion_proposal_created": False,
        "strategy_activated": False,
        "online_deployment_created": False,
    }


def _add_strategy_audit(
    session: Session,
    *,
    action: str,
    actor: str,
    proposal_id: str | None = None,
    strategy_version_id: str | None = None,
    idempotency_key: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> StrategyAuditEventRecord:
    record = StrategyAuditEventRecord(
        id=_new_id(),
        action=_required_text(action, "action", max_length=32),
        actor=_required_text(actor, "actor", max_length=200),
        proposal_id=proposal_id,
        strategy_version_id=strategy_version_id,
        idempotency_key=idempotency_key,
        details=_jsonable(details or {}),
    )
    session.add(record)
    session.flush()
    return record


def _is_bad_result(status: str, metrics: Any, passed: bool | None) -> bool:
    if passed is not None:
        return not passed
    if status in {"failed", "error"}:
        return True
    if isinstance(metrics, Mapping):
        for key in ("passed", "success", "overall_passed"):
            value = metrics.get(key)
            if isinstance(value, bool):
                return not value
    return False


def _create_or_get_badcase(
    session: Session,
    case_run: CaseRunRecord,
    *,
    category: str,
    severity: str,
    details: Any,
) -> BadcaseRecord:
    existing = session.scalar(
        select(BadcaseRecord).where(BadcaseRecord.case_run_id == case_run.id)
    )
    if existing is not None:
        return existing
    record = BadcaseRecord(
        id=_new_id(),
        eval_run_id=case_run.eval_run_id,
        case_run_id=case_run.id,
        eval_case_id=case_run.eval_case_id,
        category=_required_text(category, "category", max_length=100),
        severity=_validated_choice(severity, "severity", BADCASE_SEVERITIES),
        status="open",
        details=_jsonable(details),
        resolution={},
    )
    session.add(record)
    session.flush()
    return record


def _require_existing_target(
    session: Session,
    model: type[Base],
    target_id: str | None,
    label: str,
) -> None:
    if target_id and session.get(model, target_id) is None:
        raise LookupError(f"{label} not found: {target_id}")


__all__ = [
    "BADCASE_SEVERITIES",
    "BADCASE_STATUSES",
    "CASE_RUN_STATUSES",
    "EVOLUTION_STATUSES",
    "PROMOTION_STATUSES",
    "RUN_STATUSES",
    "STRATEGY_SOURCE",
    "TERMINAL_CASE_RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "EvaluationStore",
    "EvaluationStoreError",
    "EvolutionAuditEventRecord",
    "EvolutionSuggestionRecord",
    "ImmutableDatasetVersionError",
    "ImmutableEvaluationEvidenceError",
    "ImmutableEvolutionSuggestionError",
    "PromotionProposalRecord",
    "StrategyAuditEventRecord",
    "StrategyPointerRecord",
    "StrategyVersionRecord",
    "Store",
    "promotion_audit_integrity_key",
]
