"""Server-side eligibility and attestations for production A/B evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


AUDIENCE_POLICY_VERSION = "production_authenticated_v1"
TRUSTED_IDENTITY_PROVENANCE = frozenset({"operator_provisioned", "trusted_sso"})
PRIVILEGED_EXPERIMENT_ROLES = frozenset(
    {"experiment_admin", "experiment_approver"}
)


def production_audience_decision(
    identity: Mapping[str, Any] | None,
    experiment: Mapping[str, Any],
    *,
    tenant_id: str,
) -> dict[str, Any]:
    """Return a privacy-preserving, fail-closed eligibility decision.

    Eligibility comes only from the authenticated server-side user record.  It
    cannot be supplied by a chat request.  Accounts provisioned after the
    preregistration submission are excluded so an operator cannot manufacture
    extra subjects after seeing an experiment.
    """

    value = identity if isinstance(identity, Mapping) else {}
    provenance = str(value.get("identity_provenance") or "unverified").strip()
    roles = _roles(value.get("roles"))
    created_at = _datetime(value.get("created_at"))
    submitted_at = _datetime(experiment.get("submitted_at"))
    reasons: list[str] = []
    if str(value.get("tenant_id") or "").strip() != str(tenant_id).strip():
        reasons.append("tenant_mismatch")
    if value.get("experiment_eligible") is not True:
        reasons.append("not_operator_eligible")
    if provenance not in TRUSTED_IDENTITY_PROVENANCE:
        reasons.append("untrusted_identity_provenance")
    if roles & PRIVILEGED_EXPERIMENT_ROLES:
        reasons.append("privileged_control_plane_identity")
    if created_at is None:
        reasons.append("account_created_at_missing")
    if submitted_at is None:
        reasons.append("experiment_submission_time_missing")
    if created_at is not None and submitted_at is not None and created_at > submitted_at:
        reasons.append("account_created_after_preregistration")
    return {
        "audience_policy_version": AUDIENCE_POLICY_VERSION,
        "audience_provenance": provenance[:40] or "unverified",
        "audience_eligible": not reasons,
        "audience_account_created_at": created_at,
        # Reasons are useful for server diagnostics but must not be written to
        # the public trace (they can reveal identity-management information).
        "audience_exclusion_reasons": reasons,
    }


def internal_audience_decision(
    identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Label internal traffic without ever presenting it as real evidence."""

    value = identity if isinstance(identity, Mapping) else {}
    return {
        "audience_policy_version": AUDIENCE_POLICY_VERSION,
        "audience_provenance": str(
            value.get("identity_provenance") or "internal_unverified"
        )[:40],
        "audience_eligible": False,
        "audience_account_created_at": _datetime(value.get("created_at")),
        "audience_exclusion_reasons": ["non_production_traffic"],
    }


def attest_audience_snapshot(
    secret: bytes,
    snapshot: Mapping[str, Any],
    *,
    tenant_id: str,
    experiment_id: str,
    assignment_id: str,
    subject_digest: str,
) -> str:
    payload = _attestation_payload(
        snapshot,
        tenant_id=tenant_id,
        experiment_id=experiment_id,
        assignment_id=assignment_id,
        subject_digest=subject_digest,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(secret, encoded, hashlib.sha256).hexdigest()


def verify_audience_attestation(
    secret: bytes,
    snapshot: Mapping[str, Any],
    attestation: str,
    *,
    tenant_id: str,
    experiment_id: str,
    assignment_id: str,
    subject_digest: str,
) -> bool:
    declared = str(attestation or "")
    if len(declared) != 64:
        return False
    expected = attest_audience_snapshot(
        secret,
        snapshot,
        tenant_id=tenant_id,
        experiment_id=experiment_id,
        assignment_id=assignment_id,
        subject_digest=subject_digest,
    )
    return hmac.compare_digest(expected, declared)


def _attestation_payload(
    snapshot: Mapping[str, Any],
    *,
    tenant_id: str,
    experiment_id: str,
    assignment_id: str,
    subject_digest: str,
) -> dict[str, Any]:
    created_at = _datetime(snapshot.get("audience_account_created_at"))
    return {
        "tenant_id": str(tenant_id),
        "experiment_id": str(experiment_id),
        "assignment_id": str(assignment_id),
        "subject_digest": str(subject_digest),
        "audience_policy_version": str(
            snapshot.get("audience_policy_version") or ""
        ),
        "audience_provenance": str(snapshot.get("audience_provenance") or ""),
        "audience_eligible": snapshot.get("audience_eligible") is True,
        "audience_account_created_at": (
            created_at.isoformat() if created_at is not None else None
        ),
    }


def _roles(value: Any) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)
