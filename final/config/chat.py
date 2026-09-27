"""Small, validated configuration surface for the chat application."""

import os


ENGINES = ("native", "langgraph")


def initialize(cfg):
    cfg.chat_engine = "langgraph"


def configure(cfg, data):
    chat = data.get("chat") or {}
    engine = os.getenv("AGI_CHAT_ENGINE", chat.get("engine", "langgraph"))
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ValueError("chat.engine must be one of: " + ", ".join(ENGINES))
    cfg.chat_engine = engine
