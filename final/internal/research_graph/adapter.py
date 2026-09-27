"""Bridge graph events and review decisions onto the existing native SSE vocabulary.

Event names, payload shapes and emission order are contract: the graph backend
must be indistinguishable from the native engine at the run-ledger boundary.
``plan_created`` and ``done`` are never emitted here — the ledger emits them via
``pause_for_plan`` and ``finish`` exactly as it does for the native engine.
"""

from __future__ import annotations

TOKEN_CHUNK_CHARS = 400

EVENT_VOCABULARY = frozenset({
    "plan_created",
    "node_start",
    "node_done",
    "research_round",
    "source_found",
    "code_exec",
    "token",
    "done",
})


class EventBridge:
    """Forwards node events to the native ``on_event`` sink."""

    def __init__(self, sink=None):
        self._sink = sink

    def emit(self, event, data=None):
        if self._sink is not None and event in EVENT_VOCABULARY and event != "done":
            self._sink(event, data or {})

    def token_chunks(self, report):
        """Emit the validated report in the same 400-char chunks the native engine uses."""
        for offset in range(0, len(report), TOKEN_CHUNK_CHARS):
            self.emit("token", {"content": report[offset:offset + TOKEN_CHUNK_CHARS]})


def review_decision(interrupt_value, ledger_plan):
    """Build the ``Command(resume=...)`` decision from the ledger-approved plan.

    The run ledger stays the source of truth for plan review (its ``review_plan``
    API owns compare-and-swap on ``plan_version``). When the ledger-approved plan
    differs from the interrupted plan, the decision is an edit that applies the
    submitted steps to the graph state; the decision must echo the interrupted
    version or the review node rejects it.
    """
    if not isinstance(interrupt_value, dict) or "plan" not in interrupt_value:
        raise ValueError("Plan review interrupt payload is malformed")
    version = interrupt_value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("A positive plan version is required")
    action = "approve" if interrupt_value.get("plan") == ledger_plan else "edit"
    return {"action": action, "version": version, "plan": ledger_plan}
