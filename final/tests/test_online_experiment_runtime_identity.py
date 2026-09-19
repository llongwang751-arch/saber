from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from config.config import APIConfig
from internal.experimentation.runtime import RUNTIME_COMPILER_VERSION
from internal.experimentation.runtime_identity import (
    ANALYSIS_PLAN_VERSION,
    COMPONENT_MANIFEST_PATH_ENV,
    COMPONENT_MANIFEST_SCHEMA_VERSION,
    COMPONENT_MANIFEST_SHA256_ENV,
    DeploymentComponentEvidence,
    RuntimeIdentity,
    RuntimeIdentityError,
    canonical_component_manifest_json,
    component_manifest_from_runtime,
    component_manifest_sha256,
    establish_production_runtime_identity,
    load_verified_component_manifest,
    production_runtime_identity_from_environment,
)


def _config() -> APIConfig:
    config = APIConfig()
    config.llm_model = "qwen-max-2026-09"
    config.embedding_model = "bge-m3-2026-08"
    config.top_k = 3
    config.rag_no_answer_threshold = 0.30
    config.rag_rerank_enabled = True
    config.rag_rerank_fallback_mode = "cross_encoder"
    config.rag_rerank_cross_encoder_model = "bge-reranker-v2-m3@sha256:abcd"
    return config


def _evidence() -> DeploymentComponentEvidence:
    return DeploymentComponentEvidence(
        corpus_version="pig-farm-kb-2026-09-14",
        corpus_sha256="1" * 64,
        index_version="milvus-pig-farm-42",
        index_sha256="2" * 64,
        application_build_sha="3" * 64,
        git_commit_sha="4" * 40,
        image_digest="sha256:" + "5" * 64,
    )


def _write_manifest(
    path: Path,
    manifest: dict,
    *,
    pretty: bool = False,
) -> str:
    if pretty:
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    else:
        payload = canonical_component_manifest_json(manifest).encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _read_only(path: Path) -> None:
    path.chmod(stat.S_IREAD)


def _writable(path: Path) -> None:
    path.chmod(stat.S_IREAD | stat.S_IWRITE)


def _identity(path: Path, config: APIConfig | None = None):
    selected_config = config or _config()
    manifest = component_manifest_from_runtime(selected_config, _evidence())
    artifact_sha = _write_manifest(path, manifest)
    _read_only(path)
    try:
        return establish_production_runtime_identity(
            selected_config,
            _evidence(),
            component_manifest_path=path,
            expected_artifact_sha256=artifact_sha,
        )
    finally:
        _writable(path)


def test_manifest_covers_code_config_models_knowledge_and_build_evidence():
    manifest = component_manifest_from_runtime(_config(), _evidence())

    assert manifest["schema_version"] == COMPONENT_MANIFEST_SCHEMA_VERSION
    assert manifest["runtime_compiler"]["version"] == RUNTIME_COMPILER_VERSION
    assert len(manifest["runtime_compiler"]["artifact_sha256"]) == 64
    assert manifest["analysis_plan"]["version"] == ANALYSIS_PLAN_VERSION
    assert len(manifest["analysis_plan"]["artifact_sha256"]) == 64
    assert manifest["rag"] == {"top_k": 3, "no_answer_threshold": 0.3}
    assert manifest["models"]["llm"] == "qwen-max-2026-09"
    assert manifest["models"]["embedding"] == "bge-m3-2026-08"
    assert manifest["models"]["reranker"].startswith("llm_reranker:qwen-max")
    assert manifest["knowledge"]["corpus_version"] == "pig-farm-kb-2026-09-14"
    assert manifest["knowledge"]["index_sha256"] == "2" * 64
    assert manifest["application"] == {
        "build_sha": "3" * 64,
        "git_commit_sha": "4" * 40,
        "image_digest": "sha256:" + "5" * 64,
    }


def test_canonical_component_digest_is_independent_of_mapping_order():
    manifest = component_manifest_from_runtime(_config(), _evidence())
    reversed_manifest = dict(reversed(list(manifest.items())))

    assert component_manifest_sha256(reversed_manifest) == component_manifest_sha256(
        manifest
    )


def test_production_identity_is_derived_and_deterministic(tmp_path: Path):
    first = _identity(tmp_path / "component-manifest-1.json")
    second = _identity(tmp_path / "component-manifest-2.json")

    assert first.production_verified is True
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert first.fingerprint not in {
        first.component_manifest_artifact_sha256,
        first.component_manifest_payload_sha256,
    }


def test_callers_cannot_self_report_the_production_verified_flag():
    forged = RuntimeIdentity(
        fingerprint="f" * 64,
        schema_version="agi-runtime-identity-v1",
        component_manifest_artifact_sha256="a" * 64,
        component_manifest_payload_sha256="b" * 64,
        component_manifest_path="/not/verified.json",
        canonical_identity_json="{}",
        canonical_component_manifest_json="{}",
        _verification_marker=object(),
    )

    assert forged.production_verified is False


def test_json_formatting_does_not_change_semantic_runtime_identity(tmp_path: Path):
    config = _config()
    evidence = _evidence()
    manifest = component_manifest_from_runtime(config, evidence)
    identities = []
    for name, pretty in (("compact.json", False), ("pretty.json", True)):
        path = tmp_path / name
        artifact_sha = _write_manifest(path, manifest, pretty=pretty)
        _read_only(path)
        try:
            identities.append(
                establish_production_runtime_identity(
                    config,
                    evidence,
                    component_manifest_path=path,
                    expected_artifact_sha256=artifact_sha,
                )
            )
        finally:
            _writable(path)

    assert identities[0].component_manifest_artifact_sha256 != identities[1].component_manifest_artifact_sha256
    assert identities[0].fingerprint == identities[1].fingerprint


def test_writable_manifest_is_not_production_evidence(tmp_path: Path):
    path = tmp_path / "writable.json"
    artifact_sha = _write_manifest(
        path, component_manifest_from_runtime(_config(), _evidence())
    )

    with pytest.raises(RuntimeIdentityError) as caught:
        load_verified_component_manifest(path, artifact_sha)

    assert caught.value.code == "component_manifest_writable"


def test_manifest_checksum_must_match_separately_injected_value(tmp_path: Path):
    path = tmp_path / "component-manifest.json"
    _write_manifest(path, component_manifest_from_runtime(_config(), _evidence()))
    _read_only(path)
    try:
        with pytest.raises(RuntimeIdentityError) as caught:
            load_verified_component_manifest(path, "f" * 64)
    finally:
        _writable(path)

    assert caught.value.code == "component_manifest_checksum_mismatch"


def test_active_config_drift_cannot_reuse_old_manifest_fingerprint(tmp_path: Path):
    original = _config()
    changed = _config()
    changed.top_k = 9
    path = tmp_path / "component-manifest.json"
    artifact_sha = _write_manifest(
        path, component_manifest_from_runtime(original, _evidence())
    )
    _read_only(path)
    try:
        with pytest.raises(RuntimeIdentityError) as caught:
            establish_production_runtime_identity(
                changed,
                _evidence(),
                component_manifest_path=path,
                expected_artifact_sha256=artifact_sha,
            )
    finally:
        _writable(path)

    assert caught.value.code == "component_manifest_runtime_mismatch"


def test_deployment_or_knowledge_drift_cannot_reuse_manifest(tmp_path: Path):
    config = _config()
    path = tmp_path / "component-manifest.json"
    artifact_sha = _write_manifest(
        path, component_manifest_from_runtime(config, _evidence())
    )
    changed = DeploymentComponentEvidence(
        **{**_evidence().__dict__, "index_sha256": "a" * 64}
    )
    _read_only(path)
    try:
        with pytest.raises(RuntimeIdentityError) as caught:
            establish_production_runtime_identity(
                config,
                changed,
                component_manifest_path=path,
                expected_artifact_sha256=artifact_sha,
            )
    finally:
        _writable(path)

    assert caught.value.code == "component_manifest_runtime_mismatch"


def test_duplicate_json_keys_are_rejected_even_with_matching_artifact_hash(tmp_path: Path):
    manifest = component_manifest_from_runtime(_config(), _evidence())
    compact = canonical_component_manifest_json(manifest)
    payload = (
        compact[:-1]
        + ',"schema_version":"agi-runtime-component-manifest-v1"}'
    ).encode("utf-8")
    path = tmp_path / "duplicate.json"
    path.write_bytes(payload)
    _read_only(path)
    try:
        with pytest.raises(RuntimeIdentityError) as caught:
            load_verified_component_manifest(path, hashlib.sha256(payload).hexdigest())
    finally:
        _writable(path)

    assert caught.value.code == "component_manifest_duplicate_key"


def test_manifest_shape_is_strict_and_cannot_hide_unbound_components():
    manifest = component_manifest_from_runtime(_config(), _evidence())
    manifest["models"]["secret_model_override"] = "unbound"

    with pytest.raises(RuntimeIdentityError) as caught:
        canonical_component_manifest_json(manifest)

    assert caught.value.code == "component_manifest_shape_invalid"


def test_disabled_reranker_has_an_explicit_identity():
    config = _config()
    config.rag_rerank_enabled = False

    manifest = component_manifest_from_runtime(config, _evidence())

    assert manifest["models"]["reranker"] == "disabled"


def test_environment_entrypoint_requires_every_deployment_evidence_field():
    with pytest.raises(RuntimeIdentityError) as caught:
        production_runtime_identity_from_environment(_config(), {})

    assert caught.value.code == "deployment_evidence_missing"
    assert COMPONENT_MANIFEST_PATH_ENV in str(caught.value)


def test_environment_entrypoint_verifies_manifest_instead_of_accepting_a_hash(
    tmp_path: Path,
):
    config = _config()
    evidence = _evidence()
    manifest = component_manifest_from_runtime(config, evidence)
    path = tmp_path / "component-manifest.json"
    artifact_sha = _write_manifest(path, manifest)
    environment = {
        COMPONENT_MANIFEST_PATH_ENV: str(path),
        COMPONENT_MANIFEST_SHA256_ENV: artifact_sha,
        "AGI_RAG_CORPUS_VERSION": evidence.corpus_version,
        "AGI_RAG_CORPUS_SHA256": evidence.corpus_sha256,
        "AGI_RAG_INDEX_VERSION": evidence.index_version,
        "AGI_RAG_INDEX_SHA256": evidence.index_sha256,
        "AGI_APPLICATION_BUILD_SHA": evidence.application_build_sha,
        "AGI_GIT_COMMIT_SHA": evidence.git_commit_sha,
        "AGI_IMAGE_DIGEST": evidence.image_digest,
        # The legacy self-reported value is intentionally ignored.
        "AGI_EXPERIMENT_RUNTIME_ENVIRONMENT_FINGERPRINT": "a" * 64,
    }
    _read_only(path)
    try:
        identity = production_runtime_identity_from_environment(config, environment)
        environment["AGI_EXPERIMENT_RUNTIME_ENVIRONMENT_FINGERPRINT"] = "b" * 64
        repeated = production_runtime_identity_from_environment(config, environment)
    finally:
        _writable(path)

    assert identity.production_verified is True
    assert json.loads(identity.canonical_identity_json)["components"] == manifest
    assert repeated.fingerprint == identity.fingerprint
    assert identity.fingerprint not in {"a" * 64, "b" * 64}


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks require optional privilege")
def test_symlink_manifest_is_rejected(tmp_path: Path):
    target = tmp_path / "target.json"
    artifact_sha = _write_manifest(
        target, component_manifest_from_runtime(_config(), _evidence())
    )
    _read_only(target)
    link = tmp_path / "manifest.json"
    link.symlink_to(target)
    try:
        with pytest.raises(RuntimeIdentityError) as caught:
            load_verified_component_manifest(link, artifact_sha)
    finally:
        _writable(target)

    assert caught.value.code == "component_manifest_symlink_forbidden"
