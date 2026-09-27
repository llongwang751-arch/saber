"""Memory service for an explicitly supplied conversation runtime.

The caller owns mutable state and lifecycle; services do not retain a user or conversation.
"""

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from typing import List

from internal.promptctx import (
    StepObservation,
    ToolCallTrace,
)
from internal.observability import redact_text, sanitize_trace

from .cancel import go_safe
from .memory_writer import (
    extract_memory_from_reply,
    maybe_consolidate_memory,
)


from .contracts import Response

logger = logging.getLogger(__name__)


def finalize(agent, query: str, resp: Response) -> None:
    """assistant 写回 + 异步记忆抽取 + 异步图感知合并 + 事件发布 + 计数。"""
    agent.stm.add("assistant", resp.answer)
    agent._save_chat_history("assistant", resp.answer)
    if hasattr(agent, "_uncompacted_turns") and isinstance(agent._uncompacted_turns, list):
        agent._uncompacted_turns.append({"role": "assistant", "content": resp.answer})

    # 异步：从回复中提取事实 → 长期记忆
    if getattr(agent.cfg, "memory_store_exchange_facts", False):
        agent.memory_writer.submit(lambda: extract_memory_from_reply(agent, resp.answer, user_query=query))
    # 异步：长期记忆合并/淘汰（有图层时走图感知合并）
    agent.memory_writer.submit(lambda: maybe_consolidate_memory(agent))

    # 每 N 轮快照一次 agent 状态到 PG
    agent._turn_count += 1
    if agent._turn_count % agent._snapshot_every == 0:
        go_safe("snapshot", lambda: agent._save_agent_snapshot(query, resp))

    try:
        agent.inf.repo.events.publish(
            "agent.chat",
            json.dumps({"query": query, "mode": resp.mode}, ensure_ascii=False),
        )
    except Exception:
        pass

    resp.short_term_count = agent.stm.count()
    resp.long_term_count = len(agent.ltm.items)
    resp.preferences = agent.preference.get_all()
    agent._persist_trace(query, resp)


def persist_trace(agent, query: str, resp: Response) -> None:
    repo = getattr(getattr(agent.inf, "repo", None), "ragtrace", None)
    if repo is None or not hasattr(repo, "save"):
        return
    payload = sanitize_trace(
        {
            "trace_id": resp.trace_id,
            "mode": resp.mode,
            "intent": resp.intent,
            "slots": resp.slots,
            "rag": resp.rag_trace,
            "steps": [asdict(step) for step in resp.steps],
            "tool_call": resp.tool_call,
            "tool_calls": resp.tool_calls,
            "task": resp.task,
            "interrupted": resp.interrupted,
            "fallback": resp.fallback,
            "error": resp.error,
            "experiment_exposure_id": resp.experiment_exposure_id,
            "runtime_strategy_checksum": resp.runtime_strategy_checksum,
        }
    )
    try:
        repo.save(
            resp.trace_id,
            agent.user_id,
            resp.mode,
            redact_text(query, max_length=1000),
            "interrupted" if resp.interrupted else "failed" if resp.error else "completed",
            payload,
        )
    except Exception as exc:
        logger.warning("⚠️  Trace 持久化失败 trace_id=%s: %s", resp.trace_id, exc)


def push_task_mem(agent, obs: StepObservation) -> None:
    if hasattr(agent, "task_mem"):
        agent.task_mem.push(obs)


def record_tool_call(agent, trace: ToolCallTrace) -> None:
    if hasattr(agent, "tool_tracker"):
        agent.tool_tracker.record(trace)


def save_snapshot(agent, task: dict) -> None:
    task_id = task.get("task_id", f"task_{int(time.time())}") if isinstance(task, dict) else f"task_{int(time.time())}"
    # 持锁追加到 cancel registry（与 main taskRuntime.appendSnapshot 对齐）
    if isinstance(task, dict):
        agent._cancel_registry.append_snapshot(
            {
                "state": dict(task),
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            }
        )
    try:
        try:
            agent.inf.repo.snapshot.save(task_id, json.dumps(task, ensure_ascii=False), user_id=agent.user_id)
        except TypeError:
            agent.inf.repo.snapshot.save(task_id, json.dumps(task, ensure_ascii=False))
    except Exception as e:
        logger.warning("⚠️  快照写入失败: %s", e)


def persist_task_checkpoint(agent, task: dict) -> None:
    agent.inf.repo.snapshot.save(task["task_id"], json.dumps(task, ensure_ascii=False), user_id=agent.user_id)


def snapshot_list(agent) -> List[dict]:
    """返回当前任务的内存快照列表（对应 main snapshotList）。"""
    return agent._cancel_registry.snapshot_list()


def save_chat_history(agent, role: str, content: str) -> None:
    """best-effort 写聊天记录。优先 chat_repo，其次 inf.repo.chat_history。"""
    chat_repo = getattr(agent, "chat_repo", None)
    if chat_repo is None:
        chat_repo = getattr(getattr(agent.inf, "repo", None), "chat_history", None)
    if chat_repo is not None and hasattr(chat_repo, "save"):
        try:
            try:
                if getattr(agent, "conversation_id", ""):
                    chat_repo.save(role, content, user_id=agent.user_id, conversation_id=agent.conversation_id)
                else:
                    chat_repo.save(role, content, user_id=agent.user_id)
            except TypeError:
                if getattr(agent, "conversation_id", ""):
                    raise  # Never spill a conversation into legacy user-wide history.
                chat_repo.save(role, content)
        except Exception:
            pass


def save_agent_snapshot(agent, query: str, resp: Response):
    """每 N 轮把 agent 整体状态序列化到 PG（含路由 mode/计数/偏好）。"""
    snapshot = {
        "task_id": f"agent_{int(time.time())}",
        "query": query,
        "mode": resp.mode,
        "short_term_count": resp.short_term_count,
        "long_term_count": resp.long_term_count,
        "preferences": resp.preferences,
        "timestamp": time.time(),
    }
    try:
        try:
            agent.inf.repo.snapshot.save(
                snapshot["task_id"], json.dumps(snapshot, ensure_ascii=False), user_id=agent.user_id
            )
        except TypeError:
            agent.inf.repo.snapshot.save(snapshot["task_id"], json.dumps(snapshot, ensure_ascii=False))
    except Exception as e:
        logger.warning("⚠️  agent 快照写入失败: %s", e)
