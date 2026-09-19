"""Bounded conversation runtimes: private STM/tasks, shared user knowledge.

No model, connection, sandbox or background writer is created per conversation.
The pool retains leases so an in-flight conversation cannot be evicted.
"""
from __future__ import annotations

import copy
import threading
import time
from contextlib import contextmanager

from internal.memory.memory import ShortTerm
from internal.memory.mem_stack import MemoryStack
from internal.tools.tools import ToolExecutor
from .cancel import CancelRegistry
from .subagents import register_builtin_subagents


class ConversationBusy(RuntimeError):
    pass


class ConversationPool:
    def __init__(self, owner, capacity=32, idle_seconds=1800):
        self.owner = owner
        self.capacity = max(1, int(capacity))
        self.idle_seconds = idle_seconds
        self._lock = threading.RLock()
        self._entries = {}

    @contextmanager
    def lease(self, conversation_id):
        with self._lock:
            now = time.monotonic()
            for key, entry in list(self._entries.items()):
                if not entry[1] and now - entry[2] > self.idle_seconds:
                    del self._entries[key]
            entry = self._entries.get(conversation_id)
            if entry is None:
                if len(self._entries) >= self.capacity:
                    idle = [(value[2], key) for key, value in self._entries.items() if not value[1]]
                    if not idle:
                        raise ConversationBusy("会话容量已满，请稍后重试")
                    del self._entries[min(idle)[1]]
                entry = [self._clone(conversation_id), False, now]
                self._entries[conversation_id] = entry
            if entry[1]:
                raise ConversationBusy("当前会话仍在执行，请等待完成或取消后重试")
            entry[1] = True
            entry[0].tool_executor = ToolExecutor(list(self.owner.tool_executor.snapshot().values()))
            if "write_document" in entry[0].tool_executor.snapshot():
                entry[0]._register_document_tools()
        try:
            yield entry[0]
        finally:
            with self._lock:
                entry[1] = False
                entry[2] = time.monotonic()

    def cancel(self, conversation_id):
        with self._lock:
            entry = self._entries.get(conversation_id)
            if entry:
                entry[0]._cancel_registry.cancel_all()

    def _clone(self, conversation_id):
        agent = copy.copy(self.owner)
        agent.conversation_id = conversation_id
        agent._conversations = None
        agent._turn_lock = threading.Lock()
        agent.stm = ShortTerm(agent.cfg.short_term_max_turns)
        agent.mem = MemoryStack(stm=agent.stm, ltm=agent.ltm, preference=agent.preference)
        agent.mem.attach_graph(getattr(agent, "graph_memory", None))
        agent._cancel_registry = CancelRegistry()
        agent._turn_count = 0
        agent.subagents = register_builtin_subagents(agent)
        agent.tool_executor = ToolExecutor(list(self.owner.tool_executor.snapshot().values()))
        # Rebind tools whose closures access the conversation's Agent.
        agent._register_builtin_tools()
        agent._build_prompt_context()
        repo = getattr(agent, "chat_repo", None)
        if repo is not None:
            history = repo.load(agent.cfg.short_term_max_turns * 2,
                                user_id=agent.user_id, conversation_id=conversation_id)
            for item in history:
                agent.stm.add(item.role, item.content)
        return agent
