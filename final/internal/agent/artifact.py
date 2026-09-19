"""ReAct 最终答案的交付文件物化，与 Go artifact.go 行为对齐。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

from internal.llm.llm import Message
from internal.sandbox.types import ExecRequest


ARTIFACT_KEYWORDS = (
    "报告", "作业", "文档", "方案", "总结", "计划书", "文案", "简历", "论文",
    "写一份", "写一篇", "生成一份", "生成一个文件", "导出",
)


def prepare_artifact_workspace(agent, task_id: str) -> str:
    base = str(getattr(agent.cfg, "sandbox_artifact_host_dir", "") or "")
    if not base:
        return ""
    user_id = _sanitize_path_segment(getattr(agent, "user_id", "") or "anonymous")
    path = Path(os.path.expanduser(base)) / user_id / _sanitize_path_segment(task_id)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    return str(path)


class ArtifactExecutionBlocked(RuntimeError):
    pass


def produce_artifact(agent, query: str, content: str, workspace: str, on_event=None, token=None) -> Optional[dict]:
    if not workspace or not (content or "").strip():
        return None
    needs_file, filename = decide_artifact(agent, query)
    if not needs_file:
        return None
    filename = _sanitize_filename(filename)
    from internal.agent.tool_execution import guarded_tool_attempt
    from internal.tools.tools import Tool
    from internal.harness.journal import fingerprint
    registry = getattr(agent, '_cancel_registry', None)
    task = registry.current_task() if registry is not None else {}
    invocation = str((task or {}).get('task_id') or fingerprint(workspace)) + ':artifact'
    wrapped = Tool('produce_artifact', '保存交付文件', [],
        lambda params: json.dumps(_produce_artifact(agent, params['content'], params['workspace'], params['filename'], on_event), ensure_ascii=False),
        side_effecting=True)
    result = guarded_tool_attempt(agent, wrapped, 'produce_artifact',
        {'workspace': workspace, 'filename': filename, 'content': content}, token, 30, invocation)
    if not result.success:
        raise ArtifactExecutionBlocked(str(result.error or '产物生成被执行策略阻止'))
    return json.loads(result.payload)


def _produce_artifact(agent, content, workspace, filename, on_event):
    absolute = str(Path(workspace) / filename)
    _emit(on_event, "node_start", {"id": "artifact", "tool": "produce_artifact"})
    _emit(on_event, "step", {"type": "Thought", "content": "生成交付文件"})
    _emit(on_event, "step", {
        "type": "Action", "content": f"在沙箱生成文件 {filename}",
        "tool": "produce_artifact",
    })
    produced, backend = _materialize(agent, workspace, filename, content)
    if not produced:
        from internal.tools.tools import ToolError
        raise ToolError('artifact_write_failed', '产物写入失败；请核对目标目录，禁止自动重放', retryable=False)
    if produced and backend:
        observation = f"已在沙箱({backend})内生成并挂载到宿主机：{absolute}"
    elif produced:
        observation = f"已生成文件：{absolute}"
    else:
        observation = f"产出文件失败：{absolute}"
    step = {"type": "Observation", "content": observation, "tool": "produce_artifact", "params": None}
    _emit(on_event, "step", step)
    _emit(on_event, "node_done", {
        "id": "artifact", "tool": "produce_artifact", "status": "done", "path": absolute,
    })
    return step


def decide_artifact(agent, query: str) -> Tuple[bool, str]:
    if re.search(r'(?:不要|无需|不需要|禁止|别)(?:生成|保存|创建|写入|导出).{0,12}(?:文件|文档|报告)', query or ''):
        return False, ''
    def heuristic() -> Tuple[bool, str]:
        if any(keyword in (query or "") for keyword in ARTIFACT_KEYWORDS):
            return True, _safe_title(query) + ".md"
        return False, ""

    if not agent.cfg.is_real_llm():
        return heuristic()
    prompt = (
        "判断用户是否需要一个可交付的成文文件（报告/作业/文档/方案等）。\n"
        '只输出极简 JSON：{"needs_file":true,"filename":"报告.md"} 或 {"needs_file":false}\n'
        f"用户请求：{query}"
    )
    chat = getattr(agent.llm, "chat_fast", None) or agent.llm.chat
    try:
        raw = chat([Message(role="user", content=prompt)], system_prompt="你严格只输出一行极简 JSON。")
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        decision = json.loads(match.group(0) if match else cleaned)
    except Exception:
        return heuristic()
    needs = decision.get("needs_file") is True
    filename = str(decision.get("filename") or "")
    if needs and not filename.strip():
        filename = _safe_title(query) + ".md"
    return needs, filename


def _materialize(agent, workspace: str, filename: str, content: str) -> Tuple[bool, str]:
    root = Path(workspace)
    stage = f".agi_stage_{time.time_ns()}"
    stage_path = root / stage
    final_path = root / filename
    try:
        stage_path.write_text(content, encoding="utf-8")
        sandbox = getattr(agent, "sandbox", None)
        if sandbox is not None:
            result = sandbox.exec(None, ExecRequest(
                command=f"cp {_shell_quote(stage)} {_shell_quote(filename)}",
                workspace_host_dir=workspace,
                timeout=20,
            ))
            if result.exit_code == 0 and final_path.exists():
                return True, str(getattr(result, "backend", "") or "")
        shutil.copyfile(stage_path, final_path)
        return True, ""
    except OSError:
        return False, ""
    finally:
        try:
            stage_path.unlink(missing_ok=True)
        except OSError:
            pass


def _sanitize_filename(name: str) -> str:
    value = Path((name or "").strip()).name.replace("/", "_").replace("\\", "_").lstrip(".")
    return value or f"artifact_{int(time.time())}.md"


def _sanitize_path_segment(value: str) -> str:
    return (value or "default").strip().replace("/", "_").replace("\\", "_").replace("..", "_") or "default"


def _safe_title(query: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", (query or "").strip())
    return value[:60].strip(" ._") or "artifact"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _emit(callback, event_type: str, data: dict) -> None:
    if callable(callback):
        callback({"type": event_type, "data": data})
