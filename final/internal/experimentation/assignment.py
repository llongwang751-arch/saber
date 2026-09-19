"""Deterministic, privacy-preserving traffic assignment.

Assignment uses only an HMAC digest.  No process-local randomness or Python
hashing is involved, so the result survives restarts and is identical across
workers without storing a user-to-arm lookup table.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal

from .schemas import Allocation


_PREFIX = b"agi-exp-v1\x00"
_USER_UNIT = b"\x00user\x00"


def canonical_bucket_input(
    tenant_id: str,
    experiment_id: str,
    user_id: str,
) -> bytes:
    """Build the byte-exact v1 assignment message.

    The format is::

        b"agi-exp-v1\\0" + tenant + b"\\0" + experiment
        + b"\\0user\\0" + user_id

    ``user`` is the literal assignment-unit label.  Identifiers are encoded as
    UTF-8 without trimming or case conversion.  Embedded NUL bytes are rejected
    so distinct tuples cannot collapse to the same message.
    """

    tenant = _identifier_bytes(tenant_id, "tenant_id")
    experiment = _identifier_bytes(experiment_id, "experiment_id")
    user = _identifier_bytes(user_id, "user_id")
    return _PREFIX + tenant + b"\x00" + experiment + _USER_UNIT + user


def stable_buckets(
    secret: str | bytes,
    tenant_id: str,
    experiment_id: str,
    user_id: str,
) -> dict[str, int]:
    """Return independent stable enrollment and variant buckets in ``0..9999``.

    Both buckets come from one HMAC-SHA256 digest.  The enrollment bucket uses
    bytes ``0:8`` and the variant bucket bytes ``8:16``, interpreted as unsigned
    big-endian integers before reducing modulo 10,000.
    """

    digest = _assignment_digest(secret, tenant_id, experiment_id, user_id)
    return {
        "enrollment_bucket": int.from_bytes(digest[:8], "big") % 10_000,
        "variant_bucket": int.from_bytes(digest[8:16], "big") % 10_000,
    }


def stable_bucket(
    secret: str | bytes,
    tenant_id: str,
    experiment_id: str,
    user_id: str,
) -> int:
    """Return the stable enrollment bucket (the historical single-bucket API)."""

    return stable_buckets(secret, tenant_id, experiment_id, user_id)[
        "enrollment_bucket"
    ]


def select_arm(
    bucket: int,
    candidate_allocation_bps: int,
) -> Literal["control", "candidate"]:
    """Map a variant bucket to the candidate or control arm.

    Allocation is half-open: for ``candidate_allocation_bps=2500``, buckets
    ``0..2499`` are candidate and ``2500..9999`` are control.  Zero and 10,000
    are accepted by this low-level helper for operational off/full switches;
    experiment creation itself requires both arms to receive traffic.
    """

    bucket = _bounded_int(bucket, "bucket", 0, 9_999)
    allocation = _bounded_int(
        candidate_allocation_bps,
        "candidate_allocation_bps",
        0,
        10_000,
    )
    return "candidate" if bucket < allocation else "control"


def assign_variant(
    secret: str | bytes,
    tenant_id: str,
    experiment_id: str,
    user_id: str,
    candidate_allocation_bps: int,
) -> Allocation:
    """Return the typed arm assignment using the independent variant bucket."""

    buckets = stable_buckets(secret, tenant_id, experiment_id, user_id)
    variant_bucket = buckets["variant_bucket"]
    return Allocation(
        bucket=variant_bucket,
        arm=select_arm(variant_bucket, candidate_allocation_bps),
        candidate_allocation_bps=candidate_allocation_bps,
    )


# A concise alias for callers that treat assignment as a domain operation.
allocate = assign_variant


def _assignment_digest(
    secret: str | bytes,
    tenant_id: str,
    experiment_id: str,
    user_id: str,
) -> bytes:
    key = _secret_bytes(secret)
    message = canonical_bucket_input(tenant_id, experiment_id, user_id)
    return hmac.new(key, message, hashlib.sha256).digest()


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        value = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        value = secret
    else:
        raise TypeError("secret must be str or bytes")
    if not value:
        raise ValueError("secret must not be empty")
    return value


def _identifier_bytes(value: str, name: str) -> bytes:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
    encoded = value.encode("utf-8")
    if b"\x00" in encoded:
        raise ValueError(f"{name} must not contain NUL")
    return encoded


def _bounded_int(value: int, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")
    return value


__all__ = [
    "allocate",
    "assign_variant",
    "canonical_bucket_input",
    "select_arm",
    "stable_bucket",
    "stable_buckets",
]
