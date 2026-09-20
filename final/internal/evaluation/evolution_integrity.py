"""受控演进（evolution）建议的完整性校验——从 evaluation/store.py 拆出。

这一簇函数是纯业务校验（证据校验和、manifest 一致性、no_auto_apply 不变量、
审计链验证），不含任何持久化写入；ORM 模型仍归 store.py 所有，
因此此处对 store 的引用全部走函数内惰性导入（与本文件既有风格一致），
避免 store <-> 校验模块的顶层循环导入。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    # 仅注解使用；运行时经函数内惰性导入（见各函数体）。
    from .store import EvolutionSuggestionRecord, StrategyVersionRecord

def verify_evolution_record(
    session: Session, record: EvolutionSuggestionRecord
) -> None:
    from .store import (  # 惰性导入：避免与 store.py 的顶层循环依赖
        ImmutableEvolutionSuggestionError,
        _jsonable,
    )
    from .evolution import (
        canonical_checksum,
        validate_suggestion_manifest,
        verify_evidence_checksums,
    )
    from .strategy import canonical_manifest_json

    evidence = _jsonable(record.evidence)
    verify_evidence_checksums(evidence)
    if canonical_checksum(evidence) != record.evidence_checksum:
        raise ImmutableEvolutionSuggestionError("evolution evidence checksum mismatch")
    manifest = validate_suggestion_manifest(record.suggestion_manifest)
    canonical = canonical_manifest_json(manifest)
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if canonical != record.manifest_canonical_json or checksum != record.manifest_checksum:
        raise ImmutableEvolutionSuggestionError("evolution suggestion manifest checksum mismatch")
    truth = _jsonable(record.truth)
    if not record.no_auto_apply or truth.get("no_auto_apply") is not True:
        raise ImmutableEvolutionSuggestionError("evolution suggestion auto-apply invariant failed")
    if truth.get("can_claim_online_improvement") is not False:
        raise ImmutableEvolutionSuggestionError("evolution truth claim invariant failed")
    verify_source_strategy_evidence(session, record, evidence)
    verify_evolution_audit_chain(session, record)


def verify_source_strategy_evidence(
    session: Session,
    record: EvolutionSuggestionRecord,
    evidence: Mapping[str, Any],
) -> None:
    """Bind the source strategy row to the immutable evidence envelope."""

    from .store import (  # 惰性导入：避免与 store.py 的顶层循环依赖
        ImmutableEvolutionSuggestionError,
        StrategyVersionRecord,
        _jsonable,
    )
    from .evolution import canonical_checksum
    from .strategy import canonical_manifest_json

    snapshot = evidence.get("strategy_version")
    if record.source_strategy_version_id is None:
        if snapshot is not None:
            raise ImmutableEvolutionSuggestionError(
                "evolution source strategy evidence is inconsistent"
            )
        return
    if not isinstance(snapshot, Mapping):
        raise ImmutableEvolutionSuggestionError(
            "evolution source strategy evidence is missing"
        )
    strategy = session.get(StrategyVersionRecord, record.source_strategy_version_id)
    if strategy is None:
        raise ImmutableEvolutionSuggestionError(
            "evolution source strategy version is missing"
        )
    manifest = _jsonable(strategy.manifest)
    canonical = canonical_manifest_json(manifest)
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        canonical != strategy.manifest_canonical_json
        or checksum != strategy.manifest_checksum
    ):
        raise ImmutableEvolutionSuggestionError(
            "evolution source strategy manifest checksum mismatch"
        )
    current_payload = {
        "id": strategy.id,
        "version": strategy.version,
        "source": strategy.source,
        "manifest": manifest,
        "manifest_canonical_json": canonical,
        "manifest_checksum": checksum,
    }
    current_snapshot = {
        **current_payload,
        "checksum": canonical_checksum(current_payload),
    }
    if _jsonable(snapshot) != current_snapshot:
        raise ImmutableEvolutionSuggestionError(
            "evolution source strategy no longer matches immutable evidence"
        )


def verify_evolution_audit_chain(
    session: Session, record: EvolutionSuggestionRecord
) -> None:
    from .store import (  # 惰性导入：避免与 store.py 的顶层循环依赖
        _EVOLUTION_AUDIT_GENESIS,
        EvolutionAuditEventRecord,
        ImmutableEvolutionSuggestionError,
        StrategyVersionRecord,
        _empty_evolution_state,
        _evolution_event_checksum,
        _evolution_state_snapshot,
        _jsonable,
    )
    """Derive lifecycle state from the append-only hash chain and compare it.

    Database constraints and ORM listeners stop ordinary application mistakes.
    This verifier is deliberately independent of them so raw SQL changes to a
    suggestion state, review metadata, materialization reference, audit body or
    audit membership fail closed at every read and lifecycle transition.
    """

    from .evolution import canonical_checksum

    audits = session.scalars(
        select(EvolutionAuditEventRecord)
        .where(EvolutionAuditEventRecord.suggestion_id == record.id)
        .order_by(EvolutionAuditEventRecord.sequence)
    ).all()
    if int(record.audit_event_count) != len(audits) or not audits:
        raise ImmutableEvolutionSuggestionError(
            "evolution lifecycle audit count mismatch"
        )
    expected_previous = _EVOLUTION_AUDIT_GENESIS
    simulated: dict[str, Any] | None = None
    initial_create_count = 0
    review_count = 0
    materialize_count = 0

    for expected_sequence, audit in enumerate(audits, start=1):
        if audit.sequence != expected_sequence:
            raise ImmutableEvolutionSuggestionError(
                "evolution lifecycle audit sequence is not contiguous"
            )
        if audit.previous_event_checksum != expected_previous:
            raise ImmutableEvolutionSuggestionError(
                "evolution lifecycle audit chain is broken"
            )
        if _evolution_event_checksum(audit) != audit.event_checksum:
            raise ImmutableEvolutionSuggestionError(
                "evolution lifecycle audit checksum mismatch"
            )
        details = _jsonable(audit.details)
        if not isinstance(details, dict):
            raise ImmutableEvolutionSuggestionError(
                "evolution lifecycle audit details are invalid"
            )
        request = details.get("request")
        if not isinstance(request, dict) or canonical_checksum(request) != audit.request_checksum:
            raise ImmutableEvolutionSuggestionError(
                "evolution lifecycle audit request checksum mismatch"
            )
        declared_state = details.get("state")
        if not isinstance(declared_state, dict):
            raise ImmutableEvolutionSuggestionError(
                "evolution lifecycle audit state commitment is missing"
            )

        if audit.action == "evolution_create":
            expected_request = {
                "source_run_id": record.source_run_id,
                "dataset_version_id": record.dataset_version_id,
                "source_strategy_version_id": record.source_strategy_version_id,
                "rule_version": record.rule_version,
                "evidence_checksum": record.evidence_checksum,
                "manifest_checksum": record.manifest_checksum,
                "actor": audit.actor,
            }
            if request != expected_request:
                raise ImmutableEvolutionSuggestionError(
                    "evolution create audit request does not match immutable evidence"
                )
            if details.get("created") is True:
                initial_create_count += 1
                if (
                    expected_sequence != 1
                    or audit.actor != record.created_by
                    or details.get("from_status") is not None
                    or details.get("to_status") != "proposed"
                    or details.get("from_generation") != -1
                    or details.get("to_generation") != 0
                    or details.get("rule_version") != record.rule_version
                    or details.get("evidence_checksum") != record.evidence_checksum
                    or details.get("manifest_checksum") != record.manifest_checksum
                ):
                    raise ImmutableEvolutionSuggestionError(
                        "evolution create audit transition is invalid"
                    )
                expected_state = _empty_evolution_state()
            else:
                if (
                    simulated is None
                    or details.get("created") is not False
                    or details.get("deterministic_duplicate") is not True
                    or details.get("from_status") != simulated["status"]
                    or details.get("to_status") != simulated["status"]
                    or details.get("from_generation") != simulated["generation"]
                    or details.get("to_generation") != simulated["generation"]
                ):
                    raise ImmutableEvolutionSuggestionError(
                        "evolution duplicate-create audit transition is invalid"
                    )
                expected_state = simulated
        elif audit.action == "evolution_review":
            review_count += 1
            decision = details.get("decision")
            if decision not in {"accept", "reject"}:
                raise ImmutableEvolutionSuggestionError(
                    "evolution review audit decision is invalid"
                )
            expected_request = {
                "suggestion_id": record.id,
                "decision": decision,
                "reviewer": audit.actor,
                "note": details.get("note"),
                "expected_generation": 0,
            }
            target_status = "accepted" if decision == "accept" else "rejected"
            if (
                simulated is None
                or simulated != _empty_evolution_state()
                or review_count != 1
                or audit.actor == record.created_by
                or request != expected_request
                or details.get("from_status") != "proposed"
                or details.get("to_status") != target_status
                or details.get("from_generation") != 0
                or details.get("to_generation") != 1
            ):
                raise ImmutableEvolutionSuggestionError(
                    "evolution review audit transition is invalid"
                )
            expected_state = {
                **simulated,
                "status": target_status,
                "generation": 1,
                "reviewed_by": audit.actor,
                "review_note": details.get("note"),
                "reviewed_at": declared_state.get("reviewed_at"),
            }
            if not expected_state["reviewed_at"]:
                raise ImmutableEvolutionSuggestionError(
                    "evolution review audit timestamp is missing"
                )
        elif audit.action == "evolution_materialize":
            materialize_count += 1
            expected_request = {
                "suggestion_id": record.id,
                "name": request.get("name"),
                "actor": audit.actor,
                "expected_generation": 1,
            }
            strategy_version_id = details.get("strategy_version_id")
            if (
                simulated is None
                or simulated.get("status") != "accepted"
                or simulated.get("generation") != 1
                or review_count != 1
                or materialize_count != 1
                or request != expected_request
                or not isinstance(request.get("name"), str)
                or not request["name"].strip()
                or details.get("from_status") != "accepted"
                or details.get("to_status") != "accepted"
                or details.get("from_generation") != 1
                or details.get("to_generation") != 2
                or not isinstance(strategy_version_id, str)
                or not strategy_version_id
                or details.get("auto_promotion") is not False
                or details.get("auto_activation") is not False
                or details.get("auto_deployment") is not False
            ):
                raise ImmutableEvolutionSuggestionError(
                    "evolution materialization audit transition is invalid"
                )
            expected_state = {
                **simulated,
                "generation": 2,
                "materialized_strategy_version_id": strategy_version_id,
                "materialized_by": audit.actor,
                "materialized_at": declared_state.get("materialized_at"),
            }
            if not expected_state["materialized_at"]:
                raise ImmutableEvolutionSuggestionError(
                    "evolution materialization audit timestamp is missing"
                )
        else:
            raise ImmutableEvolutionSuggestionError(
                f"unsupported evolution audit action: {audit.action}"
            )

        if declared_state != expected_state:
            raise ImmutableEvolutionSuggestionError(
                "evolution lifecycle audit state commitment mismatch"
            )
        simulated = dict(expected_state)
        expected_previous = audit.event_checksum

    if initial_create_count != 1 or simulated != _evolution_state_snapshot(record):
        raise ImmutableEvolutionSuggestionError(
            "evolution record state does not match its lifecycle audit"
        )
    if record.audit_head_checksum != expected_previous:
        raise ImmutableEvolutionSuggestionError(
            "evolution lifecycle audit head checksum mismatch"
        )
    expected_review_count = 0 if record.status == "proposed" else 1
    if review_count != expected_review_count:
        raise ImmutableEvolutionSuggestionError(
            "evolution review audit does not match suggestion status"
        )
    expected_materialize_count = 1 if record.materialized_strategy_version_id else 0
    if materialize_count != expected_materialize_count:
        raise ImmutableEvolutionSuggestionError(
            "evolution materialization audit does not match suggestion state"
        )
    if materialize_count:
        strategy = session.get(
            StrategyVersionRecord, record.materialized_strategy_version_id
        )
        if strategy is None:
            raise ImmutableEvolutionSuggestionError(
                "materialized strategy version is missing"
            )
        verify_materialized_strategy(record, strategy)




def verify_materialized_strategy(
    suggestion: EvolutionSuggestionRecord, strategy: StrategyVersionRecord
) -> None:
    from .store import (  # 惰性导入：避免与 store.py 的顶层循环依赖
        ImmutableEvolutionSuggestionError,
        STRATEGY_SOURCE,
    )
    from .strategy import canonical_manifest_json

    canonical = canonical_manifest_json(strategy.manifest)
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if (
        strategy.source != STRATEGY_SOURCE
        or canonical != strategy.manifest_canonical_json
        or checksum != strategy.manifest_checksum
        or canonical != suggestion.manifest_canonical_json
        or checksum != suggestion.manifest_checksum
    ):
        raise ImmutableEvolutionSuggestionError(
            "materialized strategy does not match the accepted suggestion"
        )
