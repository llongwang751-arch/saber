"""生成并封存生产在线实验所需的运行环境组件清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config import default_config  # noqa: E402
from internal.experimentation.runtime_identity import (  # noqa: E402
    DeploymentComponentEvidence,
    RuntimeIdentity,
    RuntimeIdentityError,
    canonical_component_manifest_json,
    component_manifest_from_runtime,
    establish_production_runtime_identity,
)


def build_runtime_component_manifest(
    config: Any,
    evidence: DeploymentComponentEvidence,
    output_path: str | os.PathLike[str],
) -> RuntimeIdentity:
    """Create one new, read-only manifest and verify it before returning.

    Existing paths, including broken symlinks, are never overwritten.  Build
    pipelines should publish a new immutable artifact for each release.
    """

    requested = Path(output_path).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    if os.path.lexists(requested):
        raise FileExistsError(
            f"输出路径已存在，拒绝覆盖不可变清单：{requested}"
        )
    target = requested.resolve(strict=False)
    parent = target.parent
    if not parent.is_dir():
        raise ValueError(f"输出目录不存在：{parent}")
    if os.path.lexists(target):
        raise FileExistsError(f"输出路径已存在，拒绝覆盖不可变清单：{target}")

    manifest = component_manifest_from_runtime(config, evidence)
    payload = canonical_component_manifest_json(manifest).encode("utf-8")
    artifact_sha = hashlib.sha256(payload).hexdigest()
    temporary_path: Path | None = None
    published = False
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
        temporary_path = Path(raw_temporary_path)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # fdopen owns the descriptor and closes it on every exit path.
            raise
        temporary_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            if os.name == "nt":
                # Windows rename refuses an existing destination and does not
                # require deleting a read-only source name after publication.
                os.rename(temporary_path, target)
                published = True
                temporary_path = None
            else:
                # A hard link in the same directory publishes atomically and,
                # unlike POSIX rename, cannot overwrite a concurrent build.
                os.link(temporary_path, target)
                published = True
                temporary_path.unlink()
                temporary_path = None
        except FileExistsError as exc:
            raise FileExistsError(
                f"输出路径已存在，拒绝覆盖不可变清单：{target}"
            ) from exc
        _sync_directory(parent)
        identity = establish_production_runtime_identity(
            config,
            evidence,
            component_manifest_path=target,
            expected_artifact_sha256=artifact_sha,
        )
        return identity
    except BaseException:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            temporary_path.unlink()
        if published and os.path.lexists(target):
            # The target belongs to this invocation and failed its mandatory
            # post-publish verification, so it must not survive as evidence.
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
            target.unlink()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "读取真实应用配置和发布证据，生成只读的在线实验运行环境组件清单"
        )
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径；省略时按 AGI_CONFIG/项目默认配置解析",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="新清单的显式输出路径；已存在的文件不会被覆盖",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出便于 CI/CD 解析的 JSON 摘要（不包含密钥）",
    )
    return parser


def safe_summary(identity: RuntimeIdentity) -> dict[str, str | bool]:
    """Return the complete stdout allowlist; never add config or environment."""

    return {
        "status": "verified",
        "component_manifest_path": identity.component_manifest_path,
        "component_manifest_artifact_sha256": (
            identity.component_manifest_artifact_sha256
        ),
        "component_manifest_payload_sha256": (
            identity.component_manifest_payload_sha256
        ),
        "runtime_environment_fingerprint": identity.fingerprint,
        "production_verified": identity.production_verified,
    }


def render_chinese_summary(summary: Mapping[str, str | bool]) -> str:
    return "\n".join(
        (
            "运行环境组件清单已生成并通过校验。",
            f"清单路径：{summary['component_manifest_path']}",
            f"文件 SHA-256：{summary['component_manifest_artifact_sha256']}",
            f"内容 SHA-256：{summary['component_manifest_payload_sha256']}",
            f"运行环境指纹：{summary['runtime_environment_fingerprint']}",
            "生产证据状态：已验证",
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = default_config(args.config)
        evidence = DeploymentComponentEvidence.from_environment()
        identity = build_runtime_component_manifest(config, evidence, args.output)
    except (RuntimeIdentityError, FileExistsError, OSError, ValueError) as exc:
        print(f"组件清单生成失败：{exc}", file=sys.stderr)
        return 2
    summary = safe_summary(identity)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(render_chinese_summary(summary))
    return 0


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
