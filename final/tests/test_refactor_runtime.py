"""Cross-layer acceptance: durable chat, transaction failure, leases and lifecycle."""

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.config import APIConfig
from internal.agent.cancel import CancelToken
from internal.agent.contracts import Response
from internal.agent.run_contracts import RunConflict
from internal.agent.run_repository import SQLiteRunRepository
from internal.agent.run_service import NativeRunService
from internal.application.bootstrap import build_deps
from internal.fastapi_compat import app_lifespan, register_shutdown
from internal.handler.handler import setup_routes


class EchoAgent:
    user_id = "alice"

    def process_with_options(self, message, opts, execution_context=None, cancel_token=None):
        return Response(query=message, answer="echo:" + message)

    def process_stream(self, message, opts, on_event, execution_context=None, cancel_token=None):
        on_event({"type": "token", "data": {"content": "echo:" + message}})
        result = self.process_with_options(message, opts, execution_context, cancel_token)
        on_event({"type": "done", "data": {"answer": result.answer}})
        return result


def test_sync_stream_and_background_share_owned_run_history(tmp_path, monkeypatch):
    monkeypatch.setenv("AGI_EVAL_DATABASE_URL", "sqlite:///" + (tmp_path / "application.db").as_posix())
    app = setup_routes(EchoAgent(), SimpleNamespace(), APIConfig(), auth_required=False)
    with TestClient(app) as client:
        sync = client.post("/api/chat", json={"message": "sync", "conversation_id": "c1"})
        stream = client.post("/api/chat/stream", json={"message": "stream", "conversation_id": "c2"})
        assert sync.status_code == stream.status_code == 200
        for response, kind in [(sync, "chat_sync"), (stream, "chat_stream")]:
            run_id = response.headers["x-saber-run-id"]
            run = client.get(f"/api/agent-runs/{run_id}").json()
            assert run["status"] == "completed" and run["kind"] == kind
            assert run["result"]["response"]["answer"].startswith("echo:")
            events = client.get(f"/api/agent-runs/{run_id}/events").json()
            assert sum(e["type"] == "done" for e in events) == 1
            for invalid_cursor in ("-1", "abc"):
                assert (
                    client.get(
                        f"/api/agent-runs/{run_id}/stream", headers={"Last-Event-ID": invalid_cursor}
                    ).status_code
                    == 422
                )
        background = client.post("/api/agent-runs", json={"message": "background", "conversation_id": "c3"}).json()
        for _ in range(100):
            row = client.get(f"/api/agent-runs/{background['run_id']}").json()
            if row["status"] == "completed":
                break
            time.sleep(0.01)
        assert row["status"] == "completed"
        assert {row["kind"] for row in client.get("/api/agent-runs").json()} == {"chat_sync", "chat_stream", "chat"}


def test_foreground_conflicts_and_remote_cancel_share_the_conversation_lock(tmp_path):
    path = tmp_path / "runs.db"
    first = NativeRunService(path, heartbeat_interval=0.05)
    second = NativeRunService(path, heartbeat_interval=0.05)
    token = CancelToken()
    observation = first.begin_inline("alice", EchoAgent(), "foreground", conversation_id="c1", token=token)
    with pytest.raises(RunConflict):
        second.create("alice", EchoAgent(), "overlap", conversation_id="c1")
    second.cancel_conversation("alice", "c1")
    for _ in range(100):
        if token.is_cancelled():
            break
        time.sleep(0.01)
    assert token.is_cancelled()
    observation.finish({"interrupted": True})
    assert second.get("alice", observation.run_id)["status"] == "cancelled"
    first.close()
    second.close()


def test_terminal_event_failure_rolls_back_status(tmp_path):
    repo = SQLiteRunRepository(tmp_path / "runs.db")
    repo.register_worker("worker")
    row, _ = repo.reserve("alice", "worker", "hello", "c1", False, "", "chat", "")
    with sqlite3.connect(tmp_path / "runs.db") as db:
        db.execute("""CREATE TRIGGER fail_done BEFORE INSERT ON agent_run_events
            WHEN NEW.event_type='done' BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        repo.finish(row["run_id"], "alice", "completed", {"status": "completed"})
    assert repo.get("alice", row["run_id"])["status"] == "pending"
    assert [e["type"] for e in repo.events_since("alice", row["run_id"])] == ["queued"]


def test_concurrent_legacy_migration_is_serialized(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,message TEXT NOT NULL,use_rag INTEGER NOT NULL,status TEXT NOT NULL,
            result_json TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL)""")
    barrier = threading.Barrier(2)

    def migrate(_):
        barrier.wait()
        return SQLiteRunRepository(path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        repos = list(executor.map(migrate, range(2)))
    for repo in repos:
        assert repo.list("alice") == []


def test_expired_worker_is_fenced_and_cancels_local_token(tmp_path):
    path = tmp_path / "runs.db"
    service = NativeRunService(path, heartbeat_interval=0.05, stale_after=1)
    token = CancelToken()
    run = service.begin_inline("alice", EchoAgent(), "work", token=token)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE agent_run_workers SET heartbeat_at=0")
    for _ in range(100):
        if token.is_cancelled():
            break
        time.sleep(0.01)
    assert token.is_cancelled()
    assert service.get("alice", run.run_id)["status"] == "interrupted"
    with pytest.raises(RunConflict):
        service.create("alice", EchoAgent(), "late")
    service.close()


def test_failed_bootstrap_releases_infrastructure(monkeypatch):
    monkeypatch.setenv("AGI_AUTH_REQUIRED", "0")
    closed = []
    infrastructure = SimpleNamespace(close=lambda: closed.append("infrastructure"))

    def fail_agent(*_):
        raise RuntimeError("agent failed")

    with pytest.raises(RuntimeError, match="agent failed"):
        build_deps(APIConfig(), infrastructure_factory=lambda _: infrastructure, agent_factory=fail_agent)
    assert closed == ["infrastructure"]


def test_lifespan_runs_shutdown_even_when_application_raises():
    app = FastAPI()
    closed = []
    register_shutdown(app, lambda: closed.append("closed"))

    async def run():
        with pytest.raises(RuntimeError):
            async with app_lifespan(app):
                raise RuntimeError("application failed")

    asyncio.run(run())
    assert closed == ["closed"]
