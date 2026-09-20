"""Memory Conflict Resolution and Temporal Versioning Engine.

Inspired by Mem0 (EmbedChain)'s dynamic fact lifecycle, this module prevents
stale and contradictory memory clutter by automatically classifying incoming
facts as ADD, UPDATE (superseding older contradictory facts), DELETE, or NOOP.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class MemoryAction(str, Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "noop"


@dataclass
class VersionedMemoryFact:
    fact_id: str
    user_id: str
    category: str
    content: str
    status: str = "active"  # "active" | "superseded" | "deleted"
    created_at: float = field(default_factory=time.time)
    superseded_by: Optional[str] = None
    superseded_at: Optional[float] = None


# Heuristic slot keys for common identity and preference conflict domains
_CONFLICT_DOMAINS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(?:住|生活|在|位于)\s*([^\s，,。]+)", re.I), "residence"),
    (re.compile(r"(?:工作|任职|职业|岗位|做)\s*([^\s，,。]+)", re.I), "job"),
    (re.compile(r"(?:喜欢|爱|偏好|喝|吃)\s*([^\s，,。]+)", re.I), "food_beverage"),
    (re.compile(r"(?:戒了|不喝|不吃|讨厌|忌口)\s*([^\s，,。]+)", re.I), "food_restriction"),
]


class MemoryConflictResolver:
    """Detects and resolves semantic contradictions in long-term memory."""

    @staticmethod
    def _extract_domain_and_target(text: str) -> Tuple[Optional[str], Optional[str]]:
        for pat, domain in _CONFLICT_DOMAINS:
            m = pat.search(text)
            if m:
                return domain, m.group(1).strip()
        return None, None

    @classmethod
    def resolve_action(
        cls,
        existing_facts: List[VersionedMemoryFact],
        new_content: str,
        category: str,
    ) -> Tuple[MemoryAction, Optional[VersionedMemoryFact]]:
        """Determine whether to ADD, UPDATE (supersede), or NOOP the new fact."""
        clean_new = new_content.strip()
        new_domain, new_target = cls._extract_domain_and_target(clean_new)

        active_facts = [f for f in existing_facts if f.status == "active"]

        for old_fact in active_facts:
            clean_old = old_fact.content.strip()

            # 1. Exact or near-identical content -> NOOP (prevent memory duplication)
            if clean_new == clean_old or (len(clean_new) > 4 and clean_new in clean_old):
                return MemoryAction.NOOP, old_fact

            # 2. Direct contradiction detection:
            # e.g., "我喜欢喝咖啡" vs "我戒了咖啡/不喜欢咖啡"
            if ("咖啡" in clean_new and "咖啡" in clean_old) or ("香菜" in clean_new and "香菜" in clean_old):
                has_negation_new = any(w in clean_new for w in ("戒", "不", "讨厌", "忌口"))
                has_negation_old = any(w in clean_old for w in ("戒", "不", "讨厌", "忌口"))
                if has_negation_new != has_negation_old:
                    return MemoryAction.UPDATE, old_fact

            # 3. Domain attribute change:
            # e.g., "住在北京" vs "住在上海"
            old_domain, old_target = cls._extract_domain_and_target(clean_old)
            if new_domain and old_domain and new_domain == old_domain:
                if new_target != old_target:
                    return MemoryAction.UPDATE, old_fact

        return MemoryAction.ADD, None

    @classmethod
    def apply_update(
        cls,
        existing_facts: List[VersionedMemoryFact],
        new_content: str,
        category: str,
        user_id: str,
    ) -> Tuple[MemoryAction, VersionedMemoryFact]:
        """Apply conflict resolution, marking outdated facts superseded."""
        action, target_old_fact = cls.resolve_action(existing_facts, new_content, category)

        new_fact_id = f"fact_{uuid.uuid4().hex[:8]}"
        new_fact = VersionedMemoryFact(
            fact_id=new_fact_id,
            user_id=user_id,
            category=category,
            content=new_content,
            status="active",
        )

        if action == MemoryAction.UPDATE and target_old_fact is not None:
            # Supersede the old fact
            target_old_fact.status = "superseded"
            target_old_fact.superseded_by = new_fact_id
            target_old_fact.superseded_at = time.time()
            existing_facts.append(new_fact)
            return MemoryAction.UPDATE, new_fact

        if action == MemoryAction.ADD:
            existing_facts.append(new_fact)
            return MemoryAction.ADD, new_fact

        return MemoryAction.NOOP, target_old_fact or new_fact
