"""Transactional tenant-scoped store for online experiment control data."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from internal.evaluation.store import EvaluationStore

from .models import (
    ACTIVE_EXPERIMENT_STATUSES,
    ExperimentAssignmentRecord,
    ExperimentAuditEventRecord,
    ExperimentExposureRecord,
    ExperimentMonitorSnapshotRecord,
    ExperimentOutcomeRecord,
    ExperimentSafetyOutboxRecord,
    OnlineExperimentRecord,
    OnlineStrategyDeploymentRecord,
)


class ExperimentStoreError(RuntimeError):
    pass


class ExperimentConflictError(ExperimentStoreError):
    pass


class GenerationConflictError(ExperimentConflictError):
    pass


class IdempotencyConflictError(ExperimentConflictError):
    pass


class ExperimentStore:
    """SQLAlchemy store; every externally addressable read includes tenant."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if engine is None:
            self._owner = EvaluationStore(database_url=database_url)
            self.engine = self._owner.engine
        else:
            self._owner = None
            self.engine = engine
        self._sessions = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=Session,
        )

    def close(self) -> None:
        if self._owner is not None:
            self._owner.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- immutable deployments --------------------------------------------

    def create_deployment(
        self,
        tenant_id: str,
        *,
        source_proposal_id: str,
        source_strategy_version_id: str,
        source_manifest_checksum: str,
        compiled_overrides: Mapping[str, Any],
        compiler_version: str,
        compiled_checksum: str,
        actor: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        tenant_id = _required(tenant_id, "tenant_id")
        action = "create_deployment"
        now = now or _utcnow()
        try:
            with self.transaction() as session:
                replay = _idempotent_replay(
                    session, tenant_id, action, idempotency_key, request_hash
                )
                if replay is not None:
                    return replay
                duplicate = session.scalar(
                    select(OnlineStrategyDeploymentRecord).where(
                        OnlineStrategyDeploymentRecord.tenant_id == tenant_id,
                        OnlineStrategyDeploymentRecord.source_strategy_version_id
                        == source_strategy_version_id,
                        OnlineStrategyDeploymentRecord.source_manifest_checksum
                        == source_manifest_checksum,
                    )
                )
                created = duplicate is None
                if duplicate is None:
                    deployment_values = {
                        "tenant_id": tenant_id,
                        "source_proposal_id": _required(
                            source_proposal_id, "source_proposal_id"
                        ),
                        "source_strategy_version_id": _required(
                            source_strategy_version_id, "source_strategy_version_id"
                        ),
                        "source_manifest_checksum": _required(
                            source_manifest_checksum, "source_manifest_checksum"
                        ),
                        "compiled_overrides": _json(compiled_overrides),
                        "compiler_version": _required(
                            compiler_version, "compiler_version"
                        ),
                        "compiled_checksum": _required(
                            compiled_checksum, "compiled_checksum"
                        ),
                    }
                    duplicate = OnlineStrategyDeploymentRecord(
                        id=_id(),
                        **deployment_values,
                        record_checksum=canonical_checksum(deployment_values),
                        created_by=_required(actor, "actor"),
                        created_at=now,
                    )
                    session.add(duplicate)
                    session.flush()
                result = _deployment_dict(duplicate)
                result["created"] = created
                _add_audit(
                    session,
                    tenant_id=tenant_id,
                    action=action,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    deployment_id=duplicate.id,
                    result=result,
                    details={"source_proposal_id": source_proposal_id},
                    now=now,
                )
                return result
        except IntegrityError as exc:
            replay = self.replay_idempotent(
                tenant_id, action, idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            raise ExperimentConflictError("deployment creation conflicted") from exc

    def get_deployment(self, tenant_id: str, deployment_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            record = session.scalar(
                select(OnlineStrategyDeploymentRecord).where(
                    OnlineStrategyDeploymentRecord.tenant_id == _required(tenant_id, "tenant_id"),
                    OnlineStrategyDeploymentRecord.id == _required(deployment_id, "deployment_id"),
                )
            )
            if record is None:
                raise LookupError(f"online strategy deployment not found: {deployment_id}")
            return _deployment_dict(record)

    def list_deployments(self, tenant_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(
                select(OnlineStrategyDeploymentRecord)
                .where(OnlineStrategyDeploymentRecord.tenant_id == _required(tenant_id, "tenant_id"))
                .order_by(OnlineStrategyDeploymentRecord.created_at.desc())
                .limit(_limit(limit))
            ).all()
            return [_deployment_dict(item) for item in records]

    # -- experiment lifecycle ---------------------------------------------

    def create_experiment(
        self,
        tenant_id: str,
        values: Mapping[str, Any],
        *,
        actor: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        tenant_id = _required(tenant_id, "tenant_id")
        action = "create_experiment"
        now = now or _utcnow()
        try:
            with self.transaction() as session:
                replay = _idempotent_replay(
                    session, tenant_id, action, idempotency_key, request_hash
                )
                if replay is not None:
                    return replay
                deployment_id = str(values["candidate_deployment_id"])
                deployment = session.scalar(
                    select(OnlineStrategyDeploymentRecord).where(
                        OnlineStrategyDeploymentRecord.id == deployment_id,
                        OnlineStrategyDeploymentRecord.tenant_id == tenant_id,
                    )
                )
                if deployment is None:
                    raise LookupError(f"online strategy deployment not found: {deployment_id}")
                record = OnlineExperimentRecord(
                    id=_id(),
                    tenant_id=tenant_id,
                    name=_required(str(values["name"]), "name", 200),
                    hypothesis=str(values.get("hypothesis") or "").strip(),
                    surface="rag_chat",
                    status="draft",
                    candidate_deployment_id=deployment_id,
                    control_overrides={},
                    baseline_runtime_overrides=_json(
                        values.get("baseline_runtime_overrides") or {}
                    ),
                    runtime_environment_fingerprint=str(
                        values.get("runtime_environment_fingerprint") or ""
                    ),
                    runtime_identity_evidence=_json(
                        values.get("runtime_identity_evidence") or {}
                    ),
                    candidate_allocation_bps=int(values["candidate_allocation_bps"]),
                    initial_enrollment_bps=int(
                        values.get("initial_enrollment_bps", values["enrollment_bps"])
                    ),
                    enrollment_bps=int(values["enrollment_bps"]),
                    primary_metric=str(values["primary_metric"]),
                    aggregation_rule=str(
                        values.get("aggregation_rule")
                        or "first_feedback_per_exposed_user_v1"
                    ),
                    baseline_rate=float(values["baseline_rate"]),
                    minimum_detectable_effect=float(values["minimum_detectable_effect"]),
                    alpha=float(values["alpha"]),
                    power=float(values["power"]),
                    required_sample_per_arm=int(values["required_sample_per_arm"]),
                    min_duration_hours=int(values["min_duration_hours"]),
                    max_duration_hours=int(values["max_duration_hours"]),
                    attribution_window_hours=int(values["attribution_window_hours"]),
                    traffic_provenance=str(values["traffic_provenance"]),
                    audience_policy_version=_required(
                        str(values.get("audience_policy_version") or ""),
                        "audience_policy_version",
                        64,
                    ),
                    hmac_key_id=str(values["hmac_key_id"]),
                    preregistration_checksum="",
                    generation=0,
                    created_by=_required(actor, "actor"),
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
                session.flush()
                result = _experiment_dict(record)
                result["created"] = True
                _add_audit(
                    session,
                    tenant_id=tenant_id,
                    experiment_id=record.id,
                    action=action,
                    actor=actor,
                    from_status="",
                    to_status="draft",
                    generation=record.generation,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result=result,
                    now=now,
                )
                return result
        except IntegrityError as exc:
            replay = self.replay_idempotent(
                tenant_id, action, idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            raise ExperimentConflictError("experiment name or request already exists") from exc

    def get_experiment(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            return _experiment_dict(_get_experiment(session, tenant_id, experiment_id))

    def list_experiments(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        stmt = select(OnlineExperimentRecord).where(
            OnlineExperimentRecord.tenant_id == _required(tenant_id, "tenant_id")
        )
        if status:
            stmt = stmt.where(OnlineExperimentRecord.status == status)
        with self._sessions() as session:
            records = session.scalars(
                stmt.order_by(OnlineExperimentRecord.created_at.desc()).limit(_limit(limit))
            ).all()
            return [_experiment_dict(item) for item in records]

    def transition_experiment(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        action: str,
        actor: str,
        expected_generation: int,
        idempotency_key: str,
        request_hash: str,
        allowed_from: Sequence[str],
        to_status: str,
        updates: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
        forbid_creator: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        tenant_id = _required(tenant_id, "tenant_id")
        experiment_id = _required(experiment_id, "experiment_id")
        actor = _required(actor, "actor")
        action = _required(action, "action")
        now = now or _utcnow()
        try:
            with self.transaction() as session:
                record = _get_experiment(
                    session, tenant_id, experiment_id, for_update=True
                )
                _require_experiment_audit_chain(session, record)
                replay = _idempotent_replay(
                    session, tenant_id, action, idempotency_key, request_hash
                )
                if replay is not None:
                    return replay
                if forbid_creator and record.created_by == actor:
                    raise ValueError("experiment creator cannot approve or reject their own experiment")
                if record.generation != int(expected_generation):
                    raise GenerationConflictError(
                        f"experiment generation changed: expected {expected_generation}, current {record.generation}"
                    )
                if record.status not in set(allowed_from):
                    raise ValueError(
                        f"experiment in status {record.status!r} cannot perform {action}"
                    )
                if to_status in ACTIVE_EXPERIMENT_STATUSES:
                    other = session.scalar(
                        select(OnlineExperimentRecord)
                        .where(
                            OnlineExperimentRecord.tenant_id == tenant_id,
                            OnlineExperimentRecord.surface == record.surface,
                            OnlineExperimentRecord.status.in_(ACTIVE_EXPERIMENT_STATUSES),
                            OnlineExperimentRecord.id != record.id,
                        )
                        .with_for_update()
                    )
                    if other is not None:
                        raise ExperimentConflictError(
                            "another experiment is already active for this tenant and surface"
                        )
                from_status = record.status
                record.status = to_status
                for key, value in dict(updates or {}).items():
                    if key in {"id", "tenant_id", "created_by", "candidate_deployment_id"}:
                        raise ValueError(f"immutable experiment field cannot be updated: {key}")
                    if not hasattr(record, key):
                        raise ValueError(f"unknown experiment update field: {key}")
                    setattr(record, key, value)
                record.generation += 1
                record.updated_at = now
                session.flush()
                result = _experiment_dict(record)
                _add_audit(
                    session,
                    tenant_id=tenant_id,
                    experiment_id=record.id,
                    action=action,
                    actor=actor,
                    from_status=from_status,
                    to_status=to_status,
                    generation=record.generation,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    details=details,
                    result=result,
                    now=now,
                )
                return result
        except IntegrityError as exc:
            replay = self.replay_idempotent(
                tenant_id, action, idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            raise ExperimentConflictError("experiment transition conflicted") from exc

    # -- request assignment/exposure --------------------------------------

    def active_experiment(self, tenant_id: str, surface: str) -> dict[str, Any] | None:
        with self._sessions() as session:
            records = session.scalars(
                select(OnlineExperimentRecord).where(
                    OnlineExperimentRecord.tenant_id == _required(tenant_id, "tenant_id"),
                    OnlineExperimentRecord.surface == surface,
                    OnlineExperimentRecord.status.in_(ACTIVE_EXPERIMENT_STATUSES),
                )
            ).all()
            if len(records) > 1:
                raise ExperimentStoreError("multiple active experiments violate the v1 surface invariant")
            return _experiment_dict(records[0]) if records else None

    def pause_if_max_duration_elapsed(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically stop an active experiment whose collection window ended."""

        now = _aware(now or _utcnow())
        with self.transaction() as session:
            record = _get_experiment(session, tenant_id, experiment_id, for_update=True)
            _require_experiment_audit_chain(session, record)
            if record.status not in ACTIVE_EXPERIMENT_STATUSES:
                return {"experiment": _experiment_dict(record), "paused": False}
            if not _max_duration_elapsed(record, now):
                return {"experiment": _experiment_dict(record), "paused": False}
            previous = record.status
            record.status = "paused"
            record.paused_from = previous
            record.paused_by = "system:max_duration"
            record.pause_reason = "pre-registered maximum collection duration elapsed"
            record.paused_at = now
            record.generation += 1
            record.updated_at = now
            result = _experiment_dict(record)
            request_hash = canonical_checksum({
                "experiment_id": record.id,
                "started_at": record.started_at,
                "max_duration_hours": record.max_duration_hours,
            })
            _add_audit(
                session,
                tenant_id=record.tenant_id,
                experiment_id=record.id,
                action="auto_max_duration_pause",
                actor="system:max_duration",
                from_status=previous,
                to_status="paused",
                generation=record.generation,
                idempotency_key=f"max-duration:{record.id}",
                request_hash=request_hash,
                details={"reason": record.pause_reason},
                result=result,
                now=now,
            )
            session.flush()
            return {"experiment": result, "paused": True}

    def pause_if_sample_target_reached(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically stop collection once both pre-registered arm targets exist.

        Assignment alone is not evidence.  Counts therefore use distinct
        assignments that have at least one exposure, including started and
        failed exposures, which is the experiment's pre-registered ITT unit.
        """

        now = _aware(now or _utcnow())
        with self.transaction() as session:
            record = _get_experiment(session, tenant_id, experiment_id, for_update=True)
            _require_experiment_audit_chain(session, record)
            counts = _exposed_user_counts(session, record.tenant_id, record.id)
            reached = _sample_target_reached(record, counts)
            minimum_duration_reached = _minimum_duration_elapsed(record, now)
            stopping_ready = reached and minimum_duration_reached
            paused = False
            if record.status in ACTIVE_EXPERIMENT_STATUSES and stopping_ready:
                _pause_for_sample_target(session, record, counts=counts, now=now)
                paused = True
            session.flush()
            return {
                "experiment": _experiment_dict(record),
                "paused": paused,
                "target_reached": reached,
                "minimum_duration_reached": minimum_duration_reached,
                "stopping_ready": stopping_ready,
                "counts": counts,
            }

    def record_integrity_failure(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fail closed and durably explain a frozen-evidence integrity drift."""

        now = _aware(now or _utcnow())
        safe_reason = _required(reason, "reason", 200)
        reason_hash = hashlib.sha256(safe_reason.encode("utf-8")).hexdigest()
        action = "auto_integrity_pause"
        with self.transaction() as session:
            record = _get_experiment(
                session, tenant_id, experiment_id, for_update=True
            )
            if (
                record.status == "safety_paused"
                and record.paused_by == "system:integrity_monitor"
            ):
                return {
                    "experiment": _experiment_dict(record),
                    "integrity_blocked": True,
                    "paused": True,
                    "reason": record.pause_reason,
                }
            # A pre-start integrity audit must not suppress a later active
            # safety pause for the same reason.  Generation is part of the
            # idempotency scope because it denotes a distinct state snapshot.
            key = f"integrity:{experiment_id}:{record.generation}:{reason_hash[:32]}"
            request_hash = canonical_checksum(
                {
                    "experiment_id": experiment_id,
                    "generation": record.generation,
                    "status": record.status,
                    "reason": safe_reason,
                }
            )
            replay = _idempotent_replay(
                session, tenant_id, action, key, request_hash
            )
            if replay is not None:
                return replay
            previous = record.status
            paused = record.status in ACTIVE_EXPERIMENT_STATUSES
            if paused:
                record.status = "safety_paused"
                record.paused_from = previous
                record.paused_by = "system:integrity_monitor"
                record.pause_reason = safe_reason
                record.paused_at = now
                record.generation += 1
                record.updated_at = now
            result = {
                "experiment": _experiment_dict(record),
                "integrity_blocked": True,
                "paused": paused,
                "reason": safe_reason,
            }
            _add_audit(
                session,
                tenant_id=record.tenant_id,
                experiment_id=record.id,
                action=action,
                actor="system:integrity_monitor",
                from_status=previous,
                to_status=record.status,
                generation=record.generation,
                idempotency_key=key,
                request_hash=request_hash,
                details={"reason": safe_reason},
                result=result,
                now=now,
            )
            session.flush()
            return result

    def get_or_create_assignment(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        subject_digest: str,
        enrollment_bucket: int,
        variant_bucket: int,
        arm: str,
        allocation_bps: int,
        experiment_generation: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        try:
            with self.transaction() as session:
                experiment = _get_experiment(
                    session, tenant_id, experiment_id, for_update=True
                )
                _require_experiment_audit_chain(session, experiment)
                if experiment.status not in ACTIVE_EXPERIMENT_STATUSES:
                    raise ExperimentConflictError("experiment is no longer accepting assignments")
                existing = session.scalar(
                    select(ExperimentAssignmentRecord).where(
                        ExperimentAssignmentRecord.tenant_id == tenant_id,
                        ExperimentAssignmentRecord.experiment_id == experiment_id,
                        ExperimentAssignmentRecord.subject_digest == subject_digest,
                    )
                )
                if existing is not None:
                    return _assignment_dict(existing)
                record = ExperimentAssignmentRecord(
                    id=_id(),
                    tenant_id=tenant_id,
                    experiment_id=experiment_id,
                    subject_digest=_required(subject_digest, "subject_digest"),
                    enrollment_bucket=int(enrollment_bucket),
                    variant_bucket=int(variant_bucket),
                    arm=arm,
                    allocation_bps=int(allocation_bps),
                    experiment_generation=int(experiment_generation),
                    assigned_at=now,
                )
                session.add(record)
                session.flush()
                return _assignment_dict(record)
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(ExperimentAssignmentRecord).where(
                        ExperimentAssignmentRecord.tenant_id == tenant_id,
                        ExperimentAssignmentRecord.experiment_id == experiment_id,
                        ExperimentAssignmentRecord.subject_digest == subject_digest,
                    )
                )
                if existing is not None:
                    return _assignment_dict(existing)
            raise

    def create_exposure(
        self,
        allocation: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        tenant_id = _required(str(allocation["tenant_id"]), "tenant_id")
        turn_id = _required(str(allocation["turn_id"]), "turn_id", 100)
        trace_id = _required(str(allocation["trace_id"]), "trace_id", 100)
        request_fingerprint = _sha256_hex(
            allocation.get("request_fingerprint"), "request_fingerprint"
        )
        audience_policy_version = _required(
            str(allocation.get("audience_policy_version") or ""),
            "audience_policy_version",
            64,
        )
        audience_provenance = _required(
            str(allocation.get("audience_provenance") or ""),
            "audience_provenance",
            40,
        )
        audience_eligible = allocation.get("audience_eligible")
        if not isinstance(audience_eligible, bool):
            raise ValueError("audience_eligible must be boolean")
        raw_account_created_at = allocation.get("audience_account_created_at")
        audience_account_created_at = (
            _aware(raw_account_created_at)
            if isinstance(raw_account_created_at, datetime)
            else None
        )
        audience_attestation = _sha256_hex(
            allocation.get("audience_attestation"), "audience_attestation"
        )
        now = now or _utcnow()
        try:
            with self.transaction() as session:
                assignment = session.scalar(
                    select(ExperimentAssignmentRecord).where(
                        ExperimentAssignmentRecord.id == allocation["assignment_id"],
                        ExperimentAssignmentRecord.tenant_id == tenant_id,
                    )
                )
                if assignment is None:
                    raise LookupError("experiment assignment not found")
                if assignment.experiment_id != str(allocation["experiment_id"]):
                    raise ExperimentConflictError(
                        "assignment does not belong to the requested experiment"
                    )
                existing = session.scalar(
                    select(ExperimentExposureRecord).where(
                        ExperimentExposureRecord.tenant_id == tenant_id,
                        ExperimentExposureRecord.turn_id == turn_id,
                    )
                )
                if existing is not None:
                    if (
                        existing.trace_id != trace_id
                        or existing.request_fingerprint != request_fingerprint
                        or existing.assignment_id != assignment.id
                        or existing.experiment_id != assignment.experiment_id
                        or existing.audience_policy_version != audience_policy_version
                        or existing.audience_provenance != audience_provenance
                        or bool(existing.audience_eligible) != audience_eligible
                        or not hmac.compare_digest(
                            str(existing.audience_attestation or ""),
                            audience_attestation,
                        )
                    ):
                        raise IdempotencyConflictError(
                            "turn_id was already used by a different request or subject"
                        )
                    result = _exposure_dict(existing)
                    result["idempotent"] = True
                    return result
                experiment = _get_experiment(
                    session, tenant_id, str(allocation["experiment_id"]), for_update=True
                )
                _require_experiment_audit_chain(session, experiment)
                if experiment.status not in ACTIVE_EXPERIMENT_STATUSES:
                    raise ExperimentConflictError("experiment paused before exposure began")
                if audience_policy_version != experiment.audience_policy_version:
                    raise ExperimentConflictError(
                        "audience policy does not match experiment preregistration"
                    )
                if (
                    experiment.traffic_provenance == "production_authenticated"
                    and (not audience_eligible or audience_account_created_at is None)
                ):
                    raise ExperimentConflictError(
                        "production exposure requires an eligible audience identity"
                    )
                if _max_duration_elapsed(experiment, _aware(now)):
                    raise ExperimentConflictError(
                        "experiment maximum collection duration elapsed"
                    )
                before_counts = _exposed_user_counts(
                    session, tenant_id, experiment.id
                )
                if _sample_target_reached(experiment, before_counts):
                    # The fixed N was already reached by another transaction.
                    # Do not admit an overshoot even if the minimum observation
                    # duration has not elapsed yet.
                    raise ExperimentConflictError(
                        "experiment reached its pre-registered sample target"
                    )
                if assignment.arm == "candidate":
                    deployment_id = str(allocation.get("deployment_id") or "")
                    if deployment_id != experiment.candidate_deployment_id:
                        raise ExperimentConflictError(
                            "candidate assignment deployment does not match experiment"
                        )
                    deployment = session.scalar(
                        select(OnlineStrategyDeploymentRecord).where(
                            OnlineStrategyDeploymentRecord.id == deployment_id,
                            OnlineStrategyDeploymentRecord.tenant_id == tenant_id,
                        )
                    )
                    if deployment is None:
                        raise LookupError("online strategy deployment not found")
                    actual_checksum = canonical_checksum(deployment.compiled_overrides)
                    declared_checksum = str(deployment.compiled_checksum or "")
                    actual_record_checksum = canonical_checksum(
                        {
                            "tenant_id": deployment.tenant_id,
                            "source_proposal_id": deployment.source_proposal_id,
                            "source_strategy_version_id": (
                                deployment.source_strategy_version_id
                            ),
                            "source_manifest_checksum": (
                                deployment.source_manifest_checksum
                            ),
                            "compiled_overrides": deployment.compiled_overrides,
                            "compiler_version": deployment.compiler_version,
                            "compiled_checksum": declared_checksum,
                        }
                    )
                    allocation_checksum = str(
                        allocation.get("deployment_compiled_checksum") or ""
                    )
                    if not (
                        re.fullmatch(r"[0-9a-f]{64}", declared_checksum)
                        and hmac.compare_digest(actual_checksum, declared_checksum)
                        and hmac.compare_digest(
                            actual_record_checksum,
                            str(deployment.record_checksum or ""),
                        )
                        and hmac.compare_digest(allocation_checksum, actual_checksum)
                        and str(allocation.get("runtime_compiler_version") or "")
                        == str(deployment.compiler_version)
                    ):
                        raise ExperimentConflictError(
                            "candidate deployment integrity check failed"
                        )
                elif allocation.get("deployment_id") or allocation.get(
                    "deployment_compiled_checksum"
                ):
                    raise ExperimentConflictError(
                        "control assignment cannot carry candidate deployment evidence"
                    )
                baseline = _json(experiment.baseline_runtime_overrides or {})
                effective_rag = dict((baseline.get("rag") or {}))
                if assignment.arm == "candidate":
                    effective_rag.update(
                        dict((deployment.compiled_overrides or {}).get("rag") or {})
                    )
                effective_runtime = {"rag": effective_rag} if effective_rag else {}
                expected_runtime_checksum = canonical_checksum(
                    {
                        "runtime_environment_fingerprint": (
                            experiment.runtime_environment_fingerprint
                        ),
                        "runtime_overrides": effective_runtime,
                    }
                )
                supplied_runtime_checksum = _sha256_hex(
                    allocation.get("runtime_strategy_checksum"),
                    "runtime_strategy_checksum",
                )
                if not hmac.compare_digest(
                    expected_runtime_checksum, supplied_runtime_checksum
                ):
                    raise ExperimentConflictError(
                        "effective runtime configuration integrity check failed"
                    )
                record = ExperimentExposureRecord(
                    id=_id(),
                    tenant_id=tenant_id,
                    experiment_id=experiment.id,
                    assignment_id=assignment.id,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    request_fingerprint=request_fingerprint,
                    arm=assignment.arm,
                    deployment_id=(
                        experiment.candidate_deployment_id
                        if assignment.arm == "candidate" else None
                    ),
                    runtime_strategy_checksum=supplied_runtime_checksum,
                    traffic_provenance=experiment.traffic_provenance,
                    audience_policy_version=audience_policy_version,
                    audience_provenance=audience_provenance,
                    audience_eligible=audience_eligible,
                    audience_account_created_at=audience_account_created_at,
                    audience_attestation=audience_attestation,
                    status="started",
                    metrics={},
                    safety_events=[],
                    started_at=now,
                )
                session.add(record)
                session.flush()
                # The experiment row is still locked here.  Pausing in this
                # transaction closes the race where two workers both observe
                # N-1 users and accept traffic beyond the fixed stopping rule.
                counts = _exposed_user_counts(session, tenant_id, experiment.id)
                if (
                    _sample_target_reached(experiment, counts)
                    and _minimum_duration_elapsed(experiment, _aware(now))
                ):
                    _pause_for_sample_target(
                        session, experiment, counts=counts, now=_aware(now)
                    )
                result = _exposure_dict(record)
                result["idempotent"] = False
                result["collection_stopped"] = (
                    experiment.status == "paused"
                    and experiment.paused_by == "system:sample_target"
                )
                return result
        except IntegrityError:
            with self._sessions() as session:
                assignment = session.scalar(
                    select(ExperimentAssignmentRecord).where(
                        ExperimentAssignmentRecord.id == allocation["assignment_id"],
                        ExperimentAssignmentRecord.tenant_id == tenant_id,
                    )
                )
                existing = session.scalar(
                    select(ExperimentExposureRecord).where(
                        ExperimentExposureRecord.tenant_id == tenant_id,
                        ExperimentExposureRecord.turn_id == turn_id,
                    )
                )
                if (
                    assignment is not None
                    and existing is not None
                    and existing.trace_id == trace_id
                    and existing.request_fingerprint == request_fingerprint
                    and existing.assignment_id == assignment.id
                    and existing.experiment_id == assignment.experiment_id
                ):
                    result = _exposure_dict(existing)
                    result["idempotent"] = True
                    return result
            raise

    def finish_exposure(
        self,
        tenant_id: str,
        exposure_id: str,
        *,
        status: str,
        latency_ms: float,
        metrics: Mapping[str, Any],
        safety_events: Sequence[Mapping[str, Any]],
        trusted_severe: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        with self.transaction() as session:
            record = session.scalar(
                select(ExperimentExposureRecord)
                .where(
                    ExperimentExposureRecord.id == _required(exposure_id, "exposure_id"),
                    ExperimentExposureRecord.tenant_id == _required(tenant_id, "tenant_id"),
                )
                .with_for_update()
            )
            if record is None:
                raise LookupError(f"experiment exposure not found: {exposure_id}")
            did_finish = False
            if record.status == "started":
                record.status = status
                record.latency_ms = float(latency_ms)
                record.metrics = _json(metrics)
                record.safety_events = _json(list(safety_events))
                record.completed_at = now
                did_finish = True
            experiment = _get_experiment(
                session, tenant_id, record.experiment_id, for_update=True
            )
            _require_experiment_audit_chain(session, experiment)
            safety_paused = False
            if (
                did_finish
                and trusted_severe
                and experiment.status in ACTIVE_EXPERIMENT_STATUSES
            ):
                previous = experiment.status
                experiment.status = "safety_paused"
                experiment.paused_from = previous
                experiment.paused_by = "system:safety_monitor"
                experiment.pause_reason = f"trusted S0/S1 event in exposure {record.id}"
                experiment.paused_at = now
                experiment.generation += 1
                experiment.updated_at = now
                safety_paused = True
                event_hash = canonical_checksum(list(safety_events))
                _add_audit(
                    session,
                    tenant_id=tenant_id,
                    experiment_id=experiment.id,
                    action="auto_safety_pause",
                    actor="system:safety_monitor",
                    from_status=previous,
                    to_status="safety_paused",
                    generation=experiment.generation,
                    idempotency_key=f"safety:{record.id}:{event_hash}",
                    request_hash=event_hash,
                    details={"exposure_id": record.id, "events": list(safety_events)},
                    result=_experiment_dict(experiment),
                    now=now,
                )
            session.flush()
            return {
                "exposure": _exposure_dict(record),
                "experiment": _experiment_dict(experiment),
                "safety_paused": safety_paused or experiment.status == "safety_paused",
            }

    def emergency_safety_pause(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        exposure_id: str,
        safety_events: Sequence[Mapping[str, Any]],
        failure_code: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a trusted severe signal in an independent transaction.

        This path is deliberately separate from ``finish_exposure``.  If the
        terminal exposure write rolls back, a trusted S0/S1 signal must still
        stop new candidate traffic and leave an auditable diagnostic record.
        It does not mark the exposure terminal; a stale ``started`` exposure
        remains visible to analysis as an additional evidence blocker.
        """

        tenant_id = _required(tenant_id, "tenant_id")
        experiment_id = _required(experiment_id, "experiment_id")
        exposure_id = _required(exposure_id, "exposure_id")
        failure_code = _required(failure_code, "failure_code", 100)
        events = _json(list(safety_events))
        event_checksum = canonical_checksum(events)
        request_hash = canonical_checksum(
            {
                "experiment_id": experiment_id,
                "exposure_id": exposure_id,
                "event_checksum": event_checksum,
                "failure_code": failure_code,
            }
        )
        idempotency_key = f"emergency-safety:{exposure_id}:{event_checksum[:32]}"
        now = _aware(now or _utcnow())
        with self.transaction() as session:
            replay = _idempotent_replay(
                session,
                tenant_id,
                "emergency_safety_pause",
                idempotency_key,
                request_hash,
            )
            if replay is not None:
                return replay
            exposure = session.scalar(
                select(ExperimentExposureRecord).where(
                    ExperimentExposureRecord.tenant_id == tenant_id,
                    ExperimentExposureRecord.id == exposure_id,
                    ExperimentExposureRecord.experiment_id == experiment_id,
                )
            )
            if exposure is None:
                raise LookupError(f"experiment exposure not found: {exposure_id}")
            experiment = _get_experiment(
                session, tenant_id, experiment_id, for_update=True
            )
            previous = experiment.status
            paused = experiment.status in ACTIVE_EXPERIMENT_STATUSES
            if paused:
                experiment.status = "safety_paused"
                experiment.paused_from = previous
                experiment.paused_by = "system:safety_monitor"
                experiment.pause_reason = (
                    f"trusted S0/S1 event could not finish exposure {exposure_id}"
                )
                experiment.paused_at = now
                experiment.generation += 1
                experiment.updated_at = now
            outbox = ExperimentSafetyOutboxRecord(
                id=_id(),
                tenant_id=tenant_id,
                experiment_id=experiment_id,
                exposure_id=exposure_id,
                signal_checksum=event_checksum,
                failure_code=failure_code,
                safety_events=events,
                status="pending",
                created_at=now,
            )
            session.add(outbox)
            session.flush()
            result = {
                "experiment": _experiment_dict(experiment),
                "exposure_id": exposure_id,
                "emergency_paused": paused,
                "failure_code": failure_code,
                "outbox_id": outbox.id,
                "outbox_status": outbox.status,
            }
            _add_audit(
                session,
                tenant_id=tenant_id,
                experiment_id=experiment_id,
                action="emergency_safety_pause",
                actor="system:safety_monitor",
                from_status=previous,
                to_status=experiment.status,
                generation=experiment.generation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                details={
                    "exposure_id": exposure_id,
                    "event_checksum": event_checksum,
                    "events": events,
                    "finish_failure_code": failure_code,
                },
                result=result,
                now=now,
            )
            session.flush()
            return result

    def get_exposure(self, tenant_id: str, exposure_id: str) -> dict[str, Any]:
        with self._sessions() as session:
            record = session.scalar(
                select(ExperimentExposureRecord).where(
                    ExperimentExposureRecord.tenant_id == _required(tenant_id, "tenant_id"),
                    ExperimentExposureRecord.id == _required(exposure_id, "exposure_id"),
                )
            )
            if record is None:
                raise LookupError(f"experiment exposure not found: {exposure_id}")
            return _exposure_dict(record)

    def get_exposure_by_id(self, exposure_id: str) -> dict[str, Any]:
        """Internal runtime lookup for an opaque UUID generated by this store."""

        with self._sessions() as session:
            record = session.get(
                ExperimentExposureRecord, _required(exposure_id, "exposure_id")
            )
            if record is None:
                raise LookupError(f"experiment exposure not found: {exposure_id}")
            return _exposure_dict(record)

    def record_outcome(
        self,
        tenant_id: str,
        *,
        subject_digest: str,
        exposure_id: str,
        event_id: str,
        metric: str,
        value: float,
        included: bool,
        exclusion_reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        try:
            with self.transaction() as session:
                # Authorize ownership before returning an idempotent replay.
                # Otherwise a tenant peer who guessed an event/exposure pair
                # could learn another participant's feedback record.
                exposure = session.scalar(
                    select(ExperimentExposureRecord).where(
                        ExperimentExposureRecord.tenant_id == _required(tenant_id, "tenant_id"),
                        ExperimentExposureRecord.id == _required(exposure_id, "exposure_id"),
                    )
                )
                if exposure is None:
                    raise LookupError(f"experiment exposure not found: {exposure_id}")
                assignment = session.get(ExperimentAssignmentRecord, exposure.assignment_id)
                if assignment is None or assignment.subject_digest != subject_digest:
                    raise LookupError(f"experiment exposure not found: {exposure_id}")
                experiment = _get_experiment(
                    session, tenant_id, exposure.experiment_id, for_update=True
                )
                _require_experiment_audit_chain(session, experiment)
                existing = session.scalar(
                    select(ExperimentOutcomeRecord).where(
                        ExperimentOutcomeRecord.tenant_id == tenant_id,
                        ExperimentOutcomeRecord.event_id == event_id,
                    )
                )
                if existing is not None:
                    if (
                        existing.exposure_id != exposure_id
                        or existing.metric != metric
                        or float(existing.value) != float(value)
                    ):
                        raise IdempotencyConflictError(
                            "event_id was already used for a different feedback payload"
                        )
                    result = _outcome_dict(existing)
                    result["idempotent"] = True
                    return result
                record = ExperimentOutcomeRecord(
                    id=_id(),
                    tenant_id=tenant_id,
                    experiment_id=exposure.experiment_id,
                    exposure_id=exposure.id,
                    event_id=_required(event_id, "event_id", 100),
                    metric=_required(metric, "metric", 100),
                    value=float(value),
                    source="authenticated_feedback",
                    included=bool(included),
                    exclusion_reason=str(exclusion_reason or "")[:200],
                    occurred_at=now,
                    received_at=now,
                )
                session.add(record)
                session.flush()
                result = _outcome_dict(record)
                result["idempotent"] = False
                return result
        except IntegrityError:
            with self._sessions() as session:
                exposure = session.scalar(
                    select(ExperimentExposureRecord).where(
                        ExperimentExposureRecord.tenant_id == _required(tenant_id, "tenant_id"),
                        ExperimentExposureRecord.id == _required(exposure_id, "exposure_id"),
                    )
                )
                assignment = (
                    session.get(ExperimentAssignmentRecord, exposure.assignment_id)
                    if exposure is not None else None
                )
                if assignment is None or assignment.subject_digest != subject_digest:
                    raise LookupError(f"experiment exposure not found: {exposure_id}")
                existing = session.scalar(
                    select(ExperimentOutcomeRecord).where(
                        ExperimentOutcomeRecord.tenant_id == tenant_id,
                        ExperimentOutcomeRecord.event_id == event_id,
                    )
                )
                if existing is not None:
                    if existing.exposure_id != exposure_id or float(existing.value) != float(value):
                        raise IdempotencyConflictError(
                            "event_id was already used for a different feedback payload"
                        )
                    result = _outcome_dict(existing)
                    result["idempotent"] = True
                    return result
            raise

    # -- evidence/monitoring ----------------------------------------------

    def evidence(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        tenant_id = _required(tenant_id, "tenant_id")
        with self._sessions() as session:
            experiment = _get_experiment(session, tenant_id, experiment_id)
            assignments = session.scalars(
                select(ExperimentAssignmentRecord).where(
                    ExperimentAssignmentRecord.tenant_id == tenant_id,
                    ExperimentAssignmentRecord.experiment_id == experiment.id,
                )
            ).all()
            exposures = session.scalars(
                select(ExperimentExposureRecord)
                .where(
                    ExperimentExposureRecord.tenant_id == tenant_id,
                    ExperimentExposureRecord.experiment_id == experiment.id,
                )
                .order_by(ExperimentExposureRecord.started_at, ExperimentExposureRecord.id)
            ).all()
            outcomes = session.scalars(
                select(ExperimentOutcomeRecord).where(
                    ExperimentOutcomeRecord.tenant_id == tenant_id,
                    ExperimentOutcomeRecord.experiment_id == experiment.id,
                )
            ).all()
            return {
                "experiment": _experiment_dict(experiment),
                "assignments": [_assignment_dict(item) for item in assignments],
                "exposures": [_exposure_dict(item) for item in exposures],
                "outcomes": [_outcome_dict(item) for item in outcomes],
            }

    def save_monitor_snapshot(
        self,
        tenant_id: str,
        experiment_id: str,
        snapshot: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        value = _json(snapshot)
        with self.transaction() as session:
            _get_experiment(session, tenant_id, experiment_id)
            checksum = canonical_checksum(value)
            previous = session.scalar(
                select(ExperimentMonitorSnapshotRecord)
                .where(
                    ExperimentMonitorSnapshotRecord.tenant_id == tenant_id,
                    ExperimentMonitorSnapshotRecord.experiment_id == experiment_id,
                )
                .order_by(
                    ExperimentMonitorSnapshotRecord.created_at.desc(),
                    ExperimentMonitorSnapshotRecord.id.desc(),
                )
                .limit(1)
            )
            if previous is not None and hmac.compare_digest(
                previous.snapshot_checksum, checksum
            ):
                return {
                    "id": previous.id,
                    "tenant_id": previous.tenant_id,
                    "experiment_id": previous.experiment_id,
                    "snapshot": _json(previous.snapshot),
                    "snapshot_checksum": previous.snapshot_checksum,
                    "created_at": previous.created_at,
                    "deduplicated": True,
                }
            record = ExperimentMonitorSnapshotRecord(
                id=_id(),
                tenant_id=tenant_id,
                experiment_id=experiment_id,
                snapshot=value,
                snapshot_checksum=checksum,
                created_at=now or _utcnow(),
            )
            session.add(record)
            session.flush()
            return {
                "id": record.id,
                "tenant_id": record.tenant_id,
                "experiment_id": record.experiment_id,
                "snapshot": record.snapshot,
                "snapshot_checksum": record.snapshot_checksum,
                "created_at": record.created_at,
                "deduplicated": False,
            }

    def verify_experiment_audit_chain(
        self, tenant_id: str, experiment_id: str
    ) -> dict[str, Any]:
        """Replay one tenant-scoped lifecycle chain against current state."""

        with self._sessions() as session:
            experiment = _get_experiment(session, tenant_id, experiment_id)
            return _verify_experiment_audit_chain(session, experiment)

    def list_audit_events(
        self,
        tenant_id: str,
        *,
        experiment_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        stmt = select(ExperimentAuditEventRecord).where(
            ExperimentAuditEventRecord.tenant_id == _required(tenant_id, "tenant_id")
        )
        if experiment_id:
            stmt = stmt.where(ExperimentAuditEventRecord.experiment_id == experiment_id)
        with self._sessions() as session:
            records = session.scalars(
                stmt.order_by(ExperimentAuditEventRecord.created_at.desc()).limit(_limit(limit, 5000))
            ).all()
            return [_audit_dict(item) for item in records]

    def replay_idempotent(
        self,
        tenant_id: str,
        action: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        with self._sessions() as session:
            return _idempotent_replay(
                session, tenant_id, action, idempotency_key, request_hash
            )


def canonical_checksum(value: Any) -> str:
    payload = json.dumps(
        _json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _idempotent_replay(
    session: Session,
    tenant_id: str,
    action: str,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    key = _required(idempotency_key, "idempotency_key", 200)
    existing = session.scalar(
        select(ExperimentAuditEventRecord).where(
            ExperimentAuditEventRecord.tenant_id == tenant_id,
            ExperimentAuditEventRecord.action == action,
            ExperimentAuditEventRecord.idempotency_key == key,
        )
    )
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise IdempotencyConflictError(
            "idempotency_key was already used with a different request payload"
        )
    # Return the exact original semantic result.  Replay metadata belongs in
    # the audit event; changing the response breaks HTTP idempotency.
    return _json(existing.result)


def _add_audit(
    session: Session,
    *,
    tenant_id: str,
    action: str,
    actor: str,
    idempotency_key: str,
    request_hash: str,
    experiment_id: str | None = None,
    deployment_id: str | None = None,
    from_status: str = "",
    to_status: str = "",
    generation: int = 0,
    details: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ExperimentAuditEventRecord:
    normalized_tenant = _required(tenant_id, "tenant_id")
    created_at = _aware(now or _utcnow())
    previous_event_hash = ""
    chain_position = 0
    if experiment_id is not None:
        previous = session.scalar(
            select(ExperimentAuditEventRecord)
            .where(
                ExperimentAuditEventRecord.tenant_id == normalized_tenant,
                ExperimentAuditEventRecord.experiment_id == experiment_id,
            )
            .order_by(ExperimentAuditEventRecord.chain_position.desc())
            .limit(1)
            .with_for_update()
        )
        if previous is not None:
            chain_position = int(previous.chain_position) + 1
            previous_event_hash = str(previous.event_hash or "")
        else:
            chain_position = 1
    record = ExperimentAuditEventRecord(
        id=_id(),
        tenant_id=normalized_tenant,
        experiment_id=experiment_id,
        deployment_id=deployment_id,
        action=_required(action, "action", 64),
        actor=_required(actor, "actor", 200),
        from_status=str(from_status or "")[:32],
        to_status=str(to_status or "")[:32],
        generation=int(generation),
        request_hash=_required(request_hash, "request_hash", 64),
        idempotency_key=_required(idempotency_key, "idempotency_key", 200),
        details=_json(details or {}),
        result=_json(result or {}),
        chain_position=chain_position,
        previous_event_hash=previous_event_hash,
        event_hash="",
        created_at=created_at,
    )
    record.event_hash = canonical_checksum(_audit_hash_payload(record))
    session.add(record)
    session.flush()
    return record


def _audit_hash_payload(record: ExperimentAuditEventRecord) -> dict[str, Any]:
    """Canonical immutable fields covered by one audit event hash."""

    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "experiment_id": record.experiment_id,
        "deployment_id": record.deployment_id,
        "action": record.action,
        "actor": record.actor,
        "from_status": record.from_status,
        "to_status": record.to_status,
        "generation": int(record.generation),
        "request_hash": record.request_hash,
        "idempotency_key": record.idempotency_key,
        "details": _json(record.details),
        "result": _json(record.result),
        "chain_position": int(record.chain_position),
        "previous_event_hash": record.previous_event_hash,
        "created_at": _aware(record.created_at).isoformat(),
    }


def _audit_result_state(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    nested = result.get("experiment")
    if isinstance(nested, Mapping):
        return nested
    if "status" in result and "generation" in result:
        return result
    return None


def _verify_experiment_audit_chain(
    session: Session,
    experiment: OnlineExperimentRecord,
) -> dict[str, Any]:
    events = session.scalars(
        select(ExperimentAuditEventRecord)
        .where(
            ExperimentAuditEventRecord.tenant_id == experiment.tenant_id,
            ExperimentAuditEventRecord.experiment_id == experiment.id,
        )
        .order_by(ExperimentAuditEventRecord.chain_position.asc())
    ).all()
    if not events:
        return {
            "valid": False,
            "reason": "audit_chain_missing",
            "event_count": 0,
            "head_hash": "",
        }

    previous_hash = ""
    previous_status = ""
    previous_generation: int | None = None
    for expected_position, event in enumerate(events, start=1):
        if int(event.chain_position) != expected_position:
            return _invalid_audit_chain(events, "audit_chain_position_gap")
        if not hmac.compare_digest(
            str(event.previous_event_hash or ""), previous_hash
        ):
            return _invalid_audit_chain(events, "audit_chain_predecessor_mismatch")
        declared_hash = str(event.event_hash or "")
        expected_hash = canonical_checksum(_audit_hash_payload(event))
        if not (
            re.fullmatch(r"[0-9a-f]{64}", declared_hash)
            and hmac.compare_digest(declared_hash, expected_hash)
        ):
            return _invalid_audit_chain(events, "audit_chain_event_hash_mismatch")

        state = _audit_result_state(event.result or {})
        if state is None:
            return _invalid_audit_chain(events, "audit_chain_state_snapshot_missing")
        status = str(state.get("status") or "")
        try:
            generation = int(state.get("generation"))
        except (TypeError, ValueError):
            return _invalid_audit_chain(events, "audit_chain_generation_invalid")
        if event.to_status != status or int(event.generation) != generation:
            return _invalid_audit_chain(events, "audit_chain_event_state_mismatch")
        if expected_position == 1:
            if event.from_status or status != "draft" or generation != 0:
                return _invalid_audit_chain(events, "audit_chain_genesis_invalid")
        else:
            if event.from_status != previous_status:
                return _invalid_audit_chain(events, "audit_chain_status_discontinuity")
            if previous_generation is None or generation not in {
                previous_generation,
                previous_generation + 1,
            }:
                return _invalid_audit_chain(events, "audit_chain_generation_discontinuity")
            if status != previous_status and generation != previous_generation + 1:
                return _invalid_audit_chain(events, "audit_chain_transition_generation_invalid")
        previous_hash = declared_hash
        previous_status = status
        previous_generation = generation

    if (
        previous_status != experiment.status
        or previous_generation != int(experiment.generation)
    ):
        return _invalid_audit_chain(events, "audit_chain_head_state_mismatch")
    return {
        "valid": True,
        "reason": None,
        "event_count": len(events),
        "head_hash": previous_hash,
    }


def _invalid_audit_chain(
    events: Sequence[ExperimentAuditEventRecord], reason: str
) -> dict[str, Any]:
    return {
        "valid": False,
        "reason": reason,
        "event_count": len(events),
        "head_hash": str(events[-1].event_hash or "") if events else "",
    }


def _require_experiment_audit_chain(
    session: Session, experiment: OnlineExperimentRecord
) -> None:
    result = _verify_experiment_audit_chain(session, experiment)
    if result["valid"] is not True:
        raise ExperimentConflictError(
            f"experiment audit chain integrity check failed: {result['reason']}"
        )


def _get_experiment(
    session: Session,
    tenant_id: str,
    experiment_id: str,
    *,
    for_update: bool = False,
) -> OnlineExperimentRecord:
    stmt = select(OnlineExperimentRecord).where(
        OnlineExperimentRecord.tenant_id == _required(tenant_id, "tenant_id"),
        OnlineExperimentRecord.id == _required(experiment_id, "experiment_id"),
    )
    if for_update:
        stmt = stmt.with_for_update()
    record = session.scalar(stmt)
    if record is None:
        raise LookupError(f"online experiment not found: {experiment_id}")
    return record


def _exposed_user_counts(
    session: Session,
    tenant_id: str,
    experiment_id: str,
) -> dict[str, int]:
    """Count unique exposed randomisation units without loading raw evidence."""

    stmt = (
        select(
            ExperimentAssignmentRecord.arm,
            func.count(func.distinct(ExperimentExposureRecord.assignment_id)),
        )
        .select_from(ExperimentExposureRecord)
        .join(
            ExperimentAssignmentRecord,
            ExperimentAssignmentRecord.id == ExperimentExposureRecord.assignment_id,
        )
        .where(
            ExperimentExposureRecord.tenant_id == tenant_id,
            ExperimentExposureRecord.experiment_id == experiment_id,
            ExperimentAssignmentRecord.tenant_id == tenant_id,
            ExperimentAssignmentRecord.experiment_id == experiment_id,
        )
    )
    experiment = session.scalar(
        select(OnlineExperimentRecord).where(
            OnlineExperimentRecord.tenant_id == tenant_id,
            OnlineExperimentRecord.id == experiment_id,
        )
    )
    if (
        experiment is not None
        and experiment.traffic_provenance == "production_authenticated"
    ):
        stmt = stmt.where(ExperimentExposureRecord.audience_eligible.is_(True))
    rows = session.execute(stmt.group_by(ExperimentAssignmentRecord.arm)).all()
    counts = {"control": 0, "candidate": 0}
    for arm, count in rows:
        if arm in counts:
            counts[str(arm)] = int(count or 0)
    return counts


def _sample_target_reached(
    experiment: OnlineExperimentRecord,
    counts: Mapping[str, int],
) -> bool:
    target = int(experiment.required_sample_per_arm)
    return (
        int(counts.get("control", 0)) >= target
        and int(counts.get("candidate", 0)) >= target
    )


def _minimum_duration_elapsed(
    experiment: OnlineExperimentRecord,
    now: datetime,
) -> bool:
    if experiment.started_at is None:
        return False
    return _aware(now) >= _aware(experiment.started_at) + timedelta(
        hours=int(experiment.min_duration_hours)
    )


def _pause_for_sample_target(
    session: Session,
    experiment: OnlineExperimentRecord,
    *,
    counts: Mapping[str, int],
    now: datetime,
) -> None:
    """Persist the deterministic fixed-sample stop and its audit evidence."""

    if experiment.status not in ACTIVE_EXPERIMENT_STATUSES:
        return
    previous = experiment.status
    experiment.status = "paused"
    experiment.paused_from = previous
    experiment.paused_by = "system:sample_target"
    experiment.pause_reason = "pre-registered per-arm sample target reached"
    experiment.paused_at = now
    experiment.generation += 1
    experiment.updated_at = now
    result = _experiment_dict(experiment)
    request_hash = canonical_checksum(
        {
            "experiment_id": experiment.id,
            "required_sample_per_arm": experiment.required_sample_per_arm,
            "control_unique_users": int(counts.get("control", 0)),
            "candidate_unique_users": int(counts.get("candidate", 0)),
        }
    )
    _add_audit(
        session,
        tenant_id=experiment.tenant_id,
        experiment_id=experiment.id,
        action="auto_sample_target_pause",
        actor="system:sample_target",
        from_status=previous,
        to_status="paused",
        generation=experiment.generation,
        idempotency_key=f"sample-target:{experiment.id}",
        request_hash=request_hash,
        details={
            "reason": experiment.pause_reason,
            "required_sample_per_arm": experiment.required_sample_per_arm,
            "exposed_unique_users": {
                "control": int(counts.get("control", 0)),
                "candidate": int(counts.get("candidate", 0)),
            },
        },
        result=result,
        now=now,
    )


def _deployment_dict(record: OnlineStrategyDeploymentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "source_proposal_id": record.source_proposal_id,
        "source_strategy_version_id": record.source_strategy_version_id,
        "source_manifest_checksum": record.source_manifest_checksum,
        "compiled_overrides": _json(record.compiled_overrides),
        "compiler_version": record.compiler_version,
        "compiled_checksum": record.compiled_checksum,
        "record_checksum": record.record_checksum,
        "created_by": record.created_by,
        "created_at": record.created_at,
        "scope": "online_experiment_candidate",
    }


def _experiment_dict(record: OnlineExperimentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "name": record.name,
        "hypothesis": record.hypothesis,
        "surface": record.surface,
        "status": record.status,
        "candidate_deployment_id": record.candidate_deployment_id,
        "control_overrides": _json(record.control_overrides),
        "baseline_runtime_overrides": _json(record.baseline_runtime_overrides),
        "runtime_environment_fingerprint": record.runtime_environment_fingerprint,
        "runtime_identity_evidence": _json(record.runtime_identity_evidence),
        "candidate_allocation_bps": record.candidate_allocation_bps,
        "initial_enrollment_bps": record.initial_enrollment_bps,
        "enrollment_bps": record.enrollment_bps,
        "primary_metric": record.primary_metric,
        "aggregation_rule": record.aggregation_rule,
        "baseline_rate": record.baseline_rate,
        "minimum_detectable_effect": record.minimum_detectable_effect,
        "alpha": record.alpha,
        "power": record.power,
        "required_sample_per_arm": record.required_sample_per_arm,
        "min_duration_hours": record.min_duration_hours,
        "max_duration_hours": record.max_duration_hours,
        "attribution_window_hours": record.attribution_window_hours,
        "traffic_provenance": record.traffic_provenance,
        "audience_policy_version": record.audience_policy_version,
        "hmac_key_id": record.hmac_key_id,
        "preregistration_checksum": record.preregistration_checksum,
        "generation": record.generation,
        "created_by": record.created_by,
        "reviewed_by": record.reviewed_by,
        "review_note": record.review_note,
        "reviewed_at": record.reviewed_at,
        "submitted_at": record.submitted_at,
        "started_by": record.started_by,
        "started_at": record.started_at,
        "paused_by": record.paused_by,
        "pause_reason": record.pause_reason,
        "paused_from": record.paused_from,
        "paused_at": record.paused_at,
        "completed_by": record.completed_by,
        "completed_at": record.completed_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "is_active": record.status in ACTIVE_EXPERIMENT_STATUSES,
        "auto_activate": False,
    }


def _assignment_dict(record: ExperimentAssignmentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "assignment_id": record.id,
        "tenant_id": record.tenant_id,
        "experiment_id": record.experiment_id,
        "subject_digest": record.subject_digest,
        "enrollment_bucket": record.enrollment_bucket,
        "variant_bucket": record.variant_bucket,
        "bucket": record.variant_bucket,
        "arm": record.arm,
        "allocation_bps": record.allocation_bps,
        "experiment_generation": record.experiment_generation,
        "assigned_at": record.assigned_at,
    }


def _exposure_dict(record: ExperimentExposureRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "exposure_id": record.id,
        "opaque_exposure_id": record.id,
        "tenant_id": record.tenant_id,
        "experiment_id": record.experiment_id,
        "assignment_id": record.assignment_id,
        "turn_id": record.turn_id,
        "trace_id": record.trace_id,
        "request_fingerprint": record.request_fingerprint,
        "arm": record.arm,
        "deployment_id": record.deployment_id,
        "runtime_strategy_checksum": record.runtime_strategy_checksum,
        "traffic_provenance": record.traffic_provenance,
        "audience_policy_version": record.audience_policy_version,
        "audience_provenance": record.audience_provenance,
        "audience_eligible": bool(record.audience_eligible),
        "audience_account_created_at": record.audience_account_created_at,
        "audience_attestation": record.audience_attestation,
        "status": record.status,
        "latency_ms": record.latency_ms,
        "metrics": _json(record.metrics),
        "safety_events": _json(record.safety_events),
        "started_at": record.started_at,
        "completed_at": record.completed_at,
    }


def _outcome_dict(record: ExperimentOutcomeRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "experiment_id": record.experiment_id,
        "exposure_id": record.exposure_id,
        "event_id": record.event_id,
        "metric": record.metric,
        "value": record.value,
        "rating": 1 if record.value >= 1.0 else -1,
        "source": record.source,
        "included": record.included,
        "exclusion_reason": record.exclusion_reason,
        "occurred_at": record.occurred_at,
        "received_at": record.received_at,
    }


def _audit_dict(record: ExperimentAuditEventRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "experiment_id": record.experiment_id,
        "deployment_id": record.deployment_id,
        "action": record.action,
        "actor": record.actor,
        "from_status": record.from_status,
        "to_status": record.to_status,
        "generation": record.generation,
        "request_hash": record.request_hash,
        "idempotency_key": record.idempotency_key,
        "details": _json(record.details),
        "result": _json(record.result),
        "chain_position": record.chain_position,
        "previous_event_hash": record.previous_event_hash,
        "event_hash": record.event_hash,
        "created_at": record.created_at,
    }


def _json(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            default=_json_default,
        )
    )


def _json_default(value: Any) -> str:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _required(value: Any, field: str, maximum: int = 200) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


def _sha256_hex(value: Any, field: str) -> str:
    text = _required(value, field, 64).lower()
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return text


def _limit(value: int, maximum: int = 1000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _max_duration_elapsed(
    record: OnlineExperimentRecord, now: datetime
) -> bool:
    if record.started_at is None:
        return False
    deadline = _aware(record.started_at) + timedelta(
        hours=int(record.max_duration_hours)
    )
    return now >= deadline


__all__ = [
    "ExperimentConflictError",
    "ExperimentStore",
    "ExperimentStoreError",
    "GenerationConflictError",
    "IdempotencyConflictError",
    "canonical_checksum",
]
