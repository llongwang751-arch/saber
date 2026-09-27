"""Context service for an explicitly supplied conversation runtime.

The caller owns mutable state and lifecycle; services do not retain a user or conversation.
"""

import json
import logging
from typing import Dict, List

from internal.llm.llm import Message
from internal.promptctx import (
    ConstraintsSource,
    ContextAssembler,
    PlannerSnapshot,
    PlannerSource,
    Policy,
    ProfileSource,
    Query,
    RecallSource,
    SourceRegistry,
    TaskMemBuffer,
    TaskMemSource,
    ToolStateSource,
    ToolStateTracker,
    default_schemas,
)
from internal.promptctx.compactor import ContextCompactor


logger = logging.getLogger(__name__)


def build_prompt_context(agent) -> None:
    agent.task_mem = TaskMemBuffer(20)
    agent.tool_tracker = ToolStateTracker(10)
    registry = SourceRegistry()
    registry.register(ProfileSource(agent.preference, agent.ltm))
    registry.register(PlannerSource(agent._planner_snapshot))
    registry.register(TaskMemSource(agent.task_mem))
    registry.register(ToolStateSource(lambda: agent.tool_executor.snapshot(), agent.tool_tracker))
    registry.register(
        ConstraintsSource(
            [
                Policy(pattern="rm -rf", reason="禁止破坏性删除命令", level="block"),
                Policy(pattern="sudo", reason="禁止提权命令", level="block"),
            ]
        )
    )
    registry.register(RecallSource(agent.ltm))
    agent.prompt_assembler = ContextAssembler(default_schemas(), registry)


def planner_snapshot(agent):
    task = agent._cancel_registry.current_task() if hasattr(agent, "_cancel_registry") else None
    if not task:
        return None
    steps = task.get("steps") or []
    return PlannerSnapshot(
        task_id=task.get("task_id", ""),
        query=task.get("query", ""),
        status=task.get("status", ""),
        phase=task.get("phase", ""),
        total_steps=len(steps),
        current_step=task.get("current_step", 0),
        interrupted_at=task.get("interrupted_at", ""),
    )


def build_context_prefix(agent, query: str, mode: str = "chat") -> str:
    if not hasattr(agent, "prompt_assembler"):
        agent._build_prompt_context()
    try:
        try:
            embedding = agent.llm.embed(query)
        except Exception:
            embedding = []
        current = agent._cancel_registry.current_task() if hasattr(agent, "_cancel_registry") else None
        task_id = str((current or {}).get("task_id", "")) if isinstance(current, dict) else ""
        return agent.prompt_assembler.assemble(
            Query(
                text=query,
                embedding=embedding,
                task_id=task_id,
                mode=mode,
                user_id=getattr(agent, "user_id", ""),
            )
        ).render()
    except Exception as e:
        logger.warning("⚠️  promptctx 装配失败，降级到旧记忆前缀: %s", e)
        return agent._build_memory_system_prefix(query)


def build_memory_system_prefix(agent, query: str = "") -> str:
    parts: List[str] = []
    prefs = agent.preference.get_all()
    if prefs:
        parts.append(f"用户偏好: {json.dumps(prefs, ensure_ascii=False)}")
    memories = agent.ltm.recall(query, agent.cfg.long_term_top_k) if query else []
    if memories:
        parts.append("相关记忆:\n" + "\n".join(f"- {m.content}" for m in memories))
    return "\n".join(parts)


def compactor_summary_fn(agent, turns_to_compress: List[Dict[str, str]]) -> str:
    """为被滑动窗口淘汰的历史对话生成滚动摘要（优先快模型 LLM，离线/测试降级为启发式规则）。"""
    if not turns_to_compress:
        return ""
    if (
        hasattr(agent, "llm")
        and agent.llm is not None
        and hasattr(agent, "cfg")
        and getattr(agent.cfg, "is_real_llm", lambda: False)()
    ):
        try:
            transcript = "\n".join(f"{t.get('role', 'user')}: {t.get('content', '')}" for t in turns_to_compress)
            prompt = [
                Message(
                    role="user",
                    content=(
                        "请用简明扼要的一两句话提炼以下多轮对话的关键背景、用户关键偏好与重要决策，"
                        "作为后续对话的前情提要，不要废话：\n\n" + transcript
                    ),
                )
            ]
            summary = agent.llm.chat_fast(prompt)
            if summary and summary.strip():
                return summary.strip()
        except Exception as e:
            logger.debug("LLM 摘要生成失败，降级为规则提炼: %s", e)
    return ContextCompactor._default_heuristic_summary(turns_to_compress)


def build_history_messages(agent, query: str) -> List[Message]:
    compactor = getattr(agent, "compactor", None)
    if compactor is None or not getattr(agent.cfg, "enable_context_compactor", True):
        msgs = [Message(role=m["role"], content=m["content"]) for m in agent.stm.get()]
        if not msgs or msgs[-1].content != query:
            msgs.append(Message(role="user", content=query))
        return msgs

    # 启用了 ContextCompactor
    uncompacted = getattr(agent, "_uncompacted_turns", None)
    if uncompacted is None:
        uncompacted = []
        agent._uncompacted_turns = uncompacted

    # 若本地缓存为空但 stm 中已有对话历史（例如 DB 还原或多会话克隆），从 stm 初始化
    if not uncompacted and hasattr(agent, "stm"):
        stm_items = agent.stm.get()
        if stm_items:
            uncompacted.extend(stm_items)

    # 确保当前 query 在消息末尾
    if not uncompacted or uncompacted[-1].get("content") != query:
        uncompacted.append({"role": "user", "content": query})

    existing_summary = getattr(agent, "_rolling_summary", "")
    res = compactor.compact(uncompacted, existing_summary=existing_summary)

    if res.compressed_turns_count > 0:
        agent._rolling_summary = res.rolling_summary
        agent._uncompacted_turns = list(res.recent_history)
    else:
        agent._rolling_summary = res.rolling_summary

    msgs: List[Message] = []
    if agent._rolling_summary:
        msgs.append(Message(role="system", content=f"【前情提要与历史背景摘要】\n{agent._rolling_summary}"))

    for m in res.recent_history:
        msgs.append(Message(role=m.get("role", "user"), content=m.get("content", "")))

    return msgs
