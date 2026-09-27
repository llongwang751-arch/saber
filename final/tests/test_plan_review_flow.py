"""Research review is a durable handoff, never a blocked worker thread."""

import copy
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from internal.agent.plan_contracts import validate_research_plan
from internal.agent.run_api import create_native_run_router
from internal.agent.run_service import NativeRunService, RunConflict, RunNotFound


PLAN = {
    "objective": "Compare storage engines using primary evidence",
    "constraints": ["Cite sources"],
    "steps": [
        {"id": "research", "title": "Find evidence", "kind": "research"},
        {"id": "report", "title": "Write report", "kind": "write", "depends_on": ["research"]},
    ],
}


class FakeResearchEngine:
    def __init__(self):
        self.executed = []

    def plan(self, message, **kwargs):
        return copy.deepcopy(PLAN)

    def execute(self, plan, *, checkpoint, on_event, **kwargs):
        self.executed.append(copy.deepcopy(plan))
        source = {"source_id": "s1", "url": "https://example.org/paper", "title": "Paper"}
        checkpoint({"sources": [source], "steps": {"research": {"status": "completed"}}})
        on_event({"type": "source_found", "data": source})
        return {
            "answer": "Evidence [1]", "report_markdown": "Evidence [1]", "sources": [source],
            "artifacts": [{"name": "report.md", "content": "Evidence [1]"}],
        }


class FakeAgent:
    user_id = "alice"

    def process_stream(self, message, opts, on_event, **kwargs):
        return {"answer": "chat can still run"}


def wait_status(service, run_id, statuses):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        run = service.get("alice", run_id)
        if run["status"] in statuses:
            return run
        time.sleep(0.005)
    raise AssertionError(f"Run did not reach {statuses}: {run}")


def make_service(path, engine, **kwargs):
    return NativeRunService(path, research_engine_factory=lambda agent: engine, **kwargs)


def research(service, agent=None, **kwargs):
    run = service.create("alice", agent or FakeAgent(), "Compare storage engines", mode="research", **kwargs)
    return wait_status(service, run["run_id"], {"awaiting_plan_review"})


def test_plan_wait_releases_capacity_and_approval_executes_exact_reviewed_plan(tmp_path):
    engine = FakeResearchEngine()
    service = make_service(tmp_path / "runs.db", engine, max_workers=1, max_active=1)
    try:
        waiting = research(service, conversation_id="research-thread")
        assert waiting["plan_version"] == 1 and not engine.executed
        assert service.summary("alice")["active"] == 0
        chat = service.create("alice", FakeAgent(), "hello", conversation_id="other-thread")
        assert wait_status(service, chat["run_id"], {"completed"})["result"]["response"]["answer"]
        with pytest.raises(RunConflict):
            service.create("alice", FakeAgent(), "overlap", conversation_id="research-thread")
        steps = waiting["plan"]["steps"]
        steps[0]["title"] = "Review only official benchmark measurements"
        edited = service.review_plan("alice", FakeAgent(), waiting["run_id"], action="edit", version=1, steps=steps)
        assert edited["version"] == 2 and edited["status"] == "awaiting_plan_review"
        with pytest.raises(RunConflict):
            service.review_plan("alice", FakeAgent(), waiting["run_id"], action="approve", version=1)
        approved = service.review_plan("alice", FakeAgent(), waiting["run_id"], action="approve", version=2)
        assert approved["review_status"] == "approved"
        done = wait_status(service, waiting["run_id"], {"completed"})
        assert engine.executed[0]["steps"][0]["title"] == steps[0]["title"]
        assert done["sources"][0]["source_id"] == "s1"
        assert done["research_state"]["steps"]["research"]["status"] == "completed"
        assert done["artifacts"][0]["name"] == "report.md"
        assert [e["type"] for e in service.events_since("alice", waiting["run_id"])] == [
            "queued", "started", "plan_created", "plan_review_required", "plan_edited",
            "plan_approved", "started", "source_found", "done",
        ]
    finally:
        service.close()


def test_waiting_plan_survives_worker_shutdown_and_is_reviewable_on_new_worker(tmp_path):
    path = tmp_path / "runs.db"
    engine = FakeResearchEngine()
    first = make_service(path, engine)
    run = research(first)
    cursor = first.events_since("alice", run["run_id"])[-1]["event_id"]
    first.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT worker_id FROM agent_runs WHERE run_id=?", (run["run_id"],)).fetchone()[0] is None
    second = make_service(path, engine)
    try:
        assert second.get("alice", run["run_id"])["status"] == "awaiting_plan_review"
        assert second.events_since("alice", run["run_id"], cursor) == []
        second.review_plan("alice", FakeAgent(), run["run_id"], action="approve", version=1)
        assert wait_status(second, run["run_id"], {"completed"})["sources"]
        assert len(engine.executed) == 1
    finally:
        second.close()


@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_reject_and_cancel_waiting_plan_are_terminal_without_execution(tmp_path, action):
    engine = FakeResearchEngine()
    service = make_service(tmp_path / "runs.db", engine)
    try:
        run = research(service)
        if action == "reject":
            service.review_plan("alice", FakeAgent(), run["run_id"], action=action, version=1)
        else:
            service.cancel_conversation("alice", run["conversation_id"])
        done = service.get("alice", run["run_id"])
        assert done["status"] == "cancelled" and not engine.executed
        with pytest.raises(RunConflict):
            service.review_plan("alice", FakeAgent(), run["run_id"], action="approve", version=1)
        assert sum(e["type"] == "done" for e in service.events_since("alice", run["run_id"])) == 1
    finally:
        service.close()


def test_approval_compare_and_swap_across_workers_starts_only_once(tmp_path):
    path = tmp_path / "runs.db"
    engine = FakeResearchEngine()
    first, second = make_service(path, engine), make_service(path, engine)
    try:
        run = research(first)
        barrier = threading.Barrier(2)

        def approve(service):
            barrier.wait()
            try:
                return service.review_plan("alice", FakeAgent(), run["run_id"], action="approve", version=1)
            except RunConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(approve, [first, second]))
        assert sum(result is not None for result in results) == 1
        assert wait_status(first, run["run_id"], {"completed"})["plan_status"] == "approved"
        assert len(engine.executed) == 1
    finally:
        first.close()
        second.close()


def test_invalid_plan_edit_rolls_back_and_preserves_version(tmp_path):
    service = make_service(tmp_path / "runs.db", FakeResearchEngine())
    try:
        run = research(service)
        invalid = copy.deepcopy(run["plan"])
        invalid["steps"][0]["depends_on"] = ["report"]
        with pytest.raises(ValueError, match="cycle"):
            service.review_plan("alice", FakeAgent(), run["run_id"], action="edit", version=1, plan=invalid)
        assert service.get_plan("alice", run["run_id"])["version"] == 1
        with pytest.raises(RunNotFound):
            service.get_plan("bob", run["run_id"])
        with pytest.raises(RunConflict):
            service._repository.checkpoint_research("alice", run["run_id"], service._worker_id, {"sources": []})
        assert service._repository.append_event(
            run["run_id"], "alice", "late_worker_event", {}, worker_id=service._worker_id,
        ) == 0
    finally:
        service.close()


def test_cancel_during_planning_cannot_publish_an_approvable_plan(tmp_path):
    started, release = threading.Event(), threading.Event()

    class SlowPlanner(FakeResearchEngine):
        def plan(self, message, **kwargs):
            started.set()
            assert release.wait(2)
            return super().plan(message, **kwargs)

    engine = SlowPlanner()
    service = make_service(tmp_path / "runs.db", engine)
    try:
        run = service.create("alice", FakeAgent(), "topic", mode="research")
        assert started.wait(1)
        service.cancel("alice", run["run_id"])
        release.set()
        assert wait_status(service, run["run_id"], {"cancelled"})["plan"] is None
        assert not engine.executed
    finally:
        release.set()
        service.close()


def test_interrupted_research_resumes_as_one_linked_run_with_fenced_checkpoint(tmp_path):
    path = tmp_path / "runs.db"
    checkpointed, release = threading.Event(), threading.Event()
    resumed_states = []

    class CrashEngine(FakeResearchEngine):
        def execute(self, plan, *, checkpoint, on_event, state, **kwargs):
            if state:
                resumed_states.append(copy.deepcopy(state))
                assert state["steps"]["research"]["finished"] is True
                return {"answer": "Continued from evidence", "sources": state["sources"]}
            checkpoint({
                "steps": {"research": {"finished": True, "status": "completed"}},
                "sources": [{"source_id": "S1", "url": "https://example.org/persisted"}],
            })
            checkpointed.set()
            assert release.wait(3)
            # A departed worker cannot write late events/checkpoints after the
            # new worker has claimed a continuation, even if its tool returns.
            on_event("late_tool_result", {"value": "stale"})
            checkpoint({"sources": []})
            raise AssertionError("stale checkpoint was accepted")

    engine = CrashEngine()
    first = make_service(path, engine)
    parent = research(first)
    first.review_plan("alice", FakeAgent(), parent["run_id"], action="approve", version=1)
    assert checkpointed.wait(1)
    first.close()
    terminal_events = first.events_since("alice", parent["run_id"])
    assert first.get("alice", parent["run_id"])["status"] == "interrupted"
    second = make_service(path, engine)
    try:
        child = second.resume_research("alice", FakeAgent(), parent["run_id"], request_key="resume-one")
        done = wait_status(second, child["run_id"], {"completed"})
        assert done["parent_run_id"] == parent["run_id"]
        assert done["plan"] == parent["plan"] and done["plan_status"] == "approved"
        assert done["sources"][0]["source_id"] == "S1" and len(resumed_states) == 1
        duplicate = second.resume_research("alice", FakeAgent(), parent["run_id"], request_key="resume-one")
        assert duplicate["run_id"] == child["run_id"]
        assert second.resume_research("alice", FakeAgent(), parent["run_id"])["run_id"] == child["run_id"]
        release.set()
        time.sleep(0.05)
        assert second.events_since("alice", parent["run_id"]) == terminal_events
        assert second.get("alice", parent["run_id"])["research_state"]["sources"][0]["source_id"] == "S1"
        assert [e["type"] for e in second.events_since("alice", child["run_id"])] == [
            "queued", "research_resumed", "started", "done",
        ]
        with pytest.raises(RunNotFound):
            second.resume_research("bob", SimpleNamespace(user_id="bob"), parent["run_id"])
        with pytest.raises(RunConflict):
            second.resume_research("alice", FakeAgent(), child["run_id"])
    finally:
        release.set()
        second.close()


def test_research_provider_error_is_actionable_and_redacted(tmp_path):
    class MissingProvider(FakeResearchEngine):
        def plan(self, message, **kwargs):
            raise RuntimeError("Configure a real LLM provider; rejected credential sk-example-secret-123456")

    service = make_service(tmp_path / "runs.db", MissingProvider())
    try:
        created = service.create("alice", FakeAgent(), "topic", mode="research")
        done = wait_status(service, created["run_id"], {"failed"})
        assert "Configure a real LLM provider" in done["result"]["reason"]
        assert "sk-example-secret" not in done["result"]["reason"]
        assert "REDACTED" in done["result"]["reason"]
    finally:
        service.close()


def test_real_engine_stops_before_next_provider_when_durable_lease_expires(tmp_path):
    from internal.research import ResearchEngine

    path = tmp_path / "runs.db"
    searches = []

    class ExpiringLLM:
        def chat(self, messages, **kwargs):
            # Simulate an unresponsive heartbeat while the current provider is
            # in flight. The next provider must consult the durable fence even
            # before this process's heartbeat loop notices the expired lease.
            with sqlite3.connect(path) as db:
                db.execute("UPDATE agent_run_workers SET heartbeat_at=0")
            return '{"queries":["must never reach search"]}'

    class Engine(ResearchEngine):
        def plan(self, message, **kwargs):
            return copy.deepcopy(PLAN)

    service = NativeRunService(
        path, heartbeat_interval=0.1,
        research_engine_factory=lambda agent: Engine(ExpiringLLM(), search=lambda *args, **kwargs: searches.append(args)),
    )
    try:
        run = research(service)
        service.review_plan("alice", FakeAgent(), run["run_id"], action="approve", version=1)
        assert wait_status(service, run["run_id"], {"interrupted"})["status"] == "interrupted"
        assert searches == []
        assert not any(event["type"] == "source_found" for event in service.events_since("alice", run["run_id"]))
    finally:
        service.close()


def test_http_review_owner_isolation_validation_and_last_event_id_replay(tmp_path):
    service = make_service(tmp_path / "runs.db", FakeResearchEngine())
    app = FastAPI()
    app.state.native_run_service = service
    app.state.agent_registry = SimpleNamespace(get=lambda owner: FakeAgent())
    app.include_router(create_native_run_router())

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.user = {"id": request.headers.get("x-test-user", "alice")}
        return await call_next(request)

    try:
        with TestClient(app) as client:
            created = client.post("/api/agent-runs", json={"message": "topic", "mode": "research"})
            assert created.status_code == 200
            run_id = created.json()["run_id"]
            wait_status(service, run_id, {"awaiting_plan_review"})
            plan = client.get(f"/api/agent-runs/{run_id}/plan").json()
            assert plan["version"] == 1
            events = client.get(f"/api/agent-runs/{run_id}/events").json()
            stream = client.get(f"/api/agent-runs/{run_id}/stream", headers={"Last-Event-ID": str(events[1]["event_id"])})
            assert "event: plan_created" in stream.text and "event: queued" not in stream.text
            assert "event: done" not in stream.text
            empty = client.get(f"/api/agent-runs/{run_id}/stream", headers={"Last-Event-ID": str(events[-1]["event_id"])})
            assert empty.text == ""
            assert client.post(f"/api/agent-runs/{run_id}/plan/review", json={"action": "approve"}).status_code == 422
            foreign = client.post(f"/api/agent-runs/{run_id}/plan/review", json={"action": "approve", "version": 1},
                                  headers={"x-test-user": "bob"})
            assert foreign.status_code == 404
            edited = copy.deepcopy(plan["plan"])
            edited["steps"][0]["guidance"] = "Only use independently reproducible results"
            update = client.post(f"/api/agent-runs/{run_id}/plan/review", json={"action": "edit", "version": 1, "plan": edited})
            assert update.status_code == 200 and update.json()["version"] == 2
            conflict = client.post(f"/api/agent-runs/{run_id}/plan/review", json={"action": "approve", "version": 1})
            assert conflict.status_code == 409
            assert client.post(f"/api/agent-runs/{run_id}/plan/review", json={"action": "approve", "version": 2}).status_code == 200
            wait_status(service, run_id, {"completed"})
            replay = client.get(f"/api/agent-runs/{run_id}/stream", headers={"Last-Event-ID": str(events[-1]["event_id"])})
            assert "event: plan_approved" in replay.text and "event: source_found" in replay.text and "event: done" in replay.text
    finally:
        service.close()


@pytest.mark.parametrize("steps", [
    [],
    [{"id": "a", "title": "A", "kind": "research", "depends_on": ["missing"]}],
    [{"id": "a", "title": "A", "kind": "research"}, {"id": "a", "title": "B", "kind": "write"}],
    [{"id": "a", "title": "A", "kind": "shell"}],
    [{"id": "a", "title": "A", "kind": "research", "tool_policy": ["shell"]}],
    [{"id": "a", "title": "A", "kind": "research", "tool_policy": ["exec_command"]}],
    [{"id": "a", "title": "A", "kind": "write", "tool_policy": ["search_web"]}],
    [{"id": "a", "title": "A", "kind": "code", "tool_policy": ["rag_search"]}],
])
def test_plan_schema_rejects_unexecutable_graphs(steps):
    with pytest.raises(ValueError):
        validate_research_plan({"objective": "Research", "steps": steps})
