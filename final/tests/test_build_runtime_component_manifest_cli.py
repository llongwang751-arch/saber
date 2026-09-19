from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from config.config import default_config
from internal.experimentation.runtime_identity import (
    APPLICATION_BUILD_SHA_ENV,
    CORPUS_SHA256_ENV,
    CORPUS_VERSION_ENV,
    GIT_COMMIT_SHA_ENV,
    IMAGE_DIGEST_ENV,
    INDEX_SHA256_ENV,
    INDEX_VERSION_ENV,
    DeploymentComponentEvidence,
    load_verified_component_manifest,
)
from scripts.build_runtime_component_manifest import (
    build_runtime_component_manifest,
    main,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _deployment_environment() -> dict[str, str]:
    return {
        CORPUS_VERSION_ENV: "pig-farm-kb-2026-09-14",
        CORPUS_SHA256_ENV: "1" * 64,
        INDEX_VERSION_ENV: "milvus-pig-farm-42",
        INDEX_SHA256_ENV: "2" * 64,
        APPLICATION_BUILD_SHA_ENV: "3" * 64,
        GIT_COMMIT_SHA_ENV: "4" * 40,
        IMAGE_DIGEST_ENV: "sha256:" + "5" * 64,
    }


def _set_deployment_environment(monkeypatch) -> None:
    for name, value in _deployment_environment().items():
        monkeypatch.setenv(name, value)


def _make_writable(path: Path) -> None:
    if path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_build_function_atomically_publishes_a_verified_read_only_file(
    tmp_path: Path,
):
    config = default_config()
    evidence = DeploymentComponentEvidence(**{
        "corpus_version": "pig-farm-kb-2026-09-14",
        "corpus_sha256": "1" * 64,
        "index_version": "milvus-pig-farm-42",
        "index_sha256": "2" * 64,
        "application_build_sha": "3" * 64,
        "git_commit_sha": "4" * 40,
        "image_digest": "sha256:" + "5" * 64,
    })
    target = tmp_path / "component-manifest.json"
    try:
        identity = build_runtime_component_manifest(config, evidence, target)

        assert target.is_file()
        assert target.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
        verified = load_verified_component_manifest(
            target, identity.component_manifest_artifact_sha256
        )
        assert verified.payload_sha256 == identity.component_manifest_payload_sha256
        assert not list(tmp_path.glob(".component-manifest.json.*.tmp"))
    finally:
        _make_writable(target)


def test_chinese_cli_outputs_only_safe_summary(monkeypatch, tmp_path: Path, capsys):
    _set_deployment_environment(monkeypatch)
    secret = "must-not-appear-in-cli-output"
    monkeypatch.setenv("AGI_LLM_API_KEY", secret)
    target = tmp_path / "component-manifest.json"
    try:
        result = main(["--output", str(target)])
        captured = capsys.readouterr()

        assert result == 0
        assert "运行环境组件清单已生成并通过校验" in captured.out
        assert "文件 SHA-256" in captured.out
        assert "运行环境指纹" in captured.out
        assert secret not in captured.out
        assert secret not in captured.err
        assert target.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    finally:
        _make_writable(target)


def test_json_mode_is_machine_readable_and_secret_free(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    _set_deployment_environment(monkeypatch)
    secret = "another-private-token"
    monkeypatch.setenv("AGI_EMBEDDING_API_KEY", secret)
    target = tmp_path / "component-manifest.json"
    try:
        assert main(["--output", str(target), "--json"]) == 0
        summary = json.loads(capsys.readouterr().out)

        assert summary["status"] == "verified"
        assert summary["production_verified"] is True
        assert len(summary["component_manifest_artifact_sha256"]) == 64
        assert len(summary["runtime_environment_fingerprint"]) == 64
        assert secret not in json.dumps(summary)
        assert set(summary) == {
            "status",
            "component_manifest_path",
            "component_manifest_artifact_sha256",
            "component_manifest_payload_sha256",
            "runtime_environment_fingerprint",
            "production_verified",
        }
    finally:
        _make_writable(target)


def test_cli_refuses_to_overwrite_an_existing_immutable_manifest(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    _set_deployment_environment(monkeypatch)
    target = tmp_path / "component-manifest.json"
    try:
        assert main(["--output", str(target)]) == 0
        original = target.read_bytes()
        capsys.readouterr()

        assert main(["--output", str(target)]) == 2
        captured = capsys.readouterr()
        assert "拒绝覆盖不可变清单" in captured.err
        assert target.read_bytes() == original
    finally:
        _make_writable(target)


def test_cli_fails_closed_when_deployment_evidence_is_missing(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    for name in _deployment_environment():
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "component-manifest.json"

    assert main(["--output", str(target)]) == 2
    captured = capsys.readouterr()

    assert "组件清单生成失败" in captured.err
    assert "deployment variable" in captured.err
    assert not os.path.lexists(target)


def test_cli_script_runs_as_a_real_subprocess(tmp_path: Path):
    target = tmp_path / "component-manifest.json"
    environment = os.environ.copy()
    environment.update(_deployment_environment())
    environment["PYTHONUTF8"] = "1"
    secret = "subprocess-secret-must-not-leak"
    environment["AGI_LLM_API_KEY"] = secret
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "build_runtime_component_manifest.py"),
        "--config",
        str(PROJECT_ROOT / "config" / "config.yaml"),
        "--output",
        str(target),
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        assert completed.returncode == 0, completed.stderr
        summary = json.loads(completed.stdout)
        assert summary["production_verified"] is True
        assert Path(summary["component_manifest_path"]) == target.resolve()
        assert secret not in completed.stdout
        assert secret not in completed.stderr
    finally:
        _make_writable(target)
