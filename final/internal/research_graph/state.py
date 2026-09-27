"""Typed state for the LangGraph research graph (plan → review → parallel research → code → report).

Channels are replaced wholesale between supersteps. Parallel research branches
publish no channel writes at all: the durable research state (sources, evidence,
usage, per-step progress) lives in the run ledger via the checkpoint callback and
in the composed native engine, so the checkpointer only persists graph-routing
state. ``data`` mirrors a snapshot of the research state after node completion.
"""

from __future__ import annotations

from typing import TypedDict


class ResearchGraphState(TypedDict, total=False):
    message: str
    use_rag: bool
    plan: dict
    review: dict
    rejected: bool
    data: dict


def pending_steps(plan, progress):
    """Plan steps that have not finished yet, in plan order."""
    return [step for step in plan["steps"] if not progress.get(step["id"], {}).get("finished")]


def ready_steps(plan, progress, kind=None):
    """Pending steps whose dependencies are all finished (native dependency semantics)."""
    pending_ids = {step["id"] for step in pending_steps(plan, progress)}
    return [
        step
        for step in pending_steps(plan, progress)
        if (kind is None or step["kind"] == kind) and all(dep not in pending_ids for dep in step["depends_on"])
    ]
