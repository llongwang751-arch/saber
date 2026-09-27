"""LangGraph execution backend for the research workflow.

The graph orchestrates; the native engine executes. Every node delegates step
work to a composed :class:`internal.research.ResearchEngine` so budget charging,
source ledger, citation validation, sandbox tooling and provider calls stay
byte-identical to the native backend. This class owns only orchestration:
interrupt()-based plan review, Send fan-out, checkpointing and the event bridge.

The run ledger remains the single source of truth. The SqliteSaver checkpoint
persists only graph state (which node is next, the pending review); ledger
state is refreshed through the same ``checkpoint`` callback the native engine
uses, so run recovery, SSE replay and the review API behave identically.
"""

from __future__ import annotations

import copy
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from langgraph.graph import END
from langgraph.types import Command, Send, interrupt

from internal.agent.plan_contracts import validate_research_plan
from internal.research import ResearchEngine, ResearchResult
from internal.research.reporting import render_report
from internal.resilience.budget import BudgetExceeded

from .adapter import EventBridge, review_decision
from .graph import build_research_graph
from .state import pending_steps, ready_steps

_SAVER = None
_SAVER_LOCK = threading.Lock()


def _open_saver(path):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(sqlite3.connect(str(target), check_same_thread=False))


def default_checkpointer(path=None):
    """One shared SqliteSaver per process; tests inject isolated paths."""
    global _SAVER
    if path is not None:
        return _open_saver(path)
    with _SAVER_LOCK:
        if _SAVER is None:
            _SAVER = _open_saver(
                os.getenv("AGI_LANGGRAPH_CHECKPOINT_DB")
                or Path(__file__).resolve().parents[2] / "runtime" / "langgraph-checkpoints.sqlite3"
            )
        return _SAVER


class LangGraphResearchEngine:
    """Same plan()/execute() surface as the native engine; orchestration via LangGraph."""

    def __init__(
        self,
        llm,
        *,
        search,
        reader=None,
        sandbox=None,
        limits=None,
        rag_search=None,
        allowed_tools=None,
        run_id="",
        conversation_id="",
        parent_run_id="",
        checkpointer=None,
        checkpoint_path=None,
    ):
        self._native = ResearchEngine(
            llm, search=search, reader=reader, sandbox=sandbox, limits=limits,
            rag_search=rag_search, allowed_tools=allowed_tools,
        )
        self._checkpointer = checkpointer or default_checkpointer(checkpoint_path)
        self._graph = build_research_graph(self, checkpointer=self._checkpointer)
        self._run_id, self._conversation_id = run_id, conversation_id
        self._parent_run_id = parent_run_id
        self._step_lock = threading.Lock()
        self._token = None
        self._on_event = None
        self._events = EventBridge(None)
        self._checkpoint_cb = None
        self._use_rag = False
        self._plan = None
        self._thread = ""
        self._active_thread = ""

    @classmethod
    def from_agent(cls, agent, *, run_id="", conversation_id="", parent_run_id="", **kwargs):
        native = ResearchEngine.from_agent(agent)
        return cls(
            native.llm, search=native.search, reader=native.reader, sandbox=native.sandbox,
            rag_search=native.rag_search, limits=native.limits, allowed_tools=native.allowed_tools,
            run_id=run_id, conversation_id=conversation_id, parent_run_id=parent_run_id, **kwargs,
        )

    # ------------------------------------------------------------------ runs

    def plan(self, message, *, use_rag=False, cancel_token=None, on_event=None):
        self._begin(cancel_token, on_event)
        self._use_rag = bool(use_rag)
        self._active_thread = self._thread_for_plan()
        config = {"configurable": {"thread_id": self._active_thread}}
        result = self._graph.invoke({"message": str(message), "use_rag": bool(use_rag)}, config)
        interrupts = result.get("__interrupt__") or []
        if not interrupts:
            raise RuntimeError("Research graph did not pause for plan review")
        self._plan = interrupts[0].value["plan"]
        return self._plan

    def execute(self, plan, *, run_id="", conversation_id="", use_rag=False, cancel_token=None,
                on_event=None, checkpoint=None, state=None, token=None):
        if hasattr(plan, "model_dump"):
            plan = plan.model_dump()
        plan = validate_research_plan(plan)
        self._begin(cancel_token or token, on_event)
        self._checkpoint_cb = checkpoint
        self._use_rag = bool(use_rag)
        self._plan = plan
        # The ledger state is the source of truth; the checkpoint only routes the graph.
        ledger_state = dict(state or {})
        self._active_thread = self._thread_for_execute(ledger_state)
        config = {"configurable": {"thread_id": self._active_thread}}
        snapshot = self._graph.get_state(config)
        if snapshot.values.get("rejected"):
            raise ValueError("Plan review was rejected; this run cannot execute")
        self._reset_native(ledger_state, run_id=run_id, conversation_id=conversation_id)
        try:
            if not snapshot.values:
                # Checkpoint missing but ledger state present: rebuild the graph
                # from the approved plan instead of re-running the planner.
                self._graph.invoke({"plan": plan, "use_rag": self._use_rag,
                                    "review": {"action": "approve", "version": 1}}, config)
            else:
                interrupts = self._interrupts(snapshot)
                if interrupts:
                    self._graph.invoke(Command(resume=review_decision(interrupts[0].value, plan)), config)
                else:
                    # Crash recovery: continue from the last committed superstep.
                    self._graph.invoke(None, config)
        except InterruptedError:
            self._native.state["cancelled"] = True
            self._native.state["limitations"].append("研究已取消；报告仅包含取消前保存的来源")
        except BudgetExceeded as exc:
            self._native.state["limitations"].append(str(exc))
        return self._result()

    def review(self, decision):
        """Deliver an explicit approve/edit/reject decision to the pending review interrupt."""
        config = {"configurable": {"thread_id": self._thread_for_plan()}}
        if not self._interrupts(self._graph.get_state(config)):
            raise ValueError("No plan review is pending for this run")
        return self._graph.invoke(Command(resume=decision), config)

    # -------------------------------------------------------------- plumbing

    def _begin(self, cancel_token, on_event):
        self._token, self._on_event = cancel_token, on_event
        self._events = EventBridge(on_event)

    def _thread_for_plan(self):
        if self._run_id:
            return f"research-{self._run_id}"
        if not self._thread:
            self._thread = f"research-graph-{uuid.uuid4()}"
        return self._thread

    def _thread_for_execute(self, ledger_state):
        return (
            str(ledger_state.get("graph_thread_id") or "")
            or self._thread
            or (f"research-{self._parent_run_id}" if self._parent_run_id else "")
            or self._thread_for_plan()
        )

    def _reset_native(self, ledger_state, *, run_id, conversation_id):
        native = self._native
        native._reset(ledger_state, self._token, self._on_event, self._checkpoint)
        native.state["run_id"], native.state["conversation_id"] = run_id, conversation_id
        native.state["graph_thread_id"] = self._active_thread

    def _checkpoint(self, state):
        state["graph_thread_id"] = self._active_thread
        if self._checkpoint_cb is not None:
            self._checkpoint_cb(state)

    def _emit(self, event, data=None):
        self._events.emit(event, data)

    def _snapshot(self):
        return copy.deepcopy(self._native.state)

    @staticmethod
    def _interrupts(snapshot):
        found = []
        for task in getattr(snapshot, "tasks", None) or ():
            found.extend(getattr(task, "interrupts", None) or ())
        return found

    def _result(self):
        native = self._native
        if not native.state.get("finalized"):
            self._finalize(self._plan)
        state = native.state
        return ResearchResult(
            state["report_markdown"], state["report_markdown"], state["status"],
            native.ledger.sources, native.ledger.evidence, state["references"],
            list(state["steps"].values()), state["artifacts"], state["usage"],
            state["limitations"], copy.deepcopy(state),
        )

    def _finalize(self, plan):
        """Report rendering, identical to the native engine's finalization block."""
        native = self._native
        state = native.state
        if state.get("finalized"):
            return
        state["finalized"] = True
        native._save()
        if not state["sections"]:
            from internal.research.reporting import _quote

            executed = [p for p in state["steps"].values() if p.get("kind") == "code" and p.get("status") == "executed"]
            state["sections"] = [
                "## 隔离代码执行结果\n\n" + _quote(p.get("stdout") or "脚本已执行成功，无标准输出。")
                for p in executed
            ] or ["已获取的原始材料如下；尚未形成完整研究结论。"]
        try:
            report, references = render_report(plan["objective"], state["sections"], native.ledger,
                                               run_id=state.get("run_id", ""), plan=plan,
                                               limitations=state["limitations"])
        except ValueError:
            state["limitations"].append("最终引用检查未通过，已交付原始证据摘录")
            report, references = render_report(plan["objective"], [], native.ledger,
                                               run_id=state.get("run_id", ""), plan=plan,
                                               limitations=state["limitations"])
        artifacts = [{"name": "research-report.md", "media_type": "text/markdown", "content": report}]
        for step_id, progress in state["steps"].items():
            if progress.get("code"):
                artifacts.append({"name": step_id + ".py", "media_type": "text/x-python", "content": progress["code"]})
        cancelled = bool(state.get("cancelled"))
        status = "cancelled" if cancelled else ("partial" if state["limitations"] else "completed")
        if not cancelled and not native.ledger.sources and state["limitations"]:
            status = "failed"
        state.update(report_markdown=report, references=references, artifacts=artifacts, status=status)
        native._save()
        if not cancelled:
            self._events.token_chunks(report)

    # ----------------------------------------------------------------- nodes

    def plan_node(self, state):
        plan = state.get("plan")
        if plan:
            return {"plan": validate_research_plan(plan)}
        return {"plan": self._native.plan(
            state["message"], use_rag=bool(state.get("use_rag")),
            cancel_token=self._token, on_event=self._on_event,
        )}

    def review_node(self, state):
        decision = state.get("review")
        if decision is not None:
            return {}
        applied = self._apply_review(interrupt({"plan": state["plan"], "version": 1}), state["plan"])
        update = {"review": applied}
        if applied["action"] == "reject":
            update["rejected"] = True
        elif "plan" in applied:
            update["plan"] = applied["plan"]
        return update

    def _apply_review(self, decision, plan):
        """Approve/edit/reject with CAS semantics matching the ledger review API."""
        if not isinstance(decision, dict):
            raise ValueError("Plan review decision must be an object")
        action = decision.get("action")
        if action not in {"approve", "edit", "reject"}:
            raise ValueError("Unknown plan review action")
        version = decision.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise ValueError("Plan changed or is no longer awaiting review; reload before reviewing")
        if action == "reject":
            return {"action": "reject", "version": version}
        if action == "edit":
            submitted = decision.get("plan")
            if submitted is None and decision.get("steps") is not None:
                submitted = {**plan, "steps": decision["steps"]}
            if submitted is None:
                raise ValueError("An edited plan or steps is required")
            plan = validate_research_plan(submitted)
        return {"action": action, "version": version, "plan": plan}

    def after_review(self, state):
        return END if state.get("rejected") else "dispatch"

    def dispatch_node(self, state):
        return {}

    def fan_out(self, state):
        """Send one branch per ready research step; otherwise route code/report."""
        plan, progress = state["plan"], self._native.state["steps"]
        research = ready_steps(plan, progress, "research")
        if research:
            return [Send("research", {"plan": plan, "step": step}) for step in research]
        if ready_steps(plan, progress, "code"):
            return "code"
        return "report"

    def research_node(self, incoming):
        self._run_step(
            incoming["plan"], incoming["step"],
            lambda plan, step, progress: self._native._research(plan, step, progress, self._use_rag),
        )
        return {}

    def code_node(self, state):
        plan = state["plan"]
        for step in ready_steps(plan, self._native.state["steps"], "code"):
            self._run_step(plan, step, self._native._code)
        return {"data": self._snapshot()}

    def report_node(self, state):
        plan = state["plan"]
        for step in ready_steps(plan, self._native.state["steps"], "write"):
            self._run_step(plan, step, self._native._write)
        return {"data": self._snapshot()}

    def after_report(self, state):
        plan, progress = state["plan"], self._native.state["steps"]
        if not pending_steps(plan, progress):
            self._finalize(plan)
            return END
        if not any(ready_steps(plan, progress, kind) for kind in ("research", "code", "write")):
            raise ValueError("Invalid plan dependency order")
        return "dispatch"

    def _run_step(self, plan, step, runner):
        """Per-step lifecycle, event parity and error policy of the native engine."""
        native = self._native
        with self._step_lock:
            progress = native.state["steps"].setdefault(step["id"], {"id": step["id"], "kind": step["kind"]})
            if progress.get("finished"):
                return
            native._check()
            self._emit("node_start", {"id": step["id"], "kind": step["kind"], "title": step["title"]})
            progress["status"] = "running"
            try:
                runner(plan, step, progress)
                if progress["status"] == "running":
                    progress["status"] = "completed"
            except BudgetExceeded as exc:
                native.state["limitations"].append(str(exc))
                progress["status"] = "budget_exhausted"
                native.state.pop("inflight_timeout_seconds", None)
            except InterruptedError:
                raise
            except Exception as exc:
                native._check()  # Lost leases/cancellations must not enter the next step.
                detail = native._error_detail(exc)
                progress.update(status="failed", error=detail)
                native.state["limitations"].append(f"{step['title']}：{progress['error']}")
                native.state.pop("inflight_timeout_seconds", None)
            progress["finished"] = True
            native._save()
            self._emit("node_done", {"id": step["id"], "kind": step["kind"], "status": progress["status"]})
