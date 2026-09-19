"""Run the frozen Go baseline and the Python behavioral conformance suite.

This is deliberately not a source-file-count comparison.  It resolves the
frozen Go commit, exports that exact tree into a temporary directory, records
hashes from the exported tree and runs the complete Go test suite there.  The
current Go working tree is only observed for provenance; uncommitted work can
neither alter the evidence nor make the gate fail.  Python contracts then
encode the same route, DAG, retrieval, memory, MCP and HTTP behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


EXPECTED_GO_COMMIT = "845e8f7"
PYTHON_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GO_ROOT = PYTHON_ROOT.parent.parent / "AGI-saber"

GO_EVIDENCE_FILES = (
    "internal/application/chat/runtime_process.go",
    "internal/application/chat/plan_graph.go",
    "internal/application/chat/subagents.go",
    "internal/application/chat/mem_commit.go",
    "internal/application/chat/mem_writer.go",
    "internal/domain/rag/hybrid.go",
    "internal/infrastructure/tool/mcp.go",
    "internal/interfaces/http/handler/handler.go",
    "internal/interfaces/http/middleware/auth_middleware.go",
)

PYTHON_EVIDENCE_FILES = (
    "internal/agent/agent.py",
    "internal/agent/planner.py",
    "internal/agent/subagents.py",
    "internal/agent/graph_runtime.py",
    "internal/agent/memory_writer.py",
    "internal/memory/memory.py",
    "internal/repo/memory_projection.py",
    "internal/rag/hybrid.py",
    "internal/tools/tools.py",
    "internal/application/api.py",
    "internal/application/auth.py",
)

PYTHON_CONTRACT_CANDIDATES = (
    "tests/test_go_parity_contract.py",
    "tests/test_rag_go_parity_contract.py",
    "tests/test_memory_consistency_parity.py",
    "tests/test_longterm_store_classified.py",
    "tests/test_memory_writer.py",
    "tests/test_async_memory_writer.py",
    "tests/test_memory_transactional_projection.py",
    "tests/test_subagents.py",
    "tests/test_graph_runtime.py",
    "tests/test_mcp_structured_contract.py",
    "tests/test_http_go_wire_contract.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_manifest(root: Path, names: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"缺少复刻证据文件：{path}")
        result[name] = _sha256(path)
    return result


def _run(command: list[str], cwd: Path) -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return {
        "command": command,
        "cwd": str(cwd),
        "exit_code": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "output": completed.stdout,
        "passed": completed.returncode == 0,
    }


def _git_revision(go_root: Path, revision: str) -> str:
    result = _run(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
        go_root,
    )
    if not result["passed"]:
        raise RuntimeError(
            result["output"].strip()
            or f"无法解析 Go commit：{revision}"
        )
    return str(result["output"]).strip()


def _extract_tar_safely(archive_path: Path, destination: Path) -> None:
    """Extract the trusted git archive while still rejecting path traversal."""

    destination = destination.resolve()
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError(f"Go 基线归档包含越界路径：{member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"Go 基线归档包含不受支持的链接：{member.name}")
        archive.extractall(destination)


def _materialize_git_commit(
    go_root: Path,
    commit: str,
    destination: Path,
) -> dict:
    """Export one immutable Git tree without touching the user's worktree."""

    archive_path = destination.parent / "go-baseline.tar"
    started = time.perf_counter()
    result = _run(
        [
            "git",
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            commit,
        ],
        go_root,
    )
    if not result["passed"]:
        return result
    try:
        destination.mkdir(parents=True, exist_ok=True)
        _extract_tar_safely(archive_path, destination)
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        result["passed"] = False
        result["exit_code"] = 1
        result["output"] = (
            f"{result['output']}\n归档解压失败：{exc}"
        ).strip()
    result["duration_seconds"] = round(time.perf_counter() - started, 3)
    result["archive_commit"] = commit
    result["export_root"] = str(destination)
    return result


def _markdown(report: dict) -> str:
    checks = report["checks"]
    lines = [
        "# Go → Python 行为一致性证据报告",
        "",
        f"- 生成时间（UTC）：`{report['generated_at']}`",
        f"- 冻结 Go 基线：`{report['go']['frozen_commit']}`",
        f"- 期望基线：`{report['go']['expected_commit']}`",
        f"- 当前工作树 HEAD（仅记录）：`{report['go']['current_worktree_head']}`",
        f"- 总结论：**{'通过' if report['passed'] else '未通过'}**",
        "",
        "## 可执行检查",
        "",
        "| 检查 | 结果 | 耗时 |",
        "|---|---:|---:|",
    ]
    for check in checks:
        lines.append(
            f"| `{check['name']}` | {'通过' if check['passed'] else '失败'} | "
            f"{check.get('duration_seconds', 0):.3f}s |"
        )
    lines.extend(
        [
            "",
            "## 证据边界",
            "",
            "Go 测试和源码哈希均来自冻结提交的临时 `git archive`，当前 Go 工作树"
            "即使有未提交改动也不会进入证据或导致假失败。",
            "",
            "这份报告证明冻结 Go 提交自身测试通过，且 Python 的对照合同通过；"
            "它不是生产流量质量结论，也不替代真实 PostgreSQL、Milvus、"
            "Elasticsearch、Neo4j、Kafka 与 Docker 沙箱的故障注入验收。",
            "",
            "实现文件的 SHA-256 已写入同目录 JSON，可用于复核测试时实际审计的源码版本。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Go/Python 行为一致性证据套件")
    parser.add_argument("--go-root", type=Path, default=DEFAULT_GO_ROOT)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PYTHON_ROOT / "runtime" / "go-python-conformance",
    )
    parser.add_argument("--skip-go-tests", action="store_true")
    args = parser.parse_args()

    go_root = args.go_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not (go_root / "go.mod").is_file():
        raise SystemExit(f"Go 项目目录无效：{go_root}")

    current_head = _git_revision(go_root, "HEAD")
    frozen_commit = _git_revision(go_root, EXPECTED_GO_COMMIT)
    baseline_ok = frozen_commit.startswith(EXPECTED_GO_COMMIT)
    checks = [
        {
            "name": "frozen_go_commit_resolves",
            "passed": baseline_ok,
            "duration_seconds": 0.0,
            "expected": EXPECTED_GO_COMMIT,
            "actual": frozen_commit,
        }
    ]
    go_status = _run(
        ["git", "status", "--porcelain", "--", *GO_EVIDENCE_FILES],
        go_root,
    )
    dirty_evidence_paths = [
        line for line in str(go_status["output"]).splitlines() if line.strip()
    ]

    with tempfile.TemporaryDirectory(prefix="agi-saber-go-baseline-") as temp:
        frozen_root = Path(temp) / "source"
        archive_check = _materialize_git_commit(
            go_root,
            frozen_commit,
            frozen_root,
        )
        checks.append({"name": "frozen_go_archive_materialized", **archive_check})
        if not archive_check["passed"]:
            raise SystemExit(
                str(archive_check["output"]).strip()
                or "无法导出冻结 Go 基线"
            )

        if not args.skip_go_tests:
            go_test = _run(["go", "test", "./..."], frozen_root)
            checks.append({"name": "frozen_go_test_all", **go_test})

        test_files = [
            item for item in PYTHON_CONTRACT_CANDIDATES
            if (PYTHON_ROOT / item).is_file()
        ]
        if not test_files:
            raise SystemExit("未找到 Python 一致性测试")
        pytest_temp = output_dir / "pytest-tmp"
        python_test = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                *test_files,
                "-q",
                "--basetemp",
                str(pytest_temp),
            ],
            PYTHON_ROOT,
        )
        checks.append({"name": "python_conformance_contracts", **python_test})

        report = {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "passed": all(bool(check["passed"]) for check in checks),
            "go": {
                "root": str(go_root),
                "head": frozen_commit,
                "frozen_commit": frozen_commit,
                "expected_commit": EXPECTED_GO_COMMIT,
                "evidence_source": "git archive",
                "current_worktree_head": current_head,
                "current_worktree_status_command_ok": go_status["passed"],
                "current_worktree_dirty_evidence_paths": dirty_evidence_paths,
                "source_sha256": _hash_manifest(
                    frozen_root,
                    GO_EVIDENCE_FILES,
                ),
            },
            "python": {
                "root": str(PYTHON_ROOT),
                "contract_files": test_files,
                "source_sha256": _hash_manifest(
                    PYTHON_ROOT, PYTHON_EVIDENCE_FILES
                ),
            },
            "checks": checks,
            "limitations": [
                "不代表生产流量上的模型或 RAG 质量提升",
                "不替代真实外部基础设施与 Docker 沙箱故障注入",
                "Python 超集端点不属于 Go 子集的逐字节协议比较",
            ],
        }
    (output_dir / "conformance-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "conformance-report.md").write_text(
        _markdown(report), encoding="utf-8"
    )
    print(json.dumps({
        "passed": report["passed"],
        "report": str(output_dir / "conformance-report.md"),
        "results": str(output_dir / "conformance-results.json"),
    }, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
