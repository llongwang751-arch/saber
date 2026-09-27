"""Typed state for the LangGraph chat graph (policy → prepare/route → branch → finalize).

One chat turn is a single request-scoped graph run: the durable outcome is the
:class:`internal.agent.contracts.Response` plus the conversation STM/chat
history and the trace persistence in ``finalize`` — the same "ledger is the
source of truth" rule as the research graph. The checkpointer is therefore
off by default (the conversation ``_turn_lock`` owns concurrency) and the
state only carries routing decisions between supersteps.
"""

from __future__ import annotations

from typing import TypedDict


class ChatGraphState(TypedDict, total=False):
    query: str
    mode: str
    denied: bool
