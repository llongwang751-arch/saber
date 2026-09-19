"""Memory package with lazy public exports to avoid infrastructure cycles."""

from __future__ import annotations

__all__ = ["ShortTerm", "LongTerm", "Item", "Preference"]


def __getattr__(name: str):
    if name in {"ShortTerm", "LongTerm", "Item"}:
        from .memory import Item, LongTerm, ShortTerm

        return {"ShortTerm": ShortTerm, "LongTerm": LongTerm, "Item": Item}[name]
    if name == "Preference":
        from .preference import Preference

        return Preference
    raise AttributeError(name)
