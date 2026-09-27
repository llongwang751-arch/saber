"""LangGraph research backend: review states, parallel steps, recovery and SSE parity."""

import copy
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from config.config import APIConfig, default_config
from internal.agent.plan_contracts import validate_research_plan
from internal.agent.run_scheduler import NativeRunService
from internal.research import ResearchEngine, ResearchLimits
from internal.research_graph import LangGraphResearchEngine

PLAN = {"objective": "Compare Alpha deployment constraints", "steps": [
    {"id": "research", "title": "Find requirements", "kind": "research", "guidance": "Find deployment requirements",
     "depends_on": []},
    {"id": "report", "title": "Write evidence report", "kind": "write", "depends_on": ["research"]},
]}
ONE = {"url": "https://example.org/alpha?utm_source=demo", "title": "Alpha specification",
       "raw_content": "Alpha requires Python 3.11. Alpha runs on Linux."}
TWO = {"url": "https://example.org/beta", "title": "Beta specification",
       "raw_content": "Production execution requires an isolated Docker sandbox."}


class ScriptedLLM:
    """An explicit fixture: no mock generation is wired into production providers."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, system_prompt=""):
        self.calls.append((system_prompt, json.loads(messages[0].content)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response) if isinstance(response, dict) else response


class KeyedLLM:
    """Order-independent fixture for parallel branches: responses keyed by payload shape."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def chat(self, messages, system_prompt=""):
        payload = json.loads(messages[0].content)
        self.calls.append(payload)
        response = self.responder(payload)
        return json.dumps(response) if isinstance(response, dict) else response


def assessment(source="S1", quote="Alpha requires Python 3.11.", sufficient=True, queries=None):
    return {"findings": [{"source_id": source, "quote": quote, "claim": quote}],
            "gaps": [] if sufficient else ["Need isolation evidence"], "queries": queries or [],
            "sufficient": sufficient}


def is_plan_call(payload):
    return "available_tools" in payload


def make_engine(llm, *, search=None, run_id="run-1", tmp_path=None, **kwargs):
    return LangGraphResearchEngine(
        llm, search=search or (lambda query, *, token=None: [ONE]),
        checkpoint_path=tmp_path / "checkpoints.sqlite3", run_id=run_id, **kwargs,
    )


def scripted_responses():
    return [
        {"queries": ["Alpha requirements"]},
        assessment(),
        {"sections": ["Requirements"]},
        "Python 3.11 is required [S1].",
    ]


def test_plan_interrupts_for_review_and_approval_executes_straight_through(tmp_path):
    llm = ScriptedLLM([PLAN, *scripted_responses()])
    events, checkpoints = [], []
    engine = make_engine(llm, tmp_path=tmp_path, run_id="run-1")
    plan = engine.plan("Compare Alpha deployment constraints")
    assert plan == validate_research_plan(PLAN)
    # The graph pauses for review; the ledger (pause_for_plan) owns plan_created/done.
    assert all(name not in {"plan_created", "done"} for name, _ in events)
    result = engine.execute(plan, run_id="run-1", conversation_id="c1",
                            on_event=lambda name, data: events.append((name, data)),
                            checkpoint=checkpoints.append)
    assert result.status == "completed" and result.to_dict()["success"] is True
    assert [name for name, _ in events] == [
        "node_start", "research_round", "source_found", "research_round",
        "node_done", "node_start", "node_done", "token",
    ]
    assert events[1][1]["status"] == "searching" and events[3][1]["status"] == "converged"
    assert result.sources[0]["url"] == "https://example.org/alpha"
    assert "[1](#source-1)" in result.report_markdown
    assert sum(name == "token" for name, _ in events) >= 1
    assert checkpoints and checkpoints[-1]["artifacts"][0]["content"] == result.report_markdown
    assert checkpoints[-1]["graph_thread_id"] == "research-run-1"


def test_execute_applies_edited_plan_to_graph_state_instead_of_interrupted_plan(tmp_path):
    llm = ScriptedLLM([PLAN, *scripted_responses()])
    events = []
    engine = make_engine(llm, tmp_path=tmp_path, run_id="run-1")
    plan = engine.plan("Compare Alpha deployment constraints")
    edited = copy.deepcopy(plan)
    edited["steps"][0]["guidance"] = "Only official deployment requirements"
    edited["steps"][0]["title"] = "Review official requirements"
    result = engine.execute(edited, run_id="run-1", on_event=lambda name, data: events.append((name, data)))
    assert result.status == "completed"
    # The edited steps reached the graph nodes: guidance drives queries, title drives node_start.
    queries_payload = next(payload for _, payload in llm.calls if "queries" not in payload and "step" in payload)
    assert queries_payload["step"]["guidance"] == "Only official deployment requirements"
    node_start = next(data for name, data in events if name == "node_start")
    assert node_start["title"] == "Review official requirements"


def test_reject_terminates_the_graph_without_executing_research(tmp_path):
    llm = ScriptedLLM([PLAN])
    engine = make_engine(
        llm, search=lambda *a, **kw: pytest.fail("rejected plan must not search"), tmp_path=tmp_path, run_id="run-1",
    )
    plan = engine.plan("Compare Alpha deployment constraints")
    assert len(llm.calls) == 1
    values = engine.review({"action": "reject", "version": 1})
    assert values["rejected"] is True
    assert len(llm.calls) == 1 and not engine._native.ledger.sources
    with pytest.raises(ValueError, match="rejected"):
        engine.execute(plan, run_id="run-1")


def test_stale_review_version_fails_compare_and_swap(tmp_path):
    llm = ScriptedLLM([PLAN, *scripted_responses()])
    engine = make_engine(llm, tmp_path=tmp_path, run_id="run-1")
    engine.plan("Compare Alpha deployment constraints")
    with pytest.raises(ValueError, match="no longer awaiting review"):
        engine.review({"action": "approve", "version": 2})
    # A delivered decision is sticky (one resume per interrupt): retries re-evaluate
    # the same value, so a reviewer cannot silently swap decisions on one version.
    with pytest.raises(ValueError, match="no longer awaiting review"):
        engine.review({"action": "reject", "version": 1})
    # A fresh thread resumes normally with a CAS-clean decision.
    engine = make_engine(ScriptedLLM(scripted_responses()), tmp_path=tmp_path, run_id="run-2")
    result = engine.execute(validate_research_plan(PLAN), run_id="run-2")
    assert result.status == "completed"


def test_parallel_research_steps_fan_out_one_send_branch_per_step(tmp_path):
    parallel = {"objective": "Compare Alpha and Beta deployments", "steps": [
        {"id": "ra", "title": "Alpha research", "kind": "research", "guidance": "alpha requirements", "depends_on": []},
        {"id": "rb", "title": "Beta research", "kind": "research", "guidance": "beta requirements", "depends_on": []},
        {"id": "report", "title": "Write evidence report", "kind": "write", "depends_on": ["ra", "rb"]},
    ]}

    def responder(payload):
        if is_plan_call(payload):
            return parallel
        if "previous_evidence" in payload:
            # Both quotes are offered so each branch validates its own source deterministically.
            return {"findings": [
                        {"source_id": "S1", "quote": "Alpha requires Python 3.11.", "claim": "alpha"},
                        {"source_id": "S2", "quote": TWO["raw_content"], "claim": "beta"}],
                    "gaps": [], "queries": [], "sufficient": True}
        if "section" in payload:
            return "Alpha needs Python 3.11 [S1]. Beta needs isolation [S2]."
        if "evidence" in payload:
            return {"sections": ["Requirements"]}
        return {"queries": [payload["step"]["guidance"]]}

    def search(query, *, token=None):
        if "alpha" in query.casefold():
            return [ONE]
        return [TWO]

    llm = KeyedLLM(responder)
    events = []
    engine = make_engine(llm, search=search, tmp_path=tmp_path, run_id="run-parallel")
    # No plan() phase: execute rebuilds the graph from the approved plan (ledger is truth).
    result = engine.execute(parallel, run_id="run-parallel",
                            on_event=lambda name, data: events.append((name, data)))
    assert result.status == "completed"
    assert not any(is_plan_call(payload) for payload in llm.calls)
    started = [data["id"] for name, data in events if name == "node_start"]
    assert sorted(started) == ["ra", "rb", "report"]
    assert {source["url_or_doc_id"] for source in result.sources} == {
        "https://example.org/alpha", "https://example.org/beta",
    }
    assert [data["id"] for name, data in events if name == "node_done"] == started


def test_crash_recovery_resumes_from_checkpoint_without_replanning(tmp_path):
    llm = KeyedLLM(lambda payload: PLAN if is_plan_call(payload) else scripted_step(payload))
    engine = make_engine(llm, tmp_path=tmp_path, run_id="run-1")
    plan = engine.plan("Compare Alpha deployment constraints")
    snapshots = []

    def crashing_checkpoint(state):
        snapshots.append(copy.deepcopy(state))
        if len(snapshots) >= 3:
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError, match="simulated process death"):
        engine.execute(plan, run_id="run-1", checkpoint=crashing_checkpoint)
    last_good = snapshots[-2]
    assert last_good["graph_thread_id"] == "research-run-1"

    recovery = make_engine(
        KeyedLLM(scripted_step), search=lambda query, *, token=None: [ONE],
        tmp_path=tmp_path, run_id="run-2", parent_run_id="run-1",
    )
    events = []
    result = recovery.execute(plan, run_id="run-2", state=last_good,
                              on_event=lambda name, data: events.append((name, data)),
                              checkpoint=lambda state: None)
    assert result.status == "completed"
    assert len(result.sources) == 1 and "[1](#source-1)" in result.report_markdown
    assert not any(is_plan_call(payload) for payload in recovery._native.llm.calls)
    assert [name for name, _ in events][0] == "node_start"


def scripted_step(payload):
    if "previous_evidence" in payload:
        return assessment()
    if "section" in payload:
        return "Python 3.11 is required [S1]."
    if "evidence" in payload:
        return {"sections": ["Requirements"]}
    return {"queries": ["Alpha requirements"]}


def test_sse_event_sequence_parity_with_native_engine(tmp_path, monkeypatch):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr("internal.research.reporting.datetime", Fixed)

    def search(query, *, token=None):
        if "security" in query:
            return [{**ONE, "url": "https://example.org/alpha#duplicate"}, TWO]
        return [{k: v for k, v in ONE.items() if k != "raw_content"}]

    def read(url, *, token=None):
        return ONE["raw_content"]

    native = ResearchEngine(ScriptedLLM(scripted_responses()), search=search, reader=read)
    native_events = []
    native_result = native.execute(PLAN, run_id="run-1",
                                   on_event=lambda name, data: native_events.append((name, data)))

    graph = make_engine(ScriptedLLM([PLAN, *scripted_responses()]), search=search, reader=read,
                        tmp_path=tmp_path, run_id="run-1")
    graph_events = []
    graph_plan = graph.plan("Compare Alpha deployment constraints")
    graph_result = graph.execute(graph_plan, run_id="run-1",
                                 on_event=lambda name, data: graph_events.append((name, data)))

    assert graph_plan == validate_research_plan(PLAN)
    assert native_events == graph_events
    assert native_result.status == graph_result.status == "completed"
    assert native_result.report_markdown == graph_result.report_markdown
    assert native_result.usage["llm_calls"] == graph_result.usage["llm_calls"]
    assert [s["source_id"] for s in native_result.sources] == [s["source_id"] for s in graph_result.sources]


def test_engine_selection_keeps_native_as_default(tmp_path):
    run = {"run_id": "r1", "conversation_id": "c1", "parent_run_id": ""}
    service_stub = SimpleNamespace(_research_engine_factory=None)

    default_agent = SimpleNamespace(cfg=SimpleNamespace(), llm=object())
    assert isinstance(NativeRunService._research_engine(service_stub, default_agent, run), ResearchEngine)

    langgraph_agent = SimpleNamespace(cfg=SimpleNamespace(research_engine="langgraph"), llm=object())
    engine = NativeRunService._research_engine(service_stub, langgraph_agent, run)
    assert isinstance(engine, LangGraphResearchEngine)

    marker = object()
    factory_stub = SimpleNamespace(_research_engine_factory=lambda agent: marker)
    assert NativeRunService._research_engine(factory_stub, default_agent, run) is marker
    # An injected factory keeps native runs patchable even under langgraph config.
    assert NativeRunService._research_engine(factory_stub, langgraph_agent, run) is marker


def test_research_engine_config_defaults_validation_and_override(tmp_path, monkeypatch):
    monkeypatch.setattr("config.config._load_dotenv_best_effort", lambda: None)
    monkeypatch.delenv("AGI_RESEARCH_ENGINE", raising=False)
    assert APIConfig().research_engine == "langgraph"

    path = tmp_path / "conf.yaml"
    path.write_text("research:\n  engine: langgraph\n", encoding="utf-8")
    assert default_config(str(path)).research_engine == "langgraph"

    monkeypatch.setenv("AGI_RESEARCH_ENGINE", "native")
    assert default_config(str(path)).research_engine == "native"

    monkeypatch.delenv("AGI_RESEARCH_ENGINE")
    path.write_text("research:\n  engine: faster\n", encoding="utf-8")
    with pytest.raises(ValueError, match="research.engine"):
        default_config(str(path))


def test_budget_exhaustion_preserves_retrieved_evidence_on_the_graph(tmp_path):
    llm = ScriptedLLM([PLAN, {"queries": ["requirements"]}])
    engine = make_engine(llm, limits=ResearchLimits(max_llm_calls=1), tmp_path=tmp_path, run_id="run-1")
    result = engine.execute(validate_research_plan(PLAN), run_id="run-1")
    assert result.status == "partial"
    assert result.usage["llm_calls"] == 1 and len(result.sources) == 1
    assert any("预算耗尽" in item for item in result.limitations)


def wait_status(service, owner, run_id, statuses, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get(owner, run_id)
        if run["status"] in statuses:
            return run
        time.sleep(0.005)
    raise AssertionError(f"Run did not reach {statuses}: {run}")


def test_run_service_dispatches_langgraph_engine_through_shared_ledger(tmp_path, monkeypatch):
    """research.engine: langgraph uses the graph backend with the native review lifecycle."""
    from internal.agent.run_service import NativeRunService as Service

    context, executions = {}, []

    class StubGraphEngine:
        def plan(self, message, **kwargs):
            return copy.deepcopy(PLAN)

        def execute(self, plan, **kwargs):
            executions.append(copy.deepcopy(plan))
            source = {"source_id": "S1", "url": "https://example.org/alpha", "title": "Alpha"}
            kwargs["checkpoint"]({"sources": [source], "steps": {"research": {"status": "completed"}}})
            kwargs["on_event"]("source_found", source)
            kwargs["on_event"]("token", {"content": "Evidence [1]"})
            return SimpleNamespace(
                to_dict=lambda: {"answer": "Evidence [1]", "report_markdown": "Evidence [1]",
                                 "sources": [source], "artifacts": [{"name": "report.md"}], "status": "completed"},
            )

    monkeypatch.setattr(
        LangGraphResearchEngine, "from_agent",
        classmethod(lambda cls, agent, **kwargs: (context.update(kwargs), StubGraphEngine())[1]),
    )
    agent = SimpleNamespace(user_id="alice", cfg=SimpleNamespace(research_engine="langgraph"))
    service = Service(tmp_path / "runs.db")
    try:
        run = service.create("alice", agent, "Compare Alpha deployment constraints", mode="research")
        assert wait_status(service, "alice", run["run_id"], {"awaiting_plan_review"})["plan_version"] == 1
        assert context["run_id"] == run["run_id"] and context["conversation_id"] == run["conversation_id"]
        service.review_plan("alice", agent, run["run_id"], action="approve", version=1)
        done = wait_status(service, "alice", run["run_id"], {"completed"})
        assert len(executions) == 1
        assert done["sources"][0]["source_id"] == "S1"
        # Identical ledger vocabulary to the native engine's research lifecycle.
        assert [event["type"] for event in service.events_since("alice", run["run_id"])] == [
            "queued", "started", "plan_created", "plan_review_required",
            "plan_approved", "started", "source_found", "token", "done",
        ]
    finally:
        service.close()
