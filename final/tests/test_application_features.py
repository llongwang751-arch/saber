from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from internal.application.auth import AuthService, AuthenticationError
from internal.application.skills import SkillService
from internal.application.store import ApplicationStore, NotFoundError
from internal.application.local_repos import (
    LocalChatHistoryRepo,
    LocalDocumentRepo,
    LocalLongTermRepo,
    LocalPreferenceRepo,
    LocalRagChunkRepo,
    LocalSnapshotRepo,
    MemoryOutboxWorker,
)
from config.config import APIConfig
from internal.handler.handler import setup_routes
from internal.document.library import WriteRequest
from internal.evaluation.service import EvaluationServiceRegistry
from internal.rag.rag import Engine as RAGEngine
from types import SimpleNamespace


def database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


@pytest.fixture()
def store(tmp_path: Path):
    value = ApplicationStore(database_url(tmp_path / "application.db"))
    try:
        yield value
    finally:
        value.close()


def create_user(store: ApplicationStore, name: str) -> tuple[AuthService, dict]:
    auth = AuthService(store, secret="a-secure-test-secret-that-is-at-least-32-bytes")
    return auth, auth.register(name, "correct-horse-battery")


def test_auth_register_login_verify_and_failure_shape(store: ApplicationStore):
    auth, registered = create_user(store, "alice")
    logged_in = auth.login("alice", "correct-horse-battery")

    assert logged_in["user_id"] == registered["user_id"]
    assert auth.verify(logged_in["token"])["username"] == "alice"
    with pytest.raises(AuthenticationError, match="用户名或密码错误"):
        auth.login("alice", "wrong-password")
    with pytest.raises(AuthenticationError, match="用户名或密码错误"):
        auth.login("nobody", "wrong-password")
    assert AuthService(store, secret="replace-with-at-least-32-random-characters").using_development_secret is True


def test_skill_install_toggle_and_tenant_isolation(store: ApplicationStore, monkeypatch):
    _, alice = create_user(store, "alice")
    _, bob = create_user(store, "bobby")
    skills = SkillService(store)
    github_calls = []
    monkeypatch.setattr(skills, "_github_skills", lambda: github_calls.append(True) or [])

    installed = skills.install(alice["user_id"], "builtin:weekly_report")
    assert installed["enabled"] is True
    assert github_calls == []
    assert skills.installed(bob["user_id"]) == []

    disabled = skills.toggle(alice["user_id"], "builtin:weekly_report", False)
    assert disabled["enabled"] is False
    skills.uninstall(alice["user_id"], "builtin:weekly_report")
    assert skills.installed(alice["user_id"]) == []
    with pytest.raises(NotFoundError):
        skills.uninstall(alice["user_id"], "builtin:weekly_report")


def test_fastapi_strict_auth_contract(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGI_EVAL_DATABASE_URL", database_url(tmp_path / "api.db"))

    class Agent:
        pass

    class Infra:
        pass

    with TestClient(setup_routes(Agent(), Infra(), APIConfig(), auth_required=True)) as client:
        assert client.get("/api/status").status_code == 401
        created = client.post(
            "/api/auth/register",
            json={"username": "interviewer", "password": "strong-password"},
        )
        assert created.status_code == 200
        token = created.json()["token"]
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["username"] == "interviewer"


def test_local_agent_state_is_durable_and_tenant_scoped(store: ApplicationStore):
    _, alice = create_user(store, "alice")
    _, bob = create_user(store, "bobby")
    alice_id = alice["user_id"]
    bob_id = bob["user_id"]

    preferences = LocalPreferenceRepo(store)
    chats = LocalChatHistoryRepo(store)
    snapshots = LocalSnapshotRepo(store)
    memory = LocalLongTermRepo(store)
    documents = LocalDocumentRepo(store)

    preferences.save(alice_id, "姓名", "Alice")
    chats.save("user", "hello", user_id=alice_id)
    snapshots.save("task-1", b'{"step": 1}', user_id=alice_id)
    memory_id = memory.save("喜欢结构化输出", 0.8, "[0.1, 0.2]", user_id=alice_id)
    written = documents.write(
        WriteRequest(title="报告", content_md="# 正文", source="agent_generated"),
        user_id=alice_id,
    )

    assert preferences.load(alice_id) == {"姓名": "Alice"}
    assert preferences.load(bob_id) == {}
    assert [entry.content for entry in chats.load(10, user_id=alice_id)] == ["hello"]
    assert chats.load(10, user_id=bob_id) == []
    assert snapshots.list(user_id=alice_id)[0]["state"] == {"step": 1}
    assert memory.load(user_id=alice_id)[0].id == memory_id
    assert memory.load(user_id=bob_id) == []
    assert documents.list(user_id=bob_id) == []
    assert documents.get(written.document.id, user_id=alice_id)[1].content_md == "# 正文"
    with pytest.raises(LookupError):
        documents.get(written.document.id, user_id=bob_id)


def test_local_memory_write_and_outbox_are_atomic(store: ApplicationStore):
    _, alice = create_user(store, "alice")
    memory = LocalLongTermRepo(store)
    memory_id = memory.save("需要持久化", 0.9, "[]", user_id=alice["user_id"])
    worker = MemoryOutboxWorker(store)

    assert memory_id > 0
    assert worker.status() == {"pending": 3, "retrying": 0, "dead": 0}
    assert worker.process_once() == 3
    assert worker.status() == {"pending": 0, "retrying": 0, "dead": 0}


def test_evaluation_registry_uses_isolated_tenant_databases(tmp_path: Path):
    registry = EvaluationServiceRegistry(root=tmp_path / "eval-tenants")
    try:
        alice = registry.get("alice-user")
        bob = registry.get("bob-user")
        alice.create_dataset("alice-private-eval")

        assert [item["name"] for item in alice.store.list_datasets()] == ["alice-private-eval"]
        assert bob.store.list_datasets() == []
        assert len(list((tmp_path / "eval-tenants").glob("eval-*.db"))) == 2
    finally:
        registry.close()


def test_local_rag_ingest_query_restart_and_tenant_isolation(store: ApplicationStore):
    _, alice = create_user(store, "alice")
    _, bob = create_user(store, "bobby")
    rag_repo = LocalRagChunkRepo(store)

    class FakeLLM:
        @staticmethod
        def embed(text):
            return []

    class Events:
        @staticmethod
        def publish(*args):
            return None

    inf = SimpleNamespace(
        repo=SimpleNamespace(ragchunk=rag_repo, events=Events()),
        ready=SimpleNamespace(postgresql="disconnected", milvus="disconnected", elasticsearch="disconnected"),
    )
    cfg = APIConfig()
    engine = RAGEngine(cfg, inf, FakeLLM(), user_id=alice["user_id"])
    chunks = engine.ingest(
        "电梯求生小说中，林枫通过系统提示预测下一层信息，并据此选择楼层。",
        document_id="doc-local",
        version_id="ver-local",
        section="upload",
    )
    answer, hits = engine.query("预测楼层信息")

    assert chunks > 0
    assert hits and hits[0]["source"] == "local_keyword"
    assert "林枫" in answer
    assert RAGEngine(cfg, inf, FakeLLM(), user_id=alice["user_id"]).loaded is True
    assert RAGEngine(cfg, inf, FakeLLM(), user_id=bob["user_id"]).loaded is False

    engine.delete_document("doc-local")
    assert rag_repo.count(user_id=alice["user_id"]) == 0
