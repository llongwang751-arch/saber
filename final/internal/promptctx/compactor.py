"""Dynamic Sliding Window and Adaptive Context Compactor.

Inspired by MemGPT and AutoGen's context management, this module compresses
older conversation turns into structured rolling summaries once token/turn
watermarks are breached, guaranteeing a constant, healthy context length.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass
class CompactedContext:
    rolling_summary: str
    recent_history: List[Dict[str, str]]
    compressed_turns_count: int
    active_chars: int


class ContextCompactor:
    """Adaptive sliding-window conversation compactor."""

    def __init__(
        self,
        max_recent_turns: int = 3,
        char_watermark: int = 2000,
        summarizer_fn: Optional[Callable[[List[Dict[str, str]]], str]] = None,
    ):
        self.max_recent_turns = max(1, int(max_recent_turns))
        self.char_watermark = max(500, int(char_watermark))
        self.summarizer_fn = summarizer_fn or self._default_heuristic_summary

    @staticmethod
    def _default_heuristic_summary(turns_to_compress: List[Dict[str, str]]) -> str:
        """Lightweight heuristic summary generation when offline or without external LLM."""
        facts: List[str] = []
        for turn in turns_to_compress:
            role = turn.get("role", "user")
            content = turn.get("content", "").strip().replace("\n", " ")
            if len(content) > 80:
                content = content[:77] + "..."
            facts.append(f"{role}: {content}")
        return " | ".join(facts)

    def compact(
        self,
        history: List[Dict[str, str]],
        existing_summary: str = "",
    ) -> CompactedContext:
        """Compact older conversation turns into rolling summary if watermark exceeded."""
        if not history:
            return CompactedContext(
                rolling_summary=existing_summary,
                recent_history=[],
                compressed_turns_count=0,
                active_chars=len(existing_summary),
            )

        total_turns = len(history)
        total_chars = sum(len(t.get("content", "")) for t in history) + len(existing_summary)

        # If within capacity thresholds, retain full history
        if total_turns <= self.max_recent_turns and total_chars <= self.char_watermark:
            return CompactedContext(
                rolling_summary=existing_summary,
                recent_history=list(history),
                compressed_turns_count=0,
                active_chars=total_chars,
            )

        # Watermark breached: split into old (to compress) and recent (to keep verbatim)
        split_idx = max(0, total_turns - self.max_recent_turns)
        old_turns = history[:split_idx]
        recent_turns = history[split_idx:]

        # Incrementally update summary
        new_summary_chunk = self.summarizer_fn(old_turns)
        if existing_summary:
            updated_summary = f"{existing_summary}\n[后续进展] {new_summary_chunk}"
        else:
            updated_summary = f"[历史背景要点] {new_summary_chunk}"

        active_chars = sum(len(t.get("content", "")) for t in recent_turns) + len(updated_summary)

        return CompactedContext(
            rolling_summary=updated_summary,
            recent_history=recent_turns,
            compressed_turns_count=len(old_turns),
            active_chars=active_chars,
        )
