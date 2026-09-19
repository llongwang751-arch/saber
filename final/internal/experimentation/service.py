"""Trusted orchestration for the v1 online RAG experiment lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from internal.evaluation.strategy import manifest_sha256

from .assignment import select_arm, stable_buckets
from .audience import (
    AUDIENCE_POLICY_VERSION,
    attest_audience_snapshot,
    internal_audience_decision,
    production_audience_decision,
    verify_audience_attestation,
)
from .models import ACTIVE_EXPERIMENT_STATUSES
from .runtime import RUNTIME_COMPILER_VERSION, compile_runtime_overrides
from .runtime_identity import (
    ANALYSIS_PLAN_VERSION,
    RUNTIME_IDENTITY_SCHEMA_VERSION,
    RuntimeIdentity,
    RuntimeIdentityError,
)
from .schemas import ExperimentCreate, RAGRuntimeOverrides, TrafficProvenance
from .statistics import analyze_binary_outcome, required_sample_size_binary, srm_exact
from .store import ExperimentStore, canonical_checksum


PRIMARY_METRIC = "positive_feedback"
SURFACE = "rag_chat"
AGGREGATION_RULE = "first_feedback_per_exposed_user_v1"
DEFAULT_ATTRIBUTION_WINDOW_HOURS = 168
_TERMINAL_EXPOSURE_STATUSES = frozenset({"completed", "error", "cancelled"})
_TRUSTED_SAFETY_SOURCES = frozenset({"server_guardrail", "server_evaluator"})


class ExperimentService:
    """Control plane plus request-time assignment and evidence recording.

    ``strategy_evidence_resolver`` is a tenant-scoped trust boundary.  It must
    return ``{"proposal": ..., "strategy": ...}`` from P2's evaluation store.
    The service validates all identities/checksums again before deployment.
    """

    def __init__(
        self,
        store: ExperimentStore,
        *,
        hmac_secret: str | bytes | None = None,
        traffic_provenance: str | TrafficProvenance | None = None,
        strategy_evidence_resolver: Callable[[str, str], Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        production_evidence_ready: bool | None = None,
        baseline_runtime_overrides: Mapping[str, Any] | None = None,
        runtime_identity: RuntimeIdentity | None = None,
        runtime_identity_provider: Callable[[], RuntimeIdentity] | None = None,
        runtime_environment_fingerprint: str | None = None,
    ) -> None:
        self.store = store
        configured_secret = hmac_secret
        if configured_secret is None:
            configured_secret = os.getenv("AGI_EXPERIMENT_HMAC_SECRET", "")
        if isinstance(configured_secret, str):
            secret_bytes = configured_secret.encode("utf-8")
        elif isinstance(configured_secret, bytes):
            secret_bytes = configured_secret
        else:
            raise TypeError("hmac_secret must be str or bytes")
        self.hmac_secret_configured = bool(secret_bytes)
        requested_provenance = traffic_provenance or os.getenv(
            "AGI_ONLINE_TRAFFIC_PROVENANCE", "disabled"
        )
        self.traffic_provenance = TrafficProvenance(requested_provenance)
        if not secret_bytes:
            # Deterministic only for a local disabled control-plane demo.  It
            # cannot route traffic and can never produce a production claim.
            secret_bytes = b"agi-experiment-disabled-development-key"
            self.traffic_provenance = TrafficProvenance.DISABLED
        if (
            self.traffic_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED
            and len(secret_bytes) < 32
        ):
            raise ValueError("production experiment HMAC secret must be at least 32 bytes")
        self._hmac_secret = secret_bytes
        self.hmac_key_id = hashlib.sha256(secret_bytes).hexdigest()[:16]
        self.storage_backend = str(self.store.engine.dialect.name)
        self.production_evidence_ready = (
            self.storage_backend != "sqlite"
            if production_evidence_ready is None
            else bool(production_evidence_ready)
        )
        self.baseline_runtime_overrides = _normalize_baseline_runtime(
            baseline_runtime_overrides
        )
        baseline_rag = self.baseline_runtime_overrides.get("rag", {})
        self.baseline_runtime_complete = set(baseline_rag) == {
            "top_k",
            "no_answer_threshold",
        }
        self._runtime_identity_provider = runtime_identity_provider
        self._runtime_identity = runtime_identity
        self.runtime_identity_error_code = ""
        if runtime_identity is None and runtime_identity_provider is not None:
            try:
                self._runtime_identity = runtime_identity_provider()
            except RuntimeIdentityError as exc:
                self.runtime_identity_error_code = exc.code
            except Exception:
                self.runtime_identity_error_code = "runtime_component_manifest_unverified"
        self.runtime_identity_verified = _verified_runtime_identity(
            self._runtime_identity
        )
        if self.runtime_identity_verified:
            self.runtime_environment_fingerprint = str(
                self._runtime_identity.fingerprint
            ).lower()
            self.runtime_identity_evidence = _runtime_identity_evidence(
                self._runtime_identity
            )
        elif self.traffic_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED:
            # The legacy free-form SHA is deliberately ignored in production.
            # Shape validation cannot prove code/model/index/corpus identity.
            self.runtime_environment_fingerprint = ""
            self.runtime_identity_evidence = {}
        else:
            configured_environment = (
                runtime_environment_fingerprint
                if runtime_environment_fingerprint is not None
                else os.getenv("AGI_EXPERIMENT_RUNTIME_ENVIRONMENT_FINGERPRINT", "")
            )
            self.runtime_environment_fingerprint = str(
                configured_environment or ""
            ).strip().lower()
            self.runtime_identity_evidence = {}
        self.runtime_environment_fingerprint_configured = _is_sha256(
            self.runtime_environment_fingerprint
        )
        self.strategy_evidence_resolver = strategy_evidence_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self.store.close()

    def readiness(self) -> dict[str, Any]:
        """Report routing readiness without overstating evidence provenance."""

        blockers: list[str] = []
        if not self.hmac_secret_configured:
            blockers.append("hmac_secret_missing")
        if self.traffic_provenance == TrafficProvenance.DISABLED:
            blockers.append("traffic_provenance_disabled")
        if (
            self.traffic_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED
            and not self.production_evidence_ready
        ):
            blockers.append("production_evidence_store_not_ready")
        if self.traffic_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED:
            if not self.baseline_runtime_complete:
                blockers.append("baseline_runtime_config_incomplete")
            try:
                self._current_runtime_identity_state()
            except (RuntimeIdentityError, ValueError):
                blockers.append("runtime_component_manifest_unverified")
        routing_blockers = {
            "hmac_secret_missing",
            "traffic_provenance_disabled",
            "production_evidence_store_not_ready",
            "baseline_runtime_config_incomplete",
            "runtime_component_manifest_unverified",
        }
        can_route = not any(item in routing_blockers for item in blockers)
        start_blockers = [item for item in blockers if item in routing_blockers]
        return {
            "ready": not blockers,
            "surface": SURFACE,
            "traffic_provenance": self.traffic_provenance.value,
            "hmac_configured": self.hmac_secret_configured,
            "hmac_key_id": self.hmac_key_id if self.hmac_secret_configured else None,
            "storage_backend": self.storage_backend,
            "production_evidence_ready": self.production_evidence_ready,
            "runtime_compiler_version": RUNTIME_COMPILER_VERSION,
            "baseline_runtime_complete": self.baseline_runtime_complete,
            "runtime_environment_fingerprint_configured": (
                self.runtime_environment_fingerprint_configured
            ),
            "runtime_component_manifest_verified": self.runtime_identity_verified,
            "runtime_identity_error_code": self.runtime_identity_error_code or None,
            "runtime_identity_evidence": dict(self.runtime_identity_evidence),
            "audience_policy_version": AUDIENCE_POLICY_VERSION,
            "supported_roles": ["participant", "experiment_admin", "experiment_approver"],
            "can_route": can_route,
            "can_start": can_route,
            "start_blockers": start_blockers,
            "can_claim_production": (
                can_route
                and self.traffic_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED
                and self.production_evidence_ready
            ),
            "blockers": blockers,
        }

    # -- approved strategy compilation ------------------------------------

    def create_deployment(
        self,
        tenant_id: str,
        proposal_id: str,
        *,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if self.strategy_evidence_resolver is None:
            raise RuntimeError("tenant-scoped offline strategy evidence is unavailable")
        evidence = self.strategy_evidence_resolver(tenant_id, proposal_id)
        if not isinstance(evidence, Mapping):
            raise ValueError("strategy evidence resolver returned an invalid envelope")
        evidence_tenant = str(evidence.get("tenant_id") or tenant_id)
        if evidence_tenant != tenant_id:
            raise LookupError("offline proposal not found for tenant")
        proposal = evidence.get("proposal")
        strategy = evidence.get("strategy")
        if not isinstance(proposal, Mapping) or not isinstance(strategy, Mapping):
            raise ValueError("strategy evidence requires proposal and strategy snapshots")
        if str(proposal.get("id") or "") != str(proposal_id):
            raise ValueError("offline proposal evidence id does not match")
        if str(proposal.get("status") or "") not in {"approved", "activated"}:
            raise ValueError("online deployment requires an approved offline proposal")
        strategy_id = str(strategy.get("id") or "")
        if strategy_id != str(proposal.get("candidate_strategy_version_id") or ""):
            raise ValueError("offline proposal and strategy version do not match")
        if str(strategy.get("source") or "") != "offline_eval":
            raise ValueError("strategy source must be offline_eval")
        manifest = strategy.get("manifest")
        if not isinstance(manifest, Mapping):
            raise ValueError("offline strategy manifest is missing")
        canonical, actual_checksum = manifest_sha256(manifest)
        declared_checksum = str(strategy.get("manifest_checksum") or "")
        if not declared_checksum or not hmac.compare_digest(
            actual_checksum, declared_checksum
        ):
            raise ValueError("offline strategy manifest checksum does not match")
        compiled = compile_runtime_overrides(manifest)
        compiled_checksum = canonical_checksum(compiled)
        request = {
            "tenant_id": tenant_id,
            "proposal_id": proposal_id,
            "strategy_id": strategy_id,
            "manifest_checksum": actual_checksum,
            "compiled_checksum": compiled_checksum,
            "actor": actor,
        }
        return self.store.create_deployment(
            tenant_id,
            source_proposal_id=proposal_id,
            source_strategy_version_id=strategy_id,
            source_manifest_checksum=actual_checksum,
            compiled_overrides=compiled,
            compiler_version=RUNTIME_COMPILER_VERSION,
            compiled_checksum=compiled_checksum,
            actor=actor,
            idempotency_key=idempotency_key,
            request_hash=canonical_checksum(request),
            now=self._now(),
        )

    def get_deployment(self, tenant_id: str, deployment_id: str) -> dict[str, Any]:
        return self.store.get_deployment(tenant_id, deployment_id)

    def list_deployments(self, tenant_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_deployments(tenant_id, limit=limit)

    # -- lifecycle ---------------------------------------------------------

    def create_experiment(
        self,
        tenant_id: str,
        *,
        name: str,
        candidate_deployment_id: str,
        baseline_rate: float,
        minimum_detectable_effect: float,
        creator: str,
        idempotency_key: str,
        hypothesis: str = "",
        primary_metric: str = PRIMARY_METRIC,
        alpha: float = 0.05,
        power: float = 0.8,
        candidate_allocation_bps: int = 5000,
        control_overrides: Mapping[str, Any] | RAGRuntimeOverrides | None = None,
        enrollment_bps: int = 1000,
        min_duration_hours: int = 168,
        max_duration_hours: int = 672,
        attribution_window_hours: int = DEFAULT_ATTRIBUTION_WINDOW_HOURS,
    ) -> dict[str, Any]:
        try:
            control_model = (
                control_overrides
                if isinstance(control_overrides, RAGRuntimeOverrides)
                else RAGRuntimeOverrides.model_validate(dict(control_overrides or {}))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("v1 control_overrides must be empty") from exc
        control = control_model.model_dump(exclude_none=True)
        if control:
            raise ValueError("v1 control_overrides must be empty")
        # Use the public typed contract for all shared fields.  The latest
        # schema includes enrollment/attribution; getattr keeps compatibility
        # while a rolling worker imports an older schema module.
        body = ExperimentCreate(
            name=name,
            hypothesis=hypothesis,
            candidate_deployment_id=candidate_deployment_id,
            primary_metric=primary_metric,
            baseline_rate=baseline_rate,
            minimum_detectable_effect=minimum_detectable_effect,
            alpha=alpha,
            power=power,
            candidate_allocation_bps=candidate_allocation_bps,
            control_overrides={},
            enrollment_bps=enrollment_bps,
            min_duration_hours=min_duration_hours,
            max_duration_hours=max_duration_hours,
            attribution_window_hours=attribution_window_hours,
            idempotency_key=idempotency_key,
        )
        self.store.get_deployment(tenant_id, body.candidate_deployment_id)
        required = required_sample_size_binary(
            body.baseline_rate,
            body.minimum_detectable_effect,
            alpha=body.alpha,
            power=body.power,
        )
        runtime_fingerprint, runtime_identity_evidence = (
            self._current_runtime_identity_state()
        )
        values = {
            "name": body.name,
            "hypothesis": body.hypothesis,
            "candidate_deployment_id": body.candidate_deployment_id,
            "candidate_allocation_bps": body.candidate_allocation_bps,
            "baseline_runtime_overrides": self.baseline_runtime_overrides,
            "runtime_environment_fingerprint": runtime_fingerprint,
            "runtime_identity_evidence": runtime_identity_evidence,
            "enrollment_bps": int(getattr(body, "enrollment_bps", enrollment_bps)),
            "initial_enrollment_bps": int(
                getattr(body, "enrollment_bps", enrollment_bps)
            ),
            "primary_metric": body.primary_metric,
            "aggregation_rule": AGGREGATION_RULE,
            "baseline_rate": body.baseline_rate,
            "minimum_detectable_effect": body.minimum_detectable_effect,
            "alpha": body.alpha,
            "power": body.power,
            "required_sample_per_arm": required,
            "min_duration_hours": body.min_duration_hours,
            "max_duration_hours": body.max_duration_hours,
            "attribution_window_hours": int(
                getattr(body, "attribution_window_hours", attribution_window_hours)
            ),
            "traffic_provenance": self.traffic_provenance.value,
            "audience_policy_version": AUDIENCE_POLICY_VERSION,
            "hmac_key_id": self.hmac_key_id,
        }
        request_hash = canonical_checksum({**values, "actor": creator})
        return self.store.create_experiment(
            tenant_id,
            values,
            actor=creator,
            idempotency_key=body.idempotency_key,
            request_hash=request_hash,
            now=self._now(),
        )

    def get_experiment(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        return self.store.get_experiment(tenant_id, experiment_id)

    def list_experiments(
        self, tenant_id: str, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self.store.list_experiments(tenant_id, status=status, limit=limit)

    def submit_experiment(
        self, tenant_id: str, experiment_id: str, *, actor: str,
        expected_generation: int, idempotency_key: str, reason: str = "",
    ) -> dict[str, Any]:
        current = self.store.get_experiment(tenant_id, experiment_id)
        preregistration = _preregistration(current)
        checksum = canonical_checksum(preregistration)
        return self._transition(
            tenant_id, experiment_id,
            action="submit", actor=actor, expected_generation=expected_generation,
            idempotency_key=idempotency_key, reason=reason,
            allowed_from=("draft",), to_status="pending_review",
            updates={"preregistration_checksum": checksum, "submitted_at": self._now()},
            extra_request={"preregistration_checksum": checksum},
        )

    def review_experiment(
        self, tenant_id: str, experiment_id: str, *, decision: str, actor: str,
        expected_generation: int, idempotency_key: str, reason: str = "",
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        target = "approved" if decision == "approve" else "rejected"
        now = self._now()
        return self._transition(
            tenant_id, experiment_id,
            action=decision, actor=actor, expected_generation=expected_generation,
            idempotency_key=idempotency_key, reason=reason,
            allowed_from=("pending_review",), to_status=target,
            updates={"reviewed_by": actor, "review_note": reason, "reviewed_at": now},
            forbid_creator=True,
        )

    def start_experiment(
        self, tenant_id: str, experiment_id: str, *, actor: str,
        expected_generation: int, idempotency_key: str, reason: str = "",
        target_status: str = "canary",
    ) -> dict[str, Any]:
        if self.traffic_provenance == TrafficProvenance.DISABLED:
            raise ValueError("online experiment routing is disabled")
        current = self.store.get_experiment(tenant_id, experiment_id)
        self._enforce_online_integrity(current)
        if target_status not in ACTIVE_EXPERIMENT_STATUSES:
            raise ValueError("target_status must be canary or running")
        now = self._now()
        updates: dict[str, Any] = {"started_by": actor, "started_at": now}
        if target_status == "running":
            updates["enrollment_bps"] = 10_000
        return self._transition(
            tenant_id, experiment_id,
            action=f"start_{target_status}", actor=actor,
            expected_generation=expected_generation, idempotency_key=idempotency_key,
            reason=reason, allowed_from=("approved",), to_status=target_status,
            updates=updates,
        )

    def ramp_experiment(
        self, tenant_id: str, experiment_id: str, *, target_enrollment_bps: int,
        actor: str, expected_generation: int, idempotency_key: str, reason: str = "",
    ) -> dict[str, Any]:
        if isinstance(target_enrollment_bps, bool) or not isinstance(target_enrollment_bps, int):
            raise ValueError("target_enrollment_bps must be an integer")
        current = self.store.get_experiment(tenant_id, experiment_id)
        self._enforce_online_integrity(current)
        if not int(current["enrollment_bps"]) < target_enrollment_bps <= 10_000:
            raise ValueError("enrollment ramp must increase and remain <= 10000")
        target_status = "running" if target_enrollment_bps == 10_000 else "canary"
        return self._transition(
            tenant_id, experiment_id,
            action="ramp", actor=actor, expected_generation=expected_generation,
            idempotency_key=idempotency_key, reason=reason,
            allowed_from=("canary", "running"), to_status=target_status,
            updates={"enrollment_bps": target_enrollment_bps},
            extra_request={"target_enrollment_bps": target_enrollment_bps},
        )

    def pause_experiment(
        self, tenant_id: str, experiment_id: str, *, actor: str,
        expected_generation: int, idempotency_key: str, reason: str = "",
    ) -> dict[str, Any]:
        current = self.store.get_experiment(tenant_id, experiment_id)
        return self._transition(
            tenant_id, experiment_id,
            action="pause", actor=actor, expected_generation=expected_generation,
            idempotency_key=idempotency_key, reason=reason,
            allowed_from=("canary", "running"), to_status="paused",
            updates={
                "paused_by": actor, "pause_reason": reason,
                "paused_from": current["status"], "paused_at": self._now(),
            },
        )

    def resume_experiment(
        self, tenant_id: str, experiment_id: str, *, actor: str,
        expected_generation: int, idempotency_key: str, reason: str = "",
        target_status: str | None = None,
        safety_approved: bool = False,
    ) -> dict[str, Any]:
        current = self.store.get_experiment(tenant_id, experiment_id)
        if self.traffic_provenance == TrafficProvenance.DISABLED:
            raise ValueError("online experiment routing is disabled")
        self._enforce_online_integrity(current)
        if current["status"] == "safety_paused":
            if not safety_approved:
                raise ValueError("safety-paused experiments require approver authorization")
            if actor in {current.get("created_by"), current.get("started_by")}:
                raise ValueError(
                    "the experiment creator or starter cannot clear a safety pause"
                )
        deadline = _collection_deadline(current)
        if deadline is not None and self._now() >= deadline:
            raise ValueError("experiment maximum collection duration elapsed")
        paused_from = current.get("paused_from") or "canary"
        if target_status is not None and target_status != paused_from:
            raise ValueError("resume target_status must match the recorded paused_from state")
        target = paused_from
        if target not in ACTIVE_EXPERIMENT_STATUSES:
            raise ValueError("resume target_status must be canary or running")
        return self._transition(
            tenant_id, experiment_id,
            action="resume", actor=actor, expected_generation=expected_generation,
            idempotency_key=idempotency_key, reason=reason,
            allowed_from=("paused", "safety_paused"), to_status=target,
            updates={"paused_by": "", "pause_reason": "", "paused_at": None},
            extra_request={
                "target_status": target,
                "safety_approved": bool(safety_approved),
            },
        )

    def complete_experiment(
        self, tenant_id: str, experiment_id: str, *, actor: str,
        expected_generation: int, idempotency_key: str, reason: str = "",
    ) -> dict[str, Any]:
        now = self._now()
        evidence = self.store.evidence(tenant_id, experiment_id)
        current = evidence["experiment"]
        stop = _fixed_stopping_state(current, evidence["exposures"], now)
        if not stop["reached"]:
            raise ValueError(
                "experiment can complete only after both pre-registered arm "
                "sample targets or the maximum collection duration are reached"
            )
        return self._transition(
            tenant_id, experiment_id,
            action="complete", actor=actor, expected_generation=expected_generation,
            idempotency_key=idempotency_key, reason=reason,
            allowed_from=("canary", "running", "paused", "safety_paused"),
            to_status="completed",
            updates={"completed_by": actor, "completed_at": now},
            extra_request={
                "stopping_reason": stop["reason"],
                "exposed_unique_users": stop["counts"],
            },
        )

    def rollback_experiment(
        self, tenant_id: str, experiment_id: str, *, actor: str,
        expected_generation: int, idempotency_key: str, reason: str = "",
    ) -> dict[str, Any]:
        return self._transition(
            tenant_id, experiment_id,
            action="rollback", actor=actor, expected_generation=expected_generation,
            idempotency_key=idempotency_key, reason=reason,
            allowed_from=("canary", "running", "paused", "safety_paused"),
            to_status="rolled_back",
            updates={"completed_by": actor, "completed_at": self._now()},
        )

    # -- runtime API consumed by the real chat handler --------------------

    def resolve_assignment(
        self,
        tenant_id: str,
        user_id: str,
        surface: str,
        turn_id: str,
        trace_id: str,
        *,
        identity_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if surface != SURFACE:
            return None
        if self.traffic_provenance == TrafficProvenance.DISABLED:
            return None
        experiment = self.store.active_experiment(tenant_id, surface)
        if experiment is None:
            return None
        try:
            # Validate the frozen stop fields before trusting either the time
            # or sample boundary.  Runtime identity/deployment checks remain
            # after the stop checks so a key rotation cannot prevent a due
            # maximum-duration pause.
            self._assert_preregistration_integrity(experiment)
            self._assert_audit_chain_integrity(experiment)
        except (ValueError, LookupError) as exc:
            self.store.record_integrity_failure(
                tenant_id,
                experiment["id"],
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            return None
        collection = self.store.pause_if_max_duration_elapsed(
            tenant_id, experiment["id"], now=self._now()
        )
        experiment = collection["experiment"]
        if collection["paused"] or experiment["status"] not in ACTIVE_EXPERIMENT_STATUSES:
            return None
        sample_stop = self.store.pause_if_sample_target_reached(
            tenant_id, experiment["id"], now=self._now()
        )
        experiment = sample_stop["experiment"]
        if sample_stop["target_reached"] or experiment["status"] not in ACTIVE_EXPERIMENT_STATUSES:
            return None
        try:
            validated_deployment = self._assert_online_integrity(experiment)
        except (ValueError, LookupError) as exc:
            # Runtime drift is a durable safety event, not a silent fallback
            # that leaves a permanently active experiment occupying surface.
            self.store.record_integrity_failure(
                tenant_id,
                experiment["id"],
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            return None
        if self.traffic_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED:
            audience = production_audience_decision(
                identity_context,
                experiment,
                tenant_id=tenant_id,
            )
            if not audience["audience_eligible"]:
                # Ineligible staff, QA, bot, late-created or self-service
                # accounts stay on the stable default and never create an
                # assignment/exposure row.
                return None
        else:
            audience = internal_audience_decision(identity_context)
        buckets = stable_buckets(
            self._hmac_secret, tenant_id, experiment["id"], user_id
        )
        if buckets["enrollment_bucket"] >= int(experiment["enrollment_bps"]):
            return None
        arm = select_arm(
            buckets["variant_bucket"], int(experiment["candidate_allocation_bps"])
        )
        subject_digest = self._subject_digest(tenant_id, user_id)
        assignment = self.store.get_or_create_assignment(
            tenant_id,
            experiment["id"],
            subject_digest=subject_digest,
            enrollment_bucket=buckets["enrollment_bucket"],
            variant_bucket=buckets["variant_bucket"],
            arm=arm,
            allocation_bps=int(experiment["candidate_allocation_bps"]),
            experiment_generation=int(experiment["generation"]),
            now=self._now(),
        )
        deployment_id = (
            experiment["candidate_deployment_id"]
            if assignment["arm"] == "candidate" else None
        )
        runtime_overrides, runtime_checksum = _effective_runtime_configuration(
            experiment,
            validated_deployment,
            arm=str(assignment["arm"]),
        )
        audience_attestation = attest_audience_snapshot(
            self._hmac_secret,
            audience,
            tenant_id=tenant_id,
            experiment_id=str(experiment["id"]),
            assignment_id=str(assignment["id"]),
            subject_digest=str(assignment["subject_digest"]),
        )
        return {
            **assignment,
            "surface": surface,
            "turn_id": _text(turn_id, "turn_id", 100),
            "trace_id": _text(trace_id, "trace_id", 100),
            "deployment_id": deployment_id,
            "runtime_overrides": runtime_overrides,
            "runtime_strategy_checksum": runtime_checksum,
            "deployment_compiled_checksum": (
                str(validated_deployment["compiled_checksum"])
                if deployment_id else ""
            ),
            "runtime_compiler_version": RUNTIME_COMPILER_VERSION,
            "traffic_provenance": experiment["traffic_provenance"],
            "audience_policy_version": audience["audience_policy_version"],
            "audience_provenance": audience["audience_provenance"],
            "audience_eligible": audience["audience_eligible"],
            "audience_account_created_at": audience[
                "audience_account_created_at"
            ],
            "audience_attestation": audience_attestation,
        }

    def begin_exposure(self, allocation: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(allocation, Mapping):
            raise TypeError("allocation must be a mapping")
        tenant_id = _text(str(allocation.get("tenant_id") or ""), "tenant_id", 64)
        experiment_id = _text(
            str(allocation.get("experiment_id") or ""), "experiment_id", 200
        )
        experiment = self.store.get_experiment(tenant_id, experiment_id)
        try:
            self._assert_preregistration_integrity(experiment)
        except (ValueError, LookupError) as exc:
            self.store.record_integrity_failure(
                tenant_id,
                experiment_id,
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            raise
        collection = self.store.pause_if_max_duration_elapsed(
            tenant_id, experiment_id, now=self._now()
        )
        if collection["paused"] or collection["experiment"]["status"] not in ACTIVE_EXPERIMENT_STATUSES:
            raise ValueError("experiment is outside its collection window")
        sample_stop = self.store.pause_if_sample_target_reached(
            tenant_id, experiment_id, now=self._now()
        )
        if sample_stop["target_reached"] or sample_stop["experiment"]["status"] not in ACTIVE_EXPERIMENT_STATUSES:
            raise ValueError("experiment reached its pre-registered sample target")
        experiment = sample_stop["experiment"]
        assignment_id = str(allocation.get("assignment_id") or "")
        subject_digest = str(allocation.get("subject_digest") or "")
        audience_snapshot = {
            "audience_policy_version": allocation.get("audience_policy_version"),
            "audience_provenance": allocation.get("audience_provenance"),
            "audience_eligible": allocation.get("audience_eligible"),
            "audience_account_created_at": allocation.get(
                "audience_account_created_at"
            ),
        }
        if (
            str(audience_snapshot["audience_policy_version"] or "")
            != str(experiment.get("audience_policy_version") or "")
            or not verify_audience_attestation(
                self._hmac_secret,
                audience_snapshot,
                str(allocation.get("audience_attestation") or ""),
                tenant_id=tenant_id,
                experiment_id=experiment_id,
                assignment_id=assignment_id,
                subject_digest=subject_digest,
            )
            or (
                experiment.get("traffic_provenance")
                == TrafficProvenance.PRODUCTION_AUTHENTICATED.value
                and audience_snapshot["audience_eligible"] is not True
            )
        ):
            self.store.record_integrity_failure(
                tenant_id,
                experiment_id,
                reason="audience_evidence_integrity_failed",
                now=self._now(),
            )
            raise ValueError("experiment audience evidence integrity check failed")
        try:
            validated_deployment = self._assert_online_integrity(experiment)
        except (ValueError, LookupError) as exc:
            self.store.record_integrity_failure(
                tenant_id,
                experiment_id,
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            raise
        exposure = self.store.create_exposure(allocation, now=self._now())
        tenant_id = str(exposure["tenant_id"])
        runtime_overrides, runtime_checksum = _effective_runtime_configuration(
            experiment,
            validated_deployment,
            arm=str(exposure["arm"]),
        )
        if not hmac.compare_digest(
            str(exposure.get("runtime_strategy_checksum") or ""),
            runtime_checksum,
        ):
            raise ValueError("exposure effective runtime checksum changed")
        return {
            **exposure,
            "surface": SURFACE,
            "runtime_overrides": runtime_overrides,
            "runtime_strategy_checksum": runtime_checksum,
        }

    def finish_exposure(
        self,
        exposure: Mapping[str, Any] | str,
        *,
        status: str,
        latency_ms: float,
        metrics: Mapping[str, Any] | None = None,
        safety_events: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if status not in _TERMINAL_EXPOSURE_STATUSES:
            raise ValueError("exposure status must be completed, error, or cancelled")
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)):
            raise ValueError("latency_ms must be numeric")
        if float(latency_ms) < 0:
            raise ValueError("latency_ms must be non-negative")
        if isinstance(exposure, Mapping):
            exposure_id = str(exposure.get("exposure_id") or exposure.get("id") or "")
            tenant_id = str(exposure.get("tenant_id") or "")
        else:
            exposure_id = str(exposure)
            stored = self.store.get_exposure_by_id(exposure_id)
            tenant_id = str(stored["tenant_id"])
        stored_exposure = self.store.get_exposure(tenant_id, exposure_id)
        experiment = self.store.get_experiment(
            tenant_id, str(stored_exposure["experiment_id"])
        )
        try:
            deployment = self._assert_online_integrity(experiment)
            _runtime, expected_runtime_checksum = _effective_runtime_configuration(
                experiment,
                deployment,
                arm=str(stored_exposure.get("arm") or ""),
            )
            if not hmac.compare_digest(
                str(stored_exposure.get("runtime_strategy_checksum") or ""),
                expected_runtime_checksum,
            ):
                raise ValueError("exposure effective runtime checksum changed")
        except (ValueError, LookupError) as exc:
            self.store.record_integrity_failure(
                tenant_id,
                str(experiment["id"]),
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            raise
        events = [_validate_safety_event(item) for item in (safety_events or [])]
        trusted_severe = any(
            item["trusted"]
            and item["source"] in _TRUSTED_SAFETY_SOURCES
            and item["severity"] in {"S0", "S1"}
            for item in events
        )
        try:
            return self.store.finish_exposure(
                tenant_id,
                exposure_id,
                status=status,
                latency_ms=float(latency_ms),
                metrics=dict(metrics or {}),
                safety_events=events,
                trusted_severe=trusted_severe,
                now=self._now(),
            )
        except Exception as finish_error:
            if trusted_severe:
                try:
                    self.store.emergency_safety_pause(
                        tenant_id,
                        str(experiment["id"]),
                        exposure_id=exposure_id,
                        safety_events=events,
                        failure_code=type(finish_error).__name__,
                        now=self._now(),
                    )
                except Exception as emergency_error:
                    raise RuntimeError(
                        "exposure finish failed and emergency safety pause "
                        "could not be persisted"
                    ) from emergency_error
            raise

    def record_feedback(
        self,
        tenant_id: str,
        user_id: str,
        exposure_id: str,
        event_id: str,
        rating: int,
    ) -> dict[str, Any]:
        if isinstance(rating, bool) or rating not in {-1, 1}:
            raise ValueError("rating must be -1 or 1")
        exposure = self.store.get_exposure(tenant_id, exposure_id)
        experiment = self.store.get_experiment(tenant_id, exposure["experiment_id"])
        try:
            deployment = self._assert_online_integrity(experiment)
            _runtime, expected_runtime_checksum = _effective_runtime_configuration(
                experiment, deployment, arm=str(exposure.get("arm") or "")
            )
            if not hmac.compare_digest(
                str(exposure.get("runtime_strategy_checksum") or ""),
                expected_runtime_checksum,
            ):
                raise ValueError("exposure effective runtime checksum changed")
        except (ValueError, LookupError) as exc:
            self.store.record_integrity_failure(
                tenant_id,
                str(experiment["id"]),
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            raise
        now = self._now()
        started_at = _aware(exposure["started_at"])
        late = (now - started_at).total_seconds() > (
            int(experiment["attribution_window_hours"]) * 3600
        )
        completed = exposure["status"] == "completed"
        exclusion_reason = ""
        if not completed:
            exclusion_reason = f"exposure_status_{exposure['status']}"
        elif late:
            exclusion_reason = "outside_attribution_window"
        return self.store.record_outcome(
            tenant_id,
            subject_digest=self._subject_digest(tenant_id, user_id),
            exposure_id=exposure_id,
            event_id=event_id,
            metric=PRIMARY_METRIC,
            value=1.0 if rating == 1 else 0.0,
            included=completed and not late,
            exclusion_reason=exclusion_reason,
            now=now,
        )

    # -- truthful monitoring/analysis -------------------------------------

    def analyze_experiment(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        evidence = self.store.evidence(tenant_id, experiment_id)
        experiment = evidence["experiment"]
        assignments = evidence["assignments"]
        exposures = evidence["exposures"]
        outcomes = evidence["outcomes"]
        integrity_failure: str | None = None
        assignment_by_id = {
            str(item.get("id") or ""): item for item in assignments
        }
        try:
            deployment = self._assert_frozen_evidence_integrity(experiment)
            for exposure in exposures:
                _runtime, expected_runtime_checksum = _effective_runtime_configuration(
                    experiment,
                    deployment,
                    arm=str(exposure.get("arm") or ""),
                )
                if not hmac.compare_digest(
                    str(exposure.get("runtime_strategy_checksum") or ""),
                    expected_runtime_checksum,
                ):
                    raise ValueError("exposure effective runtime checksum changed")
                assignment = assignment_by_id.get(
                    str(exposure.get("assignment_id") or "")
                )
                if assignment is None or not verify_audience_attestation(
                    self._hmac_secret,
                    exposure,
                    str(exposure.get("audience_attestation") or ""),
                    tenant_id=tenant_id,
                    experiment_id=experiment_id,
                    assignment_id=str(exposure.get("assignment_id") or ""),
                    subject_digest=str(assignment.get("subject_digest") or ""),
                ):
                    raise ValueError("exposure audience evidence integrity changed")
                if (
                    experiment.get("traffic_provenance")
                    == TrafficProvenance.PRODUCTION_AUTHENTICATED.value
                    and exposure.get("audience_eligible") is not True
                ):
                    raise ValueError("production exposure audience is ineligible")
        except (ValueError, LookupError) as exc:
            integrity_failure = _integrity_reason(exc)
            self.store.record_integrity_failure(
                tenant_id,
                experiment_id,
                reason=integrity_failure,
                now=self._now(),
            )
            experiment = self.store.get_experiment(tenant_id, experiment_id)

        analysis_exposures = (
            [item for item in exposures if item.get("audience_eligible") is True]
            if experiment.get("traffic_provenance")
            == TrafficProvenance.PRODUCTION_AUTHENTICATED.value
            else exposures
        )
        first_by_assignment: dict[str, dict[str, Any]] = {}
        terminal_by_exposure: dict[str, dict[str, Any]] = {}
        for item in analysis_exposures:
            assignment_id = str(item["assignment_id"])
            first_by_assignment.setdefault(assignment_id, item)
            if item["status"] == "completed":
                terminal_by_exposure[str(item["id"])] = item

        # Outcomes may be attached to any later turn, but the preregistered
        # unit of analysis is one user.  Join outcome -> exposure -> assignment
        # and retain that user's earliest eligible feedback.
        included_by_assignment: dict[str, dict[str, Any]] = {}
        for item in sorted(
            outcomes,
            key=lambda value: (
                _aware(value["occurred_at"]), str(value.get("id") or "")
            ),
        ):
            if not item.get("included") or item.get("metric") != experiment["primary_metric"]:
                continue
            exposure_id = str(item["exposure_id"])
            exposure = terminal_by_exposure.get(exposure_id)
            if exposure is not None:
                included_by_assignment.setdefault(str(exposure["assignment_id"]), item)

        counts = {
            "control": {"successes": 0, "total": 0},
            "candidate": {"successes": 0, "total": 0},
        }
        arm_by_assignment = {
            str(item["id"]): str(item["arm"]) for item in assignments
        }
        # Intent-to-treat: every genuinely exposed user is in the denominator,
        # including errors/cancellations/no-feedback.  This prevents a faulty
        # candidate from looking good by considering only responders.
        for assignment_id in first_by_assignment:
            arm = arm_by_assignment[assignment_id]
            counts[arm]["total"] += 1
        for assignment_id, outcome in included_by_assignment.items():
            arm = arm_by_assignment[assignment_id]
            counts[arm]["successes"] += int(float(outcome["value"]) >= 1.0)

        assigned_counts = {
            arm: sum(1 for item in assignments if item["arm"] == arm)
            for arm in ("control", "candidate")
        }
        exposed_counts = {
            arm: sum(
                1
                for assignment_id in first_by_assignment
                if arm_by_assignment.get(assignment_id) == arm
            )
            for arm in ("control", "candidate")
        }
        now = self._now()
        stopping = _fixed_stopping_state(experiment, analysis_exposures, now)
        srm_result = srm_exact(
            exposed_counts["control"],
            exposed_counts["candidate"],
            candidate_allocation_bps=int(experiment["candidate_allocation_bps"]),
        )
        started = experiment.get("started_at")
        observation_end = _observation_end(experiment, now)
        duration_reached = bool(
            started
            and (observation_end - _aware(started)).total_seconds()
            >= int(experiment["min_duration_hours"]) * 3600
        )
        collection_deadline = (
            _aware(started) + timedelta(hours=int(experiment["max_duration_hours"]))
            if started else None
        )
        max_duration_reached = bool(stopping["max_duration_reached"])
        last_exposure_at = max(
            (_aware(item["started_at"]) for item in analysis_exposures),
            default=None,
        )
        outcome_matures_at = (
            last_exposure_at
            + timedelta(hours=int(experiment["attribution_window_hours"]))
            if last_exposure_at is not None else None
        )
        outcome_maturity_reached = bool(
            outcome_matures_at is not None and now >= outcome_matures_at
        )
        stale_started_exposures = [
            item
            for item in analysis_exposures
            if item.get("status") == "started"
            and now
            >= _aware(item["started_at"])
            + timedelta(hours=int(experiment["attribution_window_hours"]))
        ]
        analysis = analyze_binary_outcome(
            counts["control"]["successes"],
            counts["control"]["total"],
            counts["candidate"]["successes"],
            counts["candidate"]["total"],
            provenance=experiment["traffic_provenance"],
            experiment_status=experiment["status"],
            required_sample_per_arm=int(experiment["required_sample_per_arm"]),
            minimum_duration_reached=duration_reached,
            srm=srm_result,
            alpha=float(experiment["alpha"]),
        )
        has_safety = any(
            event.get("trusted") is True
            and event.get("severity") in {"S0", "S1"}
            and event.get("source") in _TRUSTED_SAFETY_SOURCES
            for exposure in analysis_exposures
            for event in (exposure.get("safety_events") or [])
            if isinstance(event, Mapping)
        )
        if has_safety:
            analysis = _block_inference(analysis, "trusted_s0_s1_safety_event")
        if stale_started_exposures:
            analysis = _block_inference(analysis, "stale_started_exposure")
        if not outcome_maturity_reached:
            analysis = _block_inference(
                analysis,
                "outcome_attribution_window_open",
                preserve_status=True,
            )
        if (
            experiment["traffic_provenance"] == "production_authenticated"
            and not self.production_evidence_ready
        ):
            analysis = _block_inference(
                analysis, "production_evidence_store_not_ready"
            )
            analysis["has_real_traffic"] = False
        if integrity_failure is not None:
            analysis = _block_inference(analysis, integrity_failure)
            analysis["has_real_traffic"] = False

        # Any outcome-dependent/manual early termination remains blinded too;
        # rollback is not a loophole for optional stopping.
        blinded = not (stopping["reached"] and outcome_maturity_reached)
        if integrity_failure is not None:
            blinded = True
        if blinded:
            analysis = _blind_until_preregistered_stop(analysis)
            srm_result = _blind_srm(srm_result)

        real_exposure_count = (
            len(first_by_assignment)
            if experiment["traffic_provenance"] == "production_authenticated"
            and self.production_evidence_ready
            and integrity_failure is None
            else 0
        )
        result = {
            **analysis,
            "experiment_id": experiment_id,
            "status": experiment["status"],
            "traffic_provenance": experiment["traffic_provenance"],
            "aggregation_rule": experiment["aggregation_rule"],
            "collection_deadline": collection_deadline,
            "max_duration_reached": max_duration_reached,
            "sample_target_reached": stopping["sample_target_reached"],
            "stopping_condition_reached": stopping["reached"],
            "stopping_reason": stopping["reason"],
            "analysis_blinded": blinded,
            "integrity": {
                "valid": integrity_failure is None,
                "blocker": integrity_failure,
            },
            "outcome_maturity_reached": outcome_maturity_reached,
            "outcome_matures_at": outcome_matures_at,
            "real_exposure_count": real_exposure_count,
            "sample": {
                "required_per_arm": int(experiment["required_sample_per_arm"]),
                "total_assigned_unique_users": sum(assigned_counts.values()),
                "total_exposed_unique_users": sum(exposed_counts.values()),
                "total_feedback_responders": len(included_by_assignment),
                "stale_started_exposures": len(stale_started_exposures),
                "assigned_control_unique_users": None if blinded else assigned_counts["control"],
                "assigned_candidate_unique_users": None if blinded else assigned_counts["candidate"],
                "control_unique_users": None if blinded else exposed_counts["control"],
                "candidate_unique_users": None if blinded else exposed_counts["candidate"],
                "control_itt_users": None if blinded else counts["control"]["total"],
                "candidate_itt_users": None if blinded else counts["candidate"]["total"],
                "control_outcomes": None if blinded else sum(
                    1
                    for assignment_id in included_by_assignment
                    if arm_by_assignment[assignment_id] == "control"
                ),
                "candidate_outcomes": None if blinded else sum(
                    1
                    for assignment_id in included_by_assignment
                    if arm_by_assignment[assignment_id] == "candidate"
                ),
                "control_feedback_responders": None if blinded else sum(
                    1
                    for assignment_id in included_by_assignment
                    if arm_by_assignment[assignment_id] == "control"
                ),
                "candidate_feedback_responders": None if blinded else sum(
                    1
                    for assignment_id in included_by_assignment
                    if arm_by_assignment[assignment_id] == "candidate"
                ),
                "outcome_maturity_reached": outcome_maturity_reached,
                "outcome_matures_at": outcome_matures_at,
                "minimum_duration_reached": duration_reached,
                "sufficient": (
                    counts["control"]["total"] >= int(experiment["required_sample_per_arm"])
                    and counts["candidate"]["total"] >= int(experiment["required_sample_per_arm"])
                    and duration_reached
                    and outcome_maturity_reached
                    and not stale_started_exposures
                ),
            },
            "srm": srm_result,
            "outcome": {
                "metric": experiment["primary_metric"],
                "control": analysis.get("control"),
                "candidate": analysis.get("candidate"),
                "effect": analysis.get("absolute_effect"),
                "ci_low": (
                    analysis.get("confidence_interval", [None, None])[0]
                    if analysis.get("confidence_interval") else None
                ),
                "ci_high": (
                    analysis.get("confidence_interval", [None, None])[1]
                    if analysis.get("confidence_interval") else None
                ),
                "p_value": analysis.get("p_value"),
                "feedback_coverage": {
                    arm: (None if blinded else (
                        sum(
                            1
                            for assignment_id in included_by_assignment
                            if arm_by_assignment[assignment_id] == arm
                        ) / counts[arm]["total"]
                        if counts[arm]["total"] else None
                    ))
                    for arm in ("control", "candidate")
                },
            },
            "auto_activate": False,
        }
        self.store.save_monitor_snapshot(
            tenant_id, experiment_id, result, now=self._now()
        )
        return result

    def list_audit_events(
        self, tenant_id: str, *, experiment_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self.store.list_audit_events(
            tenant_id, experiment_id=experiment_id, limit=limit
        )

    def _transition(
        self,
        tenant_id: str,
        experiment_id: str,
        *,
        action: str,
        actor: str,
        expected_generation: int,
        idempotency_key: str,
        reason: str,
        allowed_from: Sequence[str],
        to_status: str,
        updates: Mapping[str, Any] | None = None,
        extra_request: Mapping[str, Any] | None = None,
        forbid_creator: bool = False,
    ) -> dict[str, Any]:
        current = self.store.get_experiment(tenant_id, experiment_id)
        try:
            # A checksum is first created by submit.  Draft fields remain
            # editable until then, but the deployment they reference is
            # immutable and validated for every lifecycle mutation.
            self._assert_frozen_evidence_integrity(current)
        except (ValueError, LookupError) as exc:
            self.store.record_integrity_failure(
                tenant_id,
                experiment_id,
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            raise
        request_hash = canonical_checksum({
            "tenant_id": tenant_id,
            "experiment_id": experiment_id,
            "action": action,
            "actor": actor,
            "expected_generation": expected_generation,
            "reason": reason,
            **dict(extra_request or {}),
        })
        return self.store.transition_experiment(
            tenant_id,
            experiment_id,
            action=action,
            actor=actor,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            allowed_from=allowed_from,
            to_status=to_status,
            updates=updates,
            details={"reason": reason, **dict(extra_request or {})},
            forbid_creator=forbid_creator,
            now=self._now(),
        )

    def _subject_digest(self, tenant_id: str, user_id: str) -> str:
        message = (
            b"agi-exp-subject-v1\x00"
            + _text(tenant_id, "tenant_id", 64).encode("utf-8")
            + b"\x00user\x00"
            + _text(user_id, "user_id", 200).encode("utf-8")
        )
        return hmac.new(self._hmac_secret, message, hashlib.sha256).hexdigest()

    def _assert_online_integrity(
        self, experiment: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate all frozen experiment and deployment evidence at use time."""

        self._assert_runtime_identity(experiment)
        self._assert_audit_chain_integrity(experiment)
        return self._validated_deployment(
            str(experiment.get("tenant_id") or ""),
            str(experiment.get("candidate_deployment_id") or ""),
        )

    def _enforce_online_integrity(
        self, experiment: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            return self._assert_online_integrity(experiment)
        except (ValueError, LookupError) as exc:
            self.store.record_integrity_failure(
                str(experiment.get("tenant_id") or ""),
                str(experiment.get("id") or ""),
                reason=_integrity_reason(exc),
                now=self._now(),
            )
            raise

    def _assert_frozen_evidence_integrity(
        self, experiment: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate frozen evidence without requiring this node to route traffic."""

        self._assert_preregistration_integrity(experiment)
        self._assert_audit_chain_integrity(experiment)
        stored_provenance = str(experiment.get("traffic_provenance") or "")
        if stored_provenance != self.traffic_provenance.value:
            raise ValueError("experiment traffic provenance changed since preregistration")
        if str(experiment.get("hmac_key_id") or "") != self.hmac_key_id:
            raise ValueError("experiment HMAC assignment key changed since preregistration")
        if canonical_checksum(
            experiment.get("baseline_runtime_overrides") or {}
        ) != canonical_checksum(self.baseline_runtime_overrides):
            raise ValueError("baseline runtime configuration changed since preregistration")
        if str(experiment.get("audience_policy_version") or "") != AUDIENCE_POLICY_VERSION:
            raise ValueError("experiment audience policy changed since preregistration")
        runtime_fingerprint, runtime_identity_evidence = (
            self._current_runtime_identity_state()
        )
        if not hmac.compare_digest(
            str(experiment.get("runtime_environment_fingerprint") or ""),
            runtime_fingerprint,
        ):
            raise ValueError("runtime environment fingerprint changed since preregistration")
        if canonical_checksum(
            experiment.get("runtime_identity_evidence") or {}
        ) != canonical_checksum(runtime_identity_evidence):
            raise ValueError("runtime component evidence changed since preregistration")
        return self._validated_deployment(
            str(experiment.get("tenant_id") or ""),
            str(experiment.get("candidate_deployment_id") or ""),
        )

    def _assert_audit_chain_integrity(
        self, experiment: Mapping[str, Any]
    ) -> None:
        verification = self.store.verify_experiment_audit_chain(
            str(experiment.get("tenant_id") or ""),
            str(experiment.get("id") or ""),
        )
        if verification.get("valid") is not True:
            reason = str(verification.get("reason") or "audit_chain_invalid")
            raise ValueError(f"experiment audit chain integrity check failed: {reason}")

    def _assert_preregistration_integrity(
        self, experiment: Mapping[str, Any]
    ) -> None:
        declared = str(experiment.get("preregistration_checksum") or "")
        status = str(experiment.get("status") or "")
        if status == "draft" and not declared:
            return
        if not _is_sha256(declared):
            raise ValueError("experiment preregistration checksum is missing or invalid")
        actual = canonical_checksum(_preregistration(experiment))
        if not hmac.compare_digest(actual, declared):
            raise ValueError("experiment preregistration integrity check failed")

    def _validated_deployment(
        self, tenant_id: str, deployment_id: str
    ) -> dict[str, Any]:
        deployment = self.store.get_deployment(tenant_id, deployment_id)
        if deployment.get("compiler_version") != RUNTIME_COMPILER_VERSION:
            raise ValueError("candidate deployment compiler version changed")
        source_checksum = str(deployment.get("source_manifest_checksum") or "")
        declared = str(deployment.get("compiled_checksum") or "")
        record_checksum = str(deployment.get("record_checksum") or "")
        if not (
            _is_sha256(source_checksum)
            and _is_sha256(declared)
            and _is_sha256(record_checksum)
        ):
            raise ValueError("candidate deployment evidence checksum is invalid")
        compiled = deployment.get("compiled_overrides")
        if not isinstance(compiled, Mapping) or set(compiled) != {"rag"}:
            raise ValueError("candidate deployment overrides violate the v1 allowlist")
        rag = compiled.get("rag")
        if not isinstance(rag, Mapping):
            raise ValueError("candidate deployment RAG overrides are invalid")
        try:
            normalized = RAGRuntimeOverrides.model_validate(dict(rag)).runtime_payload()
        except (TypeError, ValueError) as exc:
            raise ValueError("candidate deployment RAG overrides are invalid") from exc
        if normalized != dict(compiled):
            raise ValueError("candidate deployment overrides are not canonical")
        actual = canonical_checksum(compiled)
        if not hmac.compare_digest(actual, declared):
            raise ValueError("candidate deployment integrity check failed")
        actual_record_checksum = canonical_checksum(
            {
                "tenant_id": deployment.get("tenant_id"),
                "source_proposal_id": deployment.get("source_proposal_id"),
                "source_strategy_version_id": deployment.get(
                    "source_strategy_version_id"
                ),
                "source_manifest_checksum": source_checksum,
                "compiled_overrides": compiled,
                "compiler_version": deployment.get("compiler_version"),
                "compiled_checksum": declared,
            }
        )
        if not hmac.compare_digest(actual_record_checksum, record_checksum):
            raise ValueError("candidate deployment evidence record was modified")
        return deployment

    def _assert_runtime_identity(self, experiment: Mapping[str, Any]) -> None:
        self._assert_preregistration_integrity(experiment)
        stored_provenance = str(experiment.get("traffic_provenance") or "")
        if stored_provenance == TrafficProvenance.DISABLED.value:
            raise ValueError("experiment was preregistered with disabled traffic provenance")
        if stored_provenance != self.traffic_provenance.value:
            raise ValueError("experiment traffic provenance changed since preregistration")
        if (
            stored_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED.value
            and not self.production_evidence_ready
        ):
            raise ValueError("production experiment evidence store is not ready")
        if (
            stored_provenance == TrafficProvenance.PRODUCTION_AUTHENTICATED.value
            and (
                not self.baseline_runtime_complete
                or not self.runtime_environment_fingerprint_configured
            )
        ):
            raise ValueError("production runtime environment evidence is not ready")
        if str(experiment.get("hmac_key_id") or "") != self.hmac_key_id:
            raise ValueError("experiment HMAC assignment key changed since preregistration")
        if canonical_checksum(
            experiment.get("baseline_runtime_overrides") or {}
        ) != canonical_checksum(self.baseline_runtime_overrides):
            raise ValueError("baseline runtime configuration changed since preregistration")
        if str(experiment.get("audience_policy_version") or "") != AUDIENCE_POLICY_VERSION:
            raise ValueError("experiment audience policy changed since preregistration")
        runtime_fingerprint, runtime_identity_evidence = (
            self._current_runtime_identity_state()
        )
        if not hmac.compare_digest(
            str(experiment.get("runtime_environment_fingerprint") or ""),
            runtime_fingerprint,
        ):
            raise ValueError("runtime environment fingerprint changed since preregistration")
        if canonical_checksum(
            experiment.get("runtime_identity_evidence") or {}
        ) != canonical_checksum(runtime_identity_evidence):
            raise ValueError("runtime component evidence changed since preregistration")

    def _current_runtime_identity_state(self) -> tuple[str, dict[str, Any]]:
        """Re-verify the deployment-owned component identity on every gate."""

        if self.traffic_provenance != TrafficProvenance.PRODUCTION_AUTHENTICATED:
            return (
                self.runtime_environment_fingerprint,
                dict(self.runtime_identity_evidence),
            )
        candidate = self._runtime_identity
        if self._runtime_identity_provider is not None:
            try:
                candidate = self._runtime_identity_provider()
            except RuntimeIdentityError as exc:
                self.runtime_identity_error_code = exc.code
                self.runtime_identity_verified = False
                self.runtime_environment_fingerprint_configured = False
                raise
            except Exception as exc:
                self.runtime_identity_error_code = (
                    "runtime_component_manifest_unverified"
                )
                self.runtime_identity_verified = False
                self.runtime_environment_fingerprint_configured = False
                raise RuntimeIdentityError(
                    self.runtime_identity_error_code,
                    "production runtime component manifest verification failed",
                ) from exc
        if not _verified_runtime_identity(candidate):
            self.runtime_identity_error_code = (
                self.runtime_identity_error_code
                or "runtime_component_manifest_unverified"
            )
            self.runtime_identity_verified = False
            self.runtime_environment_fingerprint_configured = False
            raise RuntimeIdentityError(
                self.runtime_identity_error_code,
                "production runtime identity is not verified",
            )
        evidence = _runtime_identity_evidence(candidate)
        if self._runtime_identity is not None and _verified_runtime_identity(
            self._runtime_identity
        ):
            original_evidence = _runtime_identity_evidence(self._runtime_identity)
            if not hmac.compare_digest(
                str(candidate.fingerprint), str(self._runtime_identity.fingerprint)
            ) or canonical_checksum(evidence) != canonical_checksum(
                original_evidence
            ):
                self.runtime_identity_error_code = "runtime_identity_drift"
                self.runtime_identity_verified = False
                self.runtime_environment_fingerprint_configured = False
                raise RuntimeIdentityError(
                    self.runtime_identity_error_code,
                    "production runtime identity changed after service startup",
                )
        else:
            self._runtime_identity = candidate
        self.runtime_identity_verified = True
        self.runtime_identity_error_code = ""
        self.runtime_environment_fingerprint = str(candidate.fingerprint).lower()
        self.runtime_environment_fingerprint_configured = True
        self.runtime_identity_evidence = evidence
        return self.runtime_environment_fingerprint, dict(evidence)

    def _now(self) -> datetime:
        return _aware(self._clock())


def _preregistration(experiment: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "surface", "candidate_deployment_id", "control_overrides",
        "baseline_runtime_overrides", "runtime_environment_fingerprint",
        "candidate_allocation_bps", "initial_enrollment_bps", "primary_metric",
        "aggregation_rule",
        "baseline_rate", "minimum_detectable_effect", "alpha", "power",
        "required_sample_per_arm", "min_duration_hours", "max_duration_hours",
        "attribution_window_hours", "traffic_provenance", "hmac_key_id",
        "audience_policy_version",
    )
    result = {
        **{key: experiment.get(key) for key in fields},
        "aggregation_rule": experiment.get("aggregation_rule") or AGGREGATION_RULE,
    }
    runtime_identity_evidence = experiment.get("runtime_identity_evidence") or {}
    if runtime_identity_evidence:
        # 0016 cannot retroactively mint trustworthy component evidence for
        # already preregistered experiments.  Keeping the absent legacy field
        # out of their checksum preserves the old audit fact, while the runtime
        # gate still rejects their empty evidence and pauses traffic.  Every
        # newly created production experiment freezes the non-empty envelope.
        result["runtime_identity_evidence"] = runtime_identity_evidence
    return result


def _validate_safety_event(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("safety event must be an object")
    allowed = {"severity", "rule_id", "trusted", "source", "evidence"}
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"unknown safety event fields: {sorted(extra)}")
    severity = str(value.get("severity") or "")
    source = str(value.get("source") or "")
    trusted = value.get("trusted", False)
    if severity not in {"S0", "S1", "S2", "S3"}:
        raise ValueError("safety severity must be S0, S1, S2, or S3")
    if source not in _TRUSTED_SAFETY_SOURCES:
        raise ValueError("safety source must be a trusted server source")
    if not isinstance(trusted, bool):
        raise ValueError("safety trusted must be boolean")
    rule_id = _text(str(value.get("rule_id") or ""), "rule_id", 200)
    evidence = value.get("evidence") or {}
    if not isinstance(evidence, Mapping):
        raise ValueError("safety evidence must be an object")
    return {
        "severity": severity,
        "rule_id": rule_id,
        "trusted": trusted,
        "source": source,
        "evidence": dict(evidence),
    }


def _block_inference(
    analysis: Mapping[str, Any],
    reason: str,
    *,
    preserve_status: bool = False,
) -> dict[str, Any]:
    result = dict(analysis)
    blockers = list(result.get("claim_blockers") or [])
    if reason not in blockers:
        blockers.append(reason)
    result.update({
        "truth": "no_claim",
        "analysis_status": (
            result.get("analysis_status")
            if preserve_status
            and result.get("analysis_status") != "confirmatory"
            else "blocked"
        ),
        "can_claim_effect": False,
        "claim_blockers": blockers,
        "winner": None,
        "absolute_effect": None,
        "relative_lift": None,
        "z_score": None,
        "p_value": None,
        "confidence_interval": None,
        "statistically_significant": None,
    })
    return result


def _blind_until_preregistered_stop(
    analysis: Mapping[str, Any],
) -> dict[str, Any]:
    """Hide treatment outcomes while an operator could still choose to stop.

    Returning arm rates during collection would let an administrator repeatedly
    peek and complete only on favourable noise.  Aggregate operational counts
    remain available in ``sample``; treatment statistics are revealed only
    after a pre-registered fixed stopping condition has been reached.
    """

    result = _block_inference(
        analysis,
        "analysis_blinded_until_preregistered_stop",
    )
    result.update(
        {
            "analysis_status": "blinded",
            "control": None,
            "candidate": None,
            "descriptive": {
                "absolute_difference": None,
                "relative_lift": None,
            },
        }
    )
    return result


def _blind_srm(srm: Mapping[str, Any]) -> dict[str, Any]:
    """Keep allocation-integrity status without revealing per-arm counts."""

    result = dict(srm)
    for key in (
        "control_count",
        "candidate_count",
        "expected_control_count",
        "expected_candidate_count",
    ):
        result[key] = None
    return result


def _text(value: str, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if "\x00" in text:
        raise ValueError(f"{field} must not contain NUL")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return text


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _verified_runtime_identity(value: Any) -> bool:
    """Validate the strong identity envelope, not merely a hash-shaped string."""

    if not isinstance(value, RuntimeIdentity) or value.production_verified is not True:
        return False
    if value.schema_version != RUNTIME_IDENTITY_SCHEMA_VERSION:
        return False
    if not all(
        _is_sha256(str(item or "").lower())
        for item in (
            value.fingerprint,
            value.component_manifest_artifact_sha256,
            value.component_manifest_payload_sha256,
        )
    ):
        return False
    if not os.path.isabs(str(value.component_manifest_path or "")):
        return False
    try:
        identity_payload = json.loads(value.canonical_identity_json)
        component_payload = json.loads(value.canonical_component_manifest_json)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(identity_payload, Mapping) or not isinstance(
        component_payload, Mapping
    ):
        return False
    canonical_identity = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_components = json.dumps(
        component_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical_identity != value.canonical_identity_json:
        return False
    if canonical_components != value.canonical_component_manifest_json:
        return False
    if identity_payload.get("schema_version") != value.schema_version:
        return False
    if (
        identity_payload.get("component_manifest_payload_sha256")
        != value.component_manifest_payload_sha256
        or identity_payload.get("components") != component_payload
    ):
        return False
    return (
        hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()
        == str(value.fingerprint).lower()
        and hashlib.sha256(canonical_components.encode("utf-8")).hexdigest()
        == str(value.component_manifest_payload_sha256).lower()
    )


def _runtime_identity_evidence(value: RuntimeIdentity | None) -> dict[str, Any]:
    if not _verified_runtime_identity(value):
        raise RuntimeIdentityError(
            "runtime_component_manifest_unverified",
            "production runtime identity evidence is invalid",
        )
    assert value is not None
    return {
        "schema_version": value.schema_version,
        "component_manifest_artifact_sha256": (
            value.component_manifest_artifact_sha256
        ),
        "component_manifest_payload_sha256": (
            value.component_manifest_payload_sha256
        ),
        "runtime_compiler_version": RUNTIME_COMPILER_VERSION,
        "analysis_plan_version": ANALYSIS_PLAN_VERSION,
    }


def _normalize_baseline_runtime(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) != {"rag"}:
        raise ValueError("baseline_runtime_overrides must contain only rag")
    raw_rag = value.get("rag")
    if not isinstance(raw_rag, Mapping):
        raise ValueError("baseline_runtime_overrides.rag must be an object")
    try:
        return RAGRuntimeOverrides.model_validate(dict(raw_rag)).runtime_payload()
    except (TypeError, ValueError) as exc:
        raise ValueError("baseline_runtime_overrides are invalid") from exc


def _effective_runtime_configuration(
    experiment: Mapping[str, Any],
    deployment: Mapping[str, Any],
    *,
    arm: str,
) -> tuple[dict[str, Any], str]:
    if arm not in {"control", "candidate"}:
        raise ValueError("experiment arm must be control or candidate")
    baseline = experiment.get("baseline_runtime_overrides") or {}
    if not isinstance(baseline, Mapping):
        raise ValueError("frozen baseline runtime configuration is invalid")
    raw_baseline_rag = baseline.get("rag") or {}
    if not isinstance(raw_baseline_rag, Mapping):
        raise ValueError("frozen baseline RAG configuration is invalid")
    effective_rag = dict(raw_baseline_rag)
    if arm == "candidate":
        compiled = deployment.get("compiled_overrides") or {}
        if not isinstance(compiled, Mapping) or not isinstance(
            compiled.get("rag"), Mapping
        ):
            raise ValueError("candidate deployment RAG overrides are invalid")
        effective_rag.update(dict(compiled["rag"]))
    effective = {"rag": effective_rag} if effective_rag else {}
    checksum = canonical_checksum(
        {
            "runtime_environment_fingerprint": experiment.get(
                "runtime_environment_fingerprint"
            ),
            "runtime_overrides": effective,
        }
    )
    return effective, checksum


def _integrity_reason(error: Exception) -> str:
    """Map internal validation details to a stable, non-secret audit reason."""

    message = str(error).lower()
    if "audit chain" in message:
        return "audit_chain_integrity_failed"
    if (
        "runtime environment" in message
        or "runtime component" in message
        or "runtime identity" in message
        or "baseline runtime" in message
    ):
        return "runtime_environment_integrity_failed"
    if "hmac" in message:
        return "assignment_key_integrity_failed"
    if "provenance" in message or "evidence store" in message:
        return "traffic_provenance_integrity_failed"
    if "deployment" in message or "override" in message or "compiler" in message:
        return "deployment_integrity_failed"
    if "preregistration" in message:
        return "preregistration_integrity_failed"
    if "exposure" in message:
        return "exposure_integrity_failed"
    return "experiment_integrity_failed"


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("clock and stored timestamps must be datetime values")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _collection_deadline(experiment: Mapping[str, Any]) -> datetime | None:
    started_at = experiment.get("started_at")
    if started_at is None:
        return None
    return _aware(started_at) + timedelta(
        hours=int(experiment["max_duration_hours"])
    )


def _fixed_stopping_state(
    experiment: Mapping[str, Any],
    exposures: Sequence[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Evaluate only the immutable fixed-N / fixed-horizon stop rules."""

    first_arm_by_assignment: dict[str, str] = {}
    for exposure in sorted(
        exposures,
        key=lambda item: (
            _aware(item["started_at"]),
            str(item.get("id") or ""),
        ),
    ):
        assignment_id = str(exposure.get("assignment_id") or "")
        arm = str(exposure.get("arm") or "")
        if assignment_id and arm in {"control", "candidate"}:
            first_arm_by_assignment.setdefault(assignment_id, arm)
    counts = {
        arm: sum(1 for value in first_arm_by_assignment.values() if value == arm)
        for arm in ("control", "candidate")
    }
    target = int(experiment["required_sample_per_arm"])
    sample_target_reached = (
        counts["control"] >= target and counts["candidate"] >= target
    )
    minimum_duration_reached = bool(
        experiment.get("started_at")
        and _aware(now)
        >= _aware(experiment["started_at"])
        + timedelta(hours=int(experiment["min_duration_hours"]))
    )
    sample_stopping_ready = sample_target_reached and minimum_duration_reached
    deadline = _collection_deadline(experiment)
    max_duration_reached = bool(
        deadline is not None
        and _observation_end(experiment, _aware(now)) >= deadline
    )
    reason = (
        "sample_target_and_minimum_duration"
        if sample_stopping_ready
        else ("max_duration" if max_duration_reached else None)
    )
    return {
        "reached": bool(reason),
        "reason": reason,
        "sample_target_reached": sample_target_reached,
        "minimum_duration_reached": minimum_duration_reached,
        "sample_stopping_ready": sample_stopping_ready,
        "max_duration_reached": max_duration_reached,
        "counts": counts,
        "required_per_arm": target,
        "deadline": deadline,
    }


def _observation_end(
    experiment: Mapping[str, Any], now: datetime
) -> datetime:
    """Freeze duration at collection stop time, not analysis-view time."""

    candidates = [_aware(now)]
    deadline = _collection_deadline(experiment)
    if deadline is not None:
        candidates.append(deadline)
    for field in ("paused_at", "completed_at"):
        value = experiment.get(field)
        if value is not None:
            candidates.append(_aware(value))
    return min(candidates)


__all__ = [
    "DEFAULT_ATTRIBUTION_WINDOW_HOURS",
    "ExperimentService",
    "PRIMARY_METRIC",
    "SURFACE",
]
