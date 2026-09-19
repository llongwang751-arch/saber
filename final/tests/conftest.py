from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from config.config import APIConfig
from internal.experimentation.runtime_identity import (
    DeploymentComponentEvidence,
    canonical_component_manifest_json,
    component_manifest_from_runtime,
    establish_production_runtime_identity,
)


@pytest.fixture()
def runtime_identity_factory(tmp_path: Path):
    """Mint genuine verifier-owned identities for production-gate tests."""

    counter = 0

    def build(label: str = "primary"):
        nonlocal counter
        counter += 1
        label_digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
        config = APIConfig()
        config.llm_model = "qwen-test-verified"
        config.embedding_model = "bge-m3-test-verified"
        config.top_k = 3
        config.rag_no_answer_threshold = 0.30
        config.rag_rerank_enabled = False
        evidence = DeploymentComponentEvidence(
            corpus_version=f"test-corpus-{label_digest[:12]}",
            corpus_sha256=label_digest,
            index_version=f"test-index-{label_digest[:12]}",
            index_sha256=hashlib.sha256(
                f"index:{label}".encode("utf-8")
            ).hexdigest(),
            application_build_sha=hashlib.sha256(
                f"build:{label}".encode("utf-8")
            ).hexdigest(),
            git_commit_sha=hashlib.sha1(
                f"git:{label}".encode("utf-8"), usedforsecurity=False
            ).hexdigest(),
            image_digest="sha256:"
            + hashlib.sha256(f"image:{label}".encode("utf-8")).hexdigest(),
        )
        manifest = component_manifest_from_runtime(config, evidence)
        payload = canonical_component_manifest_json(manifest).encode("utf-8")
        path = (tmp_path / f"runtime-components-{counter}.json").resolve()
        path.write_bytes(payload)
        path.chmod(stat.S_IREAD)
        try:
            return establish_production_runtime_identity(
                config,
                evidence,
                component_manifest_path=path,
                expected_artifact_sha256=hashlib.sha256(payload).hexdigest(),
            )
        finally:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)

    return build
