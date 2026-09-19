"""Lazy per-user Agent registry used to enforce memory and tool isolation."""

from __future__ import annotations

import threading
from typing import Callable


class AgentRegistry:
    def __init__(self, factory: Callable[[str], object], *, seed_user_id: str = "", seed_agent=None, capacity: int = 128):
        self._factory = factory
        self.capacity = max(1, int(capacity))
        self._agents: dict[str, object] = {}
        self._lock = threading.RLock()
        if seed_user_id and seed_agent is not None:
            self._agents[seed_user_id] = seed_agent

    def get(self, user_id: str):
        user_id = str(user_id or "default_user")
        with self._lock:
            agent = self._agents.get(user_id)
            if agent is None:
                if len(self._agents) >= self.capacity:
                    raise RuntimeError("Agent capacity reached; retry after capacity is released")
                agent = self._factory(user_id)
                self._agents[user_id] = agent
            return agent

    def close(self) -> None:
        with self._lock:
            agents = list(self._agents.values())
            self._agents.clear()
        for agent in agents:
            writer = getattr(agent, "memory_writer", None)
            close = getattr(writer, "close", None) or getattr(writer, "stop", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
