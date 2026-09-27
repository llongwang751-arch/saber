"""Saber-native durable run lifecycle, replay, ownership and cancellation."""

import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from internal.agent.agent import Response
from internal.agent.run_api import create_native_run_router
from internal.agent.run_service import NativeRunService, RunCapacityExceeded, RunConflict, RunNotFound


class FakeAgent:
    def __init__(self, owner, *, blocked=False):
        self.user_id = owner
        self.started = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def process_stream(self, message, opts, on_event, *, cancel_token):
        on_event({"type": "route", "data": {"mode": "react"}})
        self.started.set()
        while not self.release.wait(0.01):
            if cancel_token.is_cancelled():
                return Response(query=message, answer="已取消", interrupted=True)
        on_event({"type": "token", "data": {"content": "OK"}})
        on_event({"type": "done", "data": {"answer": "OK"}})
        return Response(query=message, answer="OK")


def _wait(service, owner, run_id):
    for _ in range(100):
        run = service.get(owner, run_id)
        if run["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_native_run_persists_events_and_replays_after_reopen(tmp_path):
    path = tmp_path / "runs.db"
    service = NativeRunService(path)
    agent = FakeAgent("alice")
    run = service.create("alice", agent, "hello", conversation_id="thread-1")
    done = _wait(service, "alice", run["run_id"])
    assert done["status"] == "completed"
    assert done["result"]["response"]["answer"] == "OK"
    events = service.events_since("alice", run["run_id"])
    assert [event["type"] for event in events] == ["queued", "started", "route", "token", "done"]
    assert service.events_since("alice", run["run_id"], events[2]["event_id"])[0]["type"] == "token"
    with pytest.raises(RunNotFound):
        service.get("bob", run["run_id"])
    service.close()
    reopened = NativeRunService(path)
    assert reopened.get("alice", run["run_id"])["status"] == "completed"
    assert len(reopened.events_since("alice", run["run_id"])) == 5
    reopened.close()


def test_active_conversation_conflict_and_cancel(tmp_path):
    service = NativeRunService(tmp_path / "runs.db")
    agent = FakeAgent("alice", blocked=True)
    run = service.create("alice", agent, "work", conversation_id="thread-1")
    assert agent.started.wait(1)
    with pytest.raises(RunConflict):
        service.create("alice", agent, "overlap", conversation_id="thread-1")
    with pytest.raises(RunNotFound):
        service.cancel("bob", run["run_id"])
    cancelled = service.cancel("alice", run["run_id"])
    assert cancelled["status"] in {"cancelling", "cancelled"}
    assert _wait(service, "alice", run["run_id"])["status"] == "cancelled"
    assert service.events_since("alice", run["run_id"])[-1]["type"] == "done"
    service.close()


def test_restart_marks_orphaned_work_interrupted_without_replay(tmp_path):
    path = tmp_path / "runs.db"
    service = NativeRunService(path)
    service.close()
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO agent_runs
            (run_id, owner_id, conversation_id, message, use_rag, status, created_at, updated_at)
            VALUES ('old-run','alice','thread-1','write file',0,'running',1,1)"""
        )
    reopened = NativeRunService(path)
    run = reopened.get("alice", "old-run")
    assert run["status"] == "interrupted"
    assert run["result"]["reason"].startswith("worker_lost")
    assert [event["type"] for event in reopened.events_since("alice", "old-run")] == ["done"]
    reopened.close()


def test_bounded_admission_and_agent_interruption(tmp_path):
    service = NativeRunService(tmp_path / "runs.db", max_workers=1, max_active=1)
    agent = FakeAgent("alice", blocked=True)
    first = service.create("alice", agent, "work", conversation_id="thread-1")
    assert agent.started.wait(1)
    with pytest.raises(RunCapacityExceeded):
        service.create("alice", agent, "later", conversation_id="thread-2")
    agent.release.set()
    assert _wait(service, "alice", first["run_id"])["status"] == "completed"

    class InterruptedAgent(FakeAgent):
        def process_stream(self, message, opts, on_event, *, cancel_token):
            return Response(query=message, answer="paused", interrupted=True)

    second = service.create("alice", InterruptedAgent("alice"), "pause")
    assert _wait(service, "alice", second["run_id"])["status"] == "interrupted"
    service.close()


def test_two_workers_do_not_interrupt_live_runs_and_can_request_remote_cancel(tmp_path):
    path = tmp_path / "runs.db"
    first = NativeRunService(path, max_workers=1, heartbeat_interval=0.05, stale_after=1)
    agent = FakeAgent("alice", blocked=True)
    run = first.create("alice", agent, "long work", conversation_id="thread-1")
    assert agent.started.wait(1)
    pending = first.create("alice", agent, "queued work", conversation_id="thread-2")
    second = NativeRunService(path, heartbeat_interval=0.05, stale_after=1)
    assert second.get("alice", run["run_id"])["status"] == "running"
    assert second.cancel("alice", pending["run_id"])["status"] == "cancelling"
    assert _wait(first, "alice", pending["run_id"])["status"] == "cancelled"
    assert second.cancel("alice", run["run_id"])["status"] == "cancelling"
    assert _wait(first, "alice", run["run_id"])["status"] == "cancelled"
    assert second.events_since("alice", run["run_id"])[-1]["type"] == "done"
    first.close()
    second.close()


def test_idempotent_creation_and_shutdown_do_not_append_late_events(tmp_path):
    service = NativeRunService(tmp_path / "runs.db")
    agent = FakeAgent("alice", blocked=True)
    run = service.create("alice", agent, "work", request_key="request-1")
    assert agent.started.wait(1)
    duplicate = service.create("alice", agent, "work", request_key="request-1")
    assert duplicate["run_id"] == run["run_id"]
    with pytest.raises(RunConflict):
        service.create("alice", agent, "different", request_key="request-1")
    service.close()
    assert service.get("alice", run["run_id"])["status"] == "interrupted"
    before = len(service.events_since("alice", run["run_id"]))
    agent.release.set()
    time.sleep(0.05)
    assert len(service.events_since("alice", run["run_id"])) == before


def test_recovery_is_a_durable_run_with_graph_events(tmp_path, monkeypatch):
    service = NativeRunService(tmp_path / "runs.db")
    agent = FakeAgent("alice")
    agent.inf = SimpleNamespace(repo=SimpleNamespace(snapshot=SimpleNamespace(
        get=lambda task_id, user_id: {"task_id": task_id} if user_id == "alice" else None,
    )))

    def fake_resume(owner, task_id, conversation_id, *, cancel_token, on_event):
        assert owner is agent and task_id == "checkpoint-1" and conversation_id == "thread-1"
        on_event({"type": "graph_ready", "data": {"nodes": {"step-1": {"status": "done"}}}})
        return {"task_id": task_id, "status": "completed", "result": "recovered answer"}

    monkeypatch.setattr("internal.agent.recovery.resume_task", fake_resume)
    run = service.create_recovery("alice", agent, "checkpoint-1", conversation_id="thread-1",
                                  request_key="recover-1")
    done = _wait(service, "alice", run["run_id"])
    assert done["kind"] == "recovery" and done["task_id"] == "checkpoint-1"
    assert done["result"]["response"]["answer"] == "recovered answer"
    assert [event["type"] for event in service.events_since("alice", run["run_id"])] == [
        "queued", "started", "graph_ready", "done",
    ]
    assert service.create_recovery("alice", agent, "checkpoint-1", conversation_id="thread-1",
                                   request_key="recover-1")["run_id"] == run["run_id"]
    service.close()


def test_migrates_existing_run_ledger_without_losing_history(tmp_path):
    path = tmp_path / "runs.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            message TEXT NOT NULL, use_rag INTEGER NOT NULL, status TEXT NOT NULL,
            result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        db.execute("""INSERT INTO agent_runs VALUES (
            'previous','alice','thread-1','hello',0,'completed','{"status":"completed"}',1,1)""")
    service = NativeRunService(path)
    previous = service.get("alice", "previous")
    assert previous["status"] == "completed" and previous["kind"] == "chat"
    assert previous["result"]["status"] == "completed"
    service.close()


def test_concurrent_queue_drains_with_one_terminal_event_per_run(tmp_path):
    service = NativeRunService(tmp_path / "runs.db", max_workers=4, max_active=64)

    class SlowAgent(FakeAgent):
        def process_stream(self, message, opts, on_event, *, cancel_token):
            time.sleep(0.01)
            return super().process_stream(message, opts, on_event, cancel_token=cancel_token)

    agent = SlowAgent("alice")
    runs = [service.create("alice", agent, f"work-{i}", conversation_id=f"thread-{i}") for i in range(40)]
    for run in runs:
        assert _wait(service, "alice", run["run_id"])["status"] == "completed"
        events = service.events_since("alice", run["run_id"])
        assert sum(event["type"] == "done" for event in events) == 1
    assert len(service.list("alice", limit=50)) == 40
    assert all("result" not in item for item in service.list("alice", limit=50))
    assert service.summary("alice")["counts"]["completed"] == 40
    service.close()


def test_http_routes_stream_and_owner_boundary(tmp_path, monkeypatch):
    service = NativeRunService(tmp_path / "runs.db")
    agent = FakeAgent("alice")
    agent.inf = SimpleNamespace(repo=SimpleNamespace(snapshot=SimpleNamespace(
        get=lambda task_id, user_id: {"task_id": task_id} if task_id == "checkpoint-1" and user_id == "alice" else None,
    )))
    monkeypatch.setattr("internal.agent.recovery.resume_task", lambda *_args, **_kwargs: {
        "task_id": "checkpoint-1", "status": "completed", "result": "restored",
    })
    app = FastAPI()
    app.state.native_run_service = service
    app.state.agent_registry = type("Registry", (), {"get": lambda self, _id: agent})()
    app.include_router(create_native_run_router())

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.user = {"id": request.headers.get("x-test-user", "alice")}
        return await call_next(request)

    with TestClient(app) as client:
        response = client.post("/api/agent-runs", json={"message": "hello", "conversation_id": "thread-1"},
                               headers={"Idempotency-Key": "http-request-1"})
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        duplicate = client.post("/api/agent-runs", json={"message": "hello", "conversation_id": "thread-1"},
                                headers={"Idempotency-Key": "http-request-1"})
        assert duplicate.json()["run_id"] == run_id
        _wait(service, "alice", run_id)
        assert client.get(f"/api/agent-runs/{run_id}").json()["status"] == "completed"
        assert client.get("/api/agent-runs/summary").json()["counts"]["completed"] == 1
        events = client.get(f"/api/agent-runs/{run_id}/events").json()
        assert events[-1]["type"] == "done"
        stream = client.get(f"/api/agent-runs/{run_id}/stream", headers={"Last-Event-ID": str(events[1]["event_id"])})
        assert stream.status_code == 200
        assert "event: done" in stream.text and "event: queued" not in stream.text
        other = client.get(f"/api/agent-runs/{run_id}", headers={"x-test-user": "bob"})
        assert other.status_code == 404
        recovery = client.post("/api/agent-runs/recover", json={
            "task_id": "checkpoint-1", "conversation_id": "thread-2",
        })
        assert recovery.status_code == 200
        assert _wait(service, "alice", recovery.json()["run_id"])["result"]["response"]["answer"] == "restored"
        missing = client.post("/api/agent-runs/recover", json={
            "task_id": "missing", "conversation_id": "thread-3",
        })
        assert missing.status_code == 404
    service.close()
