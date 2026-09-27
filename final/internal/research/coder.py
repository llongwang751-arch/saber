"""Python analysis is confined to a disposable Docker workspace."""

import json
import tempfile
from pathlib import Path

from internal.sandbox.types import ExecRequest

from .providers import check_cancel


def execute_python(code, evidence, sandbox, *, token=None, timeout=30):
    """Never execute on local/mock backends or interpret simulated output as success."""
    check_cancel(token)
    result = {"status": "code_only", "code": code, "stdout": "", "stderr": "",
              "reason": "没有可用的 Docker 隔离沙箱；仅生成代码，未执行"}
    if sandbox is None or not callable(getattr(sandbox, "backend", None)) or sandbox.backend() != "docker":
        return result
    executor = getattr(sandbox, "_executor", sandbox)
    cfg = getattr(executor, "cfg", None)
    if cfg is not None and (not cfg.network_disabled or not cfg.readonly_rootfs):
        result["reason"] = "研究代码要求断网且根文件系统只读的 Docker 沙箱；仅生成代码，未执行"
        return result
    # The only host mount is a new directory containing this script and its
    # explicit input. No repository, credentials, or previous workspace is mounted.
    with tempfile.TemporaryDirectory(prefix="saber-research-") as workspace:
        Path(workspace, "analysis.py").write_text(code, encoding="utf-8")
        Path(workspace, "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        try:
            from internal.resilience.budget import charge
            charge("tool")
            response = sandbox.exec(token, ExecRequest(
                command="python3 -I /workspace/analysis.py", timeout=max(0.01, min(timeout, 30)),
                confirm=False, workspace_host_dir=workspace,
            ))
            check_cancel(token)
        except InterruptedError:
            raise
        except Exception as exc:
            result["reason"] = f"沙箱执行不可用；代码未确认执行成功：{type(exc).__name__}"
            return result
    result.update(stdout=str(response.stdout or "")[:16000], stderr=str(response.stderr or "")[:8000],
                  exit_code=response.exit_code)
    if response.backend != "docker":
        result["reason"] = "沙箱后端发生降级；结果未计作真实执行"
    elif response.exit_code == 0 and not response.killed:
        result.update(status="executed", reason="")
    else:
        result.update(status="failed", reason="隔离分析执行失败；未将代码推测结果写作已验证事实")
    return result
