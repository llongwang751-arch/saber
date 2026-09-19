"""Verified, reproducible identity for the online RAG runtime.

The production experiment gate must not trust a caller-provided 64-character
string.  This module derives the identity from the active runtime
configuration and from a separately checksummed, read-only component manifest
created by the deployment pipeline.

No secret values, endpoints, tenant identifiers, or raw corpus contents are
included.  Knowledge and application artifacts are represented by versioned
content digests instead.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runtime import RUNTIME_COMPILER_VERSION


RUNTIME_IDENTITY_SCHEMA_VERSION = "agi-runtime-identity-v1"
COMPONENT_MANIFEST_SCHEMA_VERSION = "agi-runtime-component-manifest-v1"
ANALYSIS_PLAN_VERSION = "online-rag-itt-first-feedback-fixed-horizon-v1"
MAX_COMPONENT_MANIFEST_BYTES = 64 * 1024

COMPONENT_MANIFEST_PATH_ENV = "AGI_EXPERIMENT_COMPONENT_MANIFEST_PATH"
COMPONENT_MANIFEST_SHA256_ENV = "AGI_EXPERIMENT_COMPONENT_MANIFEST_SHA256"
CORPUS_VERSION_ENV = "AGI_RAG_CORPUS_VERSION"
CORPUS_SHA256_ENV = "AGI_RAG_CORPUS_SHA256"
INDEX_VERSION_ENV = "AGI_RAG_INDEX_VERSION"
INDEX_SHA256_ENV = "AGI_RAG_INDEX_SHA256"
APPLICATION_BUILD_SHA_ENV = "AGI_APPLICATION_BUILD_SHA"
GIT_COMMIT_SHA_ENV = "AGI_GIT_COMMIT_SHA"
IMAGE_DIGEST_ENV = "AGI_IMAGE_DIGEST"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_VERIFIED_RUNTIME_IDENTITY_MARKER = object()
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "runtime_compiler",
        "analysis_plan",
        "rag",
        "models",
        "knowledge",
        "application",
    }
)


class RuntimeIdentityError(ValueError):
    """A stable, fail-closed production runtime identity error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeploymentComponentEvidence:
    """Non-secret artifact evidence supplied by the deployment pipeline."""

    corpus_version: str
    corpus_sha256: str
    index_version: str
    index_sha256: str
    application_build_sha: str
    git_commit_sha: str
    image_digest: str

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "DeploymentComponentEvidence":
        values = os.environ if environ is None else environ
        return cls(
            corpus_version=_environment_value(values, CORPUS_VERSION_ENV),
            corpus_sha256=_environment_value(values, CORPUS_SHA256_ENV),
            index_version=_environment_value(values, INDEX_VERSION_ENV),
            index_sha256=_environment_value(values, INDEX_SHA256_ENV),
            application_build_sha=_environment_value(
                values, APPLICATION_BUILD_SHA_ENV
            ),
            git_commit_sha=_environment_value(values, GIT_COMMIT_SHA_ENV),
            image_digest=_environment_value(values, IMAGE_DIGEST_ENV),
        )


@dataclass(frozen=True)
class VerifiedComponentManifest:
    """A stable read of a deployment artifact and both of its digests."""

    source_path: str
    artifact_sha256: str
    payload_sha256: str
    canonical_json: str

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)


@dataclass(frozen=True)
class RuntimeIdentity:
    """Evidence object that a production gate can accept instead of a string."""

    fingerprint: str
    schema_version: str
    component_manifest_artifact_sha256: str
    component_manifest_payload_sha256: str
    component_manifest_path: str
    canonical_identity_json: str
    canonical_component_manifest_json: str
    _verification_marker: object = field(repr=False, compare=False)

    @property
    def production_verified(self) -> bool:
        """True only for objects minted by the complete verifier."""

        return self._verification_marker is _VERIFIED_RUNTIME_IDENTITY_MARKER


def component_manifest_from_runtime(
    config: Any,
    evidence: DeploymentComponentEvidence,
    *,
    runtime_compiler_version: str = RUNTIME_COMPILER_VERSION,
    analysis_plan_version: str = ANALYSIS_PLAN_VERSION,
) -> dict[str, Any]:
    """Observe code, active config and deployment evidence as one manifest.

    This function deliberately has no ``fingerprint`` input.  The compiler and
    analysis artifact hashes are calculated from the files that are actually
    present in this application build.  Model and RAG values come from the
    active configuration object, while corpus/index/build evidence comes from
    the deployment or indexing pipeline.
    """

    top_k = _strict_int(_config_value(config, "top_k"), "rag.top_k", 1, 20)
    threshold = _finite_number(
        _config_value(config, "rag_no_answer_threshold"),
        "rag.no_answer_threshold",
        0.0,
        1.0,
    )
    llm_model = _identifier(_config_value(config, "llm_model"), "models.llm")
    embedding_model = _identifier(
        _config_value(config, "embedding_model"), "models.embedding"
    )
    reranker_model = _reranker_identifier(config, llm_model)
    manifest = {
        "schema_version": COMPONENT_MANIFEST_SCHEMA_VERSION,
        "runtime_compiler": {
            "version": _identifier(
                runtime_compiler_version, "runtime_compiler.version"
            ),
            "artifact_sha256": _artifact_bundle_sha256(("runtime.py",)),
        },
        "analysis_plan": {
            "version": _identifier(
                analysis_plan_version, "analysis_plan.version"
            ),
            "artifact_sha256": _artifact_bundle_sha256(
                ("service.py", "statistics.py")
            ),
        },
        "rag": {
            "top_k": top_k,
            "no_answer_threshold": threshold,
        },
        "models": {
            "llm": llm_model,
            "embedding": embedding_model,
            "reranker": reranker_model,
        },
        "knowledge": {
            "corpus_version": _identifier(
                evidence.corpus_version, "knowledge.corpus_version"
            ),
            "corpus_sha256": _sha256(
                evidence.corpus_sha256, "knowledge.corpus_sha256"
            ),
            "index_version": _identifier(
                evidence.index_version, "knowledge.index_version"
            ),
            "index_sha256": _sha256(
                evidence.index_sha256, "knowledge.index_sha256"
            ),
        },
        "application": {
            "build_sha": _sha256(
                evidence.application_build_sha, "application.build_sha"
            ),
            "git_commit_sha": _git_sha(
                evidence.git_commit_sha, "application.git_commit_sha"
            ),
            "image_digest": _image_digest(evidence.image_digest),
        },
    }
    # Run the same strict validation used when loading a file.  This prevents a
    # build-time manifest generator and the runtime verifier from drifting.
    return _normalize_component_manifest(manifest)


def canonical_component_manifest_json(manifest: Mapping[str, Any]) -> str:
    """Return the strict canonical JSON used for semantic manifest equality."""

    value = _normalize_component_manifest(manifest)
    return _canonical_json(value)


def component_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the canonical component manifest payload."""

    return hashlib.sha256(
        canonical_component_manifest_json(manifest).encode("utf-8")
    ).hexdigest()


def load_verified_component_manifest(
    path: str | os.PathLike[str],
    expected_artifact_sha256: str,
    *,
    require_read_only: bool = True,
    max_bytes: int = MAX_COMPONENT_MANIFEST_BYTES,
) -> VerifiedComponentManifest:
    """Load one immutable manifest file and verify its deployment digest.

    The expected digest is intentionally external to the file; in production
    it must be injected by the deployment controller.  Symlinks, writable
    files, oversized payloads, duplicate JSON keys and a file replaced during
    the read are rejected.
    """

    source = Path(path)
    if not source.is_absolute():
        raise RuntimeIdentityError(
            "component_manifest_path_not_absolute",
            "production component manifest path must be absolute",
        )
    declared_sha = _sha256(
        expected_artifact_sha256, "component_manifest_artifact_sha256"
    )
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise RuntimeIdentityError(
            "component_manifest_size_limit_invalid",
            "component manifest size limit must be a positive integer",
        )
    try:
        before = source.lstat()
    except OSError as exc:
        raise RuntimeIdentityError(
            "component_manifest_unavailable",
            f"production component manifest is unavailable: {source}",
        ) from exc
    if stat.S_ISLNK(before.st_mode):
        raise RuntimeIdentityError(
            "component_manifest_symlink_forbidden",
            "production component manifest must not be a symlink",
        )
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeIdentityError(
            "component_manifest_not_regular_file",
            "production component manifest must be a regular file",
        )
    if require_read_only and before.st_mode & _WRITE_BITS:
        raise RuntimeIdentityError(
            "component_manifest_writable",
            "production component manifest must have no filesystem write bits",
        )
    if before.st_size > max_bytes:
        raise RuntimeIdentityError(
            "component_manifest_too_large",
            f"component manifest exceeds {max_bytes} bytes",
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise RuntimeIdentityError(
            "component_manifest_open_failed",
            "production component manifest could not be opened safely",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not _same_file_snapshot(before, opened):
            raise RuntimeIdentityError(
                "component_manifest_changed_during_open",
                "production component manifest changed while it was opened",
            )
        payload = _read_bounded(descriptor, max_bytes)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = source.lstat()
    except OSError as exc:
        raise RuntimeIdentityError(
            "component_manifest_changed_during_read",
            "production component manifest disappeared during verification",
        ) from exc
    if not (
        _same_file_snapshot(opened, after_fd)
        and _same_file_snapshot(after_fd, after_path)
    ):
        raise RuntimeIdentityError(
            "component_manifest_changed_during_read",
            "production component manifest changed during verification",
        )
    artifact_sha = hashlib.sha256(payload).hexdigest()
    if not _constant_time_equal(artifact_sha, declared_sha):
        raise RuntimeIdentityError(
            "component_manifest_checksum_mismatch",
            "production component manifest does not match the deployment checksum",
        )
    manifest = _decode_component_manifest(payload)
    canonical = canonical_component_manifest_json(manifest)
    return VerifiedComponentManifest(
        source_path=str(source.resolve()),
        artifact_sha256=artifact_sha,
        payload_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        canonical_json=canonical,
    )


def establish_production_runtime_identity(
    config: Any,
    evidence: DeploymentComponentEvidence,
    *,
    component_manifest_path: str | os.PathLike[str],
    expected_artifact_sha256: str,
) -> RuntimeIdentity:
    """Verify the deployment manifest against this process and derive identity.

    A valid result proves all of the following together: the artifact bytes
    match the separately injected digest; the file is read-only and was stable
    while read; its strict component payload equals the active config, code
    artifacts and deployment evidence; and the final fingerprint was derived
    here from that evidence.
    """

    verified = load_verified_component_manifest(
        component_manifest_path,
        expected_artifact_sha256,
        require_read_only=True,
    )
    observed = component_manifest_from_runtime(config, evidence)
    observed_json = canonical_component_manifest_json(observed)
    if not _constant_time_equal(observed_json, verified.canonical_json):
        raise RuntimeIdentityError(
            "component_manifest_runtime_mismatch",
            "deployment component manifest does not match the active runtime",
        )
    identity_payload = {
        "schema_version": RUNTIME_IDENTITY_SCHEMA_VERSION,
        "component_manifest_payload_sha256": verified.payload_sha256,
        "components": observed,
    }
    canonical_identity = _canonical_json(identity_payload)
    fingerprint = hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()
    return RuntimeIdentity(
        fingerprint=fingerprint,
        schema_version=RUNTIME_IDENTITY_SCHEMA_VERSION,
        component_manifest_artifact_sha256=verified.artifact_sha256,
        component_manifest_payload_sha256=verified.payload_sha256,
        component_manifest_path=verified.source_path,
        canonical_identity_json=canonical_identity,
        canonical_component_manifest_json=observed_json,
        _verification_marker=_VERIFIED_RUNTIME_IDENTITY_MARKER,
    )


def production_runtime_identity_from_environment(
    config: Any,
    environ: Mapping[str, str] | None = None,
) -> RuntimeIdentity:
    """Production integration entry point using deployment-owned variables."""

    values = os.environ if environ is None else environ
    manifest_path = _environment_value(values, COMPONENT_MANIFEST_PATH_ENV)
    manifest_sha = _environment_value(values, COMPONENT_MANIFEST_SHA256_ENV)
    evidence = DeploymentComponentEvidence.from_environment(values)
    return establish_production_runtime_identity(
        config,
        evidence,
        component_manifest_path=manifest_path,
        expected_artifact_sha256=manifest_sha,
    )


def _normalize_component_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise RuntimeIdentityError(
            "component_manifest_invalid", "component manifest must be a JSON object"
        )
    _exact_keys(manifest, _ROOT_KEYS, "component manifest")
    if manifest.get("schema_version") != COMPONENT_MANIFEST_SCHEMA_VERSION:
        raise RuntimeIdentityError(
            "component_manifest_schema_unsupported",
            "component manifest schema version is unsupported",
        )
    compiler = _object(manifest.get("runtime_compiler"), "runtime_compiler")
    analysis = _object(manifest.get("analysis_plan"), "analysis_plan")
    rag = _object(manifest.get("rag"), "rag")
    models = _object(manifest.get("models"), "models")
    knowledge = _object(manifest.get("knowledge"), "knowledge")
    application = _object(manifest.get("application"), "application")
    _exact_keys(compiler, {"version", "artifact_sha256"}, "runtime_compiler")
    _exact_keys(analysis, {"version", "artifact_sha256"}, "analysis_plan")
    _exact_keys(rag, {"top_k", "no_answer_threshold"}, "rag")
    _exact_keys(models, {"llm", "embedding", "reranker"}, "models")
    _exact_keys(
        knowledge,
        {"corpus_version", "corpus_sha256", "index_version", "index_sha256"},
        "knowledge",
    )
    _exact_keys(
        application,
        {"build_sha", "git_commit_sha", "image_digest"},
        "application",
    )
    return {
        "schema_version": COMPONENT_MANIFEST_SCHEMA_VERSION,
        "runtime_compiler": {
            "version": _identifier(compiler.get("version"), "runtime_compiler.version"),
            "artifact_sha256": _sha256(
                compiler.get("artifact_sha256"),
                "runtime_compiler.artifact_sha256",
            ),
        },
        "analysis_plan": {
            "version": _identifier(analysis.get("version"), "analysis_plan.version"),
            "artifact_sha256": _sha256(
                analysis.get("artifact_sha256"), "analysis_plan.artifact_sha256"
            ),
        },
        "rag": {
            "top_k": _strict_int(rag.get("top_k"), "rag.top_k", 1, 20),
            "no_answer_threshold": _finite_number(
                rag.get("no_answer_threshold"),
                "rag.no_answer_threshold",
                0.0,
                1.0,
            ),
        },
        "models": {
            "llm": _identifier(models.get("llm"), "models.llm"),
            "embedding": _identifier(models.get("embedding"), "models.embedding"),
            "reranker": _identifier(models.get("reranker"), "models.reranker"),
        },
        "knowledge": {
            "corpus_version": _identifier(
                knowledge.get("corpus_version"), "knowledge.corpus_version"
            ),
            "corpus_sha256": _sha256(
                knowledge.get("corpus_sha256"), "knowledge.corpus_sha256"
            ),
            "index_version": _identifier(
                knowledge.get("index_version"), "knowledge.index_version"
            ),
            "index_sha256": _sha256(
                knowledge.get("index_sha256"), "knowledge.index_sha256"
            ),
        },
        "application": {
            "build_sha": _sha256(
                application.get("build_sha"), "application.build_sha"
            ),
            "git_commit_sha": _git_sha(
                application.get("git_commit_sha"), "application.git_commit_sha"
            ),
            "image_digest": _image_digest(application.get("image_digest")),
        },
    }


def _decode_component_manifest(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeIdentityError(
            "component_manifest_not_utf8", "component manifest must be UTF-8 JSON"
        ) from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except RuntimeIdentityError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeIdentityError(
            "component_manifest_json_invalid", "component manifest is invalid JSON"
        ) from exc
    return _normalize_component_manifest(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeIdentityError(
                "component_manifest_duplicate_key",
                f"component manifest contains duplicate key: {key}",
            )
        value[key] = item
    return value


def _read_bounded(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > max_bytes:
        raise RuntimeIdentityError(
            "component_manifest_too_large",
            f"component manifest exceeds {max_bytes} bytes",
        )
    return value


def _artifact_bundle_sha256(names: tuple[str, ...]) -> str:
    base = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in names:
        path = base / name
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RuntimeIdentityError(
                "runtime_artifact_unavailable",
                f"runtime artifact is unavailable: {name}",
            ) from exc
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _reranker_identifier(config: Any, llm_model: str) -> str:
    enabled = _config_value(config, "rag_rerank_enabled")
    if not isinstance(enabled, bool):
        raise RuntimeIdentityError(
            "runtime_config_invalid",
            "runtime config rag_rerank_enabled must be boolean",
        )
    if not enabled:
        return "disabled"
    fallback = _identifier(
        _config_value(config, "rag_rerank_fallback_mode"),
        "models.reranker_fallback",
    )
    if fallback not in {"rrf", "local_overlap", "cross_encoder"}:
        raise RuntimeIdentityError(
            "runtime_config_invalid", "unsupported RAG reranker fallback mode"
        )
    if fallback == "cross_encoder":
        fallback_model = _identifier(
            _config_value(config, "rag_rerank_cross_encoder_model"),
            "models.reranker_cross_encoder",
        )
        fallback_identity = f"cross_encoder:{fallback_model}"
    elif fallback == "local_overlap":
        fallback_identity = "local_overlap:v1"
    else:
        fallback_identity = "rrf:v1"
    return f"llm_reranker:{llm_model};fallback={fallback_identity}"


def _config_value(config: Any, name: str) -> Any:
    if not hasattr(config, name):
        raise RuntimeIdentityError(
            "runtime_config_missing", f"runtime config is missing {name}"
        )
    return getattr(config, name)


def _environment_value(values: Mapping[str, str], name: str) -> str:
    raw = values.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeIdentityError(
            "deployment_evidence_missing", f"deployment variable {name} is required"
        )
    return raw.strip()


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeIdentityError(
            "component_manifest_invalid", f"component manifest {name} must be an object"
        )
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str] | frozenset[str], name: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"extra={','.join(extra)}")
        raise RuntimeIdentityError(
            "component_manifest_shape_invalid",
            f"component manifest {name} has invalid fields ({'; '.join(details)})",
        )


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeIdentityError(
            "component_identifier_invalid", f"{name} must be a string"
        )
    result = value.strip()
    if not result or len(result) > 300 or any(ord(char) < 32 for char in result):
        raise RuntimeIdentityError(
            "component_identifier_invalid", f"{name} is not a valid identifier"
        )
    return result


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value.strip().lower()) is None:
        raise RuntimeIdentityError(
            "component_digest_invalid", f"{name} must be a SHA-256 hex digest"
        )
    return value.strip().lower()


def _git_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value.strip().lower()) is None:
        raise RuntimeIdentityError(
            "component_digest_invalid", f"{name} must be a full Git SHA"
        )
    return value.strip().lower()


def _image_digest(value: Any) -> str:
    if not isinstance(value, str) or _IMAGE_DIGEST_RE.fullmatch(value.strip().lower()) is None:
        raise RuntimeIdentityError(
            "component_digest_invalid",
            "application.image_digest must be a sha256:<digest> image identity",
        )
    return value.strip().lower()


def _strict_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RuntimeIdentityError(
            "runtime_config_invalid",
            f"{name} must be an integer between {minimum} and {maximum}",
        )
    return value


def _finite_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeIdentityError(
            "runtime_config_invalid", f"{name} must be numeric"
        )
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise RuntimeIdentityError(
            "runtime_config_invalid",
            f"{name} must be finite and between {minimum} and {maximum}",
        )
    return 0.0 if result == 0.0 else result


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeIdentityError(
            "component_manifest_not_canonicalizable",
            "component manifest cannot be represented as strict JSON",
        ) from exc


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    return all(getattr(left, field, None) == getattr(right, field, None) for field in fields)


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


__all__ = [
    "ANALYSIS_PLAN_VERSION",
    "APPLICATION_BUILD_SHA_ENV",
    "COMPONENT_MANIFEST_PATH_ENV",
    "COMPONENT_MANIFEST_SCHEMA_VERSION",
    "COMPONENT_MANIFEST_SHA256_ENV",
    "CORPUS_SHA256_ENV",
    "CORPUS_VERSION_ENV",
    "DeploymentComponentEvidence",
    "GIT_COMMIT_SHA_ENV",
    "IMAGE_DIGEST_ENV",
    "INDEX_SHA256_ENV",
    "INDEX_VERSION_ENV",
    "MAX_COMPONENT_MANIFEST_BYTES",
    "RUNTIME_IDENTITY_SCHEMA_VERSION",
    "RuntimeIdentity",
    "RuntimeIdentityError",
    "VerifiedComponentManifest",
    "canonical_component_manifest_json",
    "component_manifest_from_runtime",
    "component_manifest_sha256",
    "establish_production_runtime_identity",
    "load_verified_component_manifest",
    "production_runtime_identity_from_environment",
]
