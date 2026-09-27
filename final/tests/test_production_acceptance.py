import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from config.config import APIConfig
from internal.application.readiness import ReadinessProbe
from internal.application.local_repos import LocalRagChunkRepo
from internal.application.store import ApplicationStore
from internal.handler.handler import setup_routes
from internal.rag.rag import Engine
from scripts.backup_server import backup, verify_restore


def test_populated_legacy_migration_preserves_cascade_children(tmp_path):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "legacy.db"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.attributes["database_url_override"] = "sqlite:///" + database.as_posix()
    command.upgrade(config, "0010_rag_projection_outbox")
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            PRAGMA foreign_keys=ON;
            INSERT INTO users(id,username,password_hash,created_at)
                VALUES('legacy','legacy','test-only','2026-01-01');
            INSERT INTO agent_preferences VALUES('legacy','locale','zh','2026-01-01');
            INSERT INTO agent_chat_history(user_id,role,content,created_at)
                VALUES('legacy','user','preserve this history','2026-01-01');
            INSERT INTO agent_documents VALUES
                ('doc-old','legacy','Original','note','user','active','legacy','2026-01-01','2026-01-01');
            INSERT INTO agent_document_versions VALUES
                ('version-old','doc-old',1,'Original content','summary','{}','2026-01-01');
            INSERT INTO agent_rag_chunks(user_id,doc_hash,chunk_idx,content,parent_content,
                embedding,document_id,version_id,section,created_at)
                VALUES('legacy','hash',0,'Original content','parent','[]','doc-old','version-old','note','2026-01-01');
        """)
        preserved = {
            table: connection.execute('SELECT * FROM ' + table).fetchall()
            for table in ('agent_preferences', 'agent_documents', 'agent_document_versions', 'agent_rag_chunks')
        }
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        for table, rows in preserved.items():
            assert connection.execute('SELECT * FROM ' + table).fetchall() == rows
        assert connection.execute('SELECT content,conversation_id FROM agent_chat_history').fetchone() == (
            'preserve this history', ''
        )
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    store = ApplicationStore(database_url="sqlite:///" + database.as_posix())
    try:
        with store.engine.connect() as connection:
            assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
    finally:
        store.close()


def test_research_organization_is_not_a_report_request():
    from internal.agent.planner import needs_subagent_plan

    assert not needs_subagent_plan("白鹭研究院的星桥项目负责人是谁？")
    assert not needs_subagent_plan("研究所在哪里？")
    assert needs_subagent_plan("请研究白鹭研究院的知识库并生成报告")


def test_datastore_environment_overrides(monkeypatch):
    from config.config import _apply_environment_overrides

    monkeypatch.setenv("AGI_ES_ADDRESSES", "http://127.0.0.1:19200, http://127.0.0.1:19201")
    monkeypatch.setenv("AGI_ES_USERNAME", "service")
    monkeypatch.setenv("AGI_ES_PASSWORD", "private-es")
    monkeypatch.setenv("AGI_NEO4J_URI", "bolt://127.0.0.1:17687")
    monkeypatch.setenv("AGI_NEO4J_USER", "neo4j")
    monkeypatch.setenv("AGI_NEO4J_PASSWORD", "private-graph")
    monkeypatch.setenv("AGI_KG_ENABLED", "true")
    cfg = APIConfig()
    _apply_environment_overrides(cfg)
    assert cfg.es_addresses == ["http://127.0.0.1:19200", "http://127.0.0.1:19201"]
    assert (cfg.es_username, cfg.es_password) == ("service", "private-es")
    assert cfg.kg_enabled and cfg.neo4j_uri == "bolt://127.0.0.1:17687"
    assert cfg.neo4j_password == "private-graph"


def test_milvus_existing_collection_is_loaded_and_load_errors_propagate():
    from internal.infra.infra import Infrastructure

    calls = []
    client = SimpleNamespace(
        has_collection=lambda name: True,
        load_collection=lambda **kwargs: calls.append(kwargs),
    )
    inf = Infrastructure.__new__(Infrastructure)
    inf.cfg, inf._milvus = APIConfig(), client
    inf._verify_milvus_rag_schema = lambda *args: None
    inf._init_milvus_collections()
    assert calls == [{"collection_name": "rag_chunks"}]

    def fail(**kwargs):
        raise RuntimeError("load failed")

    client.load_collection = fail
    with pytest.raises(RuntimeError, match="load failed"):
        inf._init_milvus_collections()


def test_release_uses_explicit_frontend_without_packaging_secrets(tmp_path):
    import tarfile
    from scripts.package_release import package

    root, web = tmp_path / "source", tmp_path / "web-build"
    for folder in (root / "scripts", root / "config", web):
        folder.mkdir(parents=True)
    for filename in ("main.py", "alembic.ini", "scripts/backup_server.py"):
        (root / filename).write_text("# public source")
    (root / "config/config.yaml").write_text("password: private")
    (web / "index.html").write_text('<script src="/api/office/assets/app.js"></script>')
    output = tmp_path / "release.tar.gz"
    package(root, output, web)
    with tarfile.open(output) as archive:
        assert "config/config.yaml" not in archive.getnames()
        assert b"/api/office/" in archive.extractfile("web/dist/index.html").read()


def test_readiness_fails_recovers_and_never_exposes_exception_text():
    state = {"ok": False}

    def check():
        if not state["ok"]:
            raise RuntimeError("password=private")
        return True

    probe = ReadinessProbe({"dependency": check}, cache_seconds=0)
    assert probe.snapshot()["ready"] is False
    assert "private" not in json.dumps(probe.snapshot())
    state["ok"] = True
    assert probe.snapshot()["ready"] is True
    probe.close()
    assert probe.snapshot()["ready"] is False


def test_slow_readiness_does_not_spawn_repeated_workers():
    unblock, calls = threading.Event(), []

    def slow():
        calls.append(1)
        unblock.wait(3)
        return True

    probe = ReadinessProbe({"slow": slow}, timeout=0.01, cache_seconds=0)
    try:
        for _ in range(5):
            assert probe.snapshot()["checks"]["slow"]["reason"] == "probe_timeout"
        assert len(calls) == 1
    finally:
        unblock.set()
        probe.close()


def test_strict_readyz_uses_live_dependencies_not_startup_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("AGI_EVAL_DATABASE_URL", "sqlite:///" + (tmp_path / "app.db").as_posix())
    monkeypatch.setenv("AGI_READINESS_REQUIRED", "application,run_store,milvus")
    inf = SimpleNamespace(ready=SimpleNamespace(milvus="connected"), _milvus=None)
    app = setup_routes(SimpleNamespace(user_id="a"), inf, APIConfig())
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        report = client.get("/api/ops/readiness").json()
        assert report["checks"]["application"]["ready"]
        assert report["checks"]["run_store"]["ready"]
        assert not report["checks"]["milvus"]["ready"]


def test_batch_ingest_bounds_embeddings_and_preserves_offsets(monkeypatch):
    monkeypatch.setenv("AGI_EMBEDDING_BATCH_SIZE", "3")
    engine = object.__new__(Engine)
    engine.parent_splitter = SimpleNamespace(split=lambda _: [SimpleNamespace(content="parent")])
    engine.child_splitter = SimpleNamespace(split=lambda _: [SimpleNamespace(content=str(i), id=i) for i in range(8)])
    sizes, offsets = [], []

    def embed(texts):
        sizes.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    def index(_, contents, parents, vectors, **kwargs):
        offsets.append(kwargs["chunk_start"])
        assert len(contents) == len(parents) == len(vectors) <= 3

    engine._llm = SimpleNamespace(embed_batch=embed)
    engine._hybrid = SimpleNamespace(index_with_parents=index)
    engine.inf = SimpleNamespace(repo=SimpleNamespace(events=SimpleNamespace(publish=lambda *_: None)))
    assert engine.ingest("text") == 8
    assert sizes == [3, 3, 2] and offsets == [0, 3, 6]


def test_batch_repo_keeps_fts_metadata_and_tenant_isolation(tmp_path):
    store = ApplicationStore("sqlite:///" + (tmp_path / "application.db").as_posix())
    alice = store.create_user("batch-alice", "unused-hash")["id"]
    repo = LocalRagChunkRepo(store)
    try:
        ids = repo.save_many_with_parents(
            "doc",
            ["alpha", "beta"],
            ["parent", "parent"],
            ["[]", "[]"],
            user_id=alice,
            start_index=5,
            document_id="d1",
            version_id="v1",
        )
        assert len(ids) == 2
        assert repo.count(alice) == 2 and repo.count("bob") == 0
        with store.engine.connect() as c:
            assert c.exec_driver_sql("SELECT count(*) FROM agent_rag_chunks_fts").scalar() == 2
            assert [r[0] for r in c.exec_driver_sql("SELECT chunk_idx FROM agent_rag_chunks ORDER BY chunk_idx")] == [
                5,
                6,
            ]
        assert (
            repo.save_many_with_parents("doc", ["changed"], ["parent"], ["[]"], user_id=alice, start_index=5)
            == ids[:1]
        )
    finally:
        store.close()


def test_backup_includes_run_ledger_and_requires_missing_ledger(tmp_path):
    root = tmp_path / "server"
    (root / "runtime").mkdir(parents=True)
    with sqlite3.connect(root / "runtime/application.db") as db:
        for table in ("users", "agent_documents", "agent_rag_chunks", "agent_long_term_memory", "agent_chat_history"):
            db.execute(f"CREATE TABLE {table} (id INTEGER)")
    with pytest.raises(FileNotFoundError):
        backup(root, tmp_path / "backups", require_runs=True)
    with sqlite3.connect(root / "runtime/application.agent-runs.sqlite3") as db:
        db.execute("CREATE TABLE agent_runs (run_id TEXT)")
        db.execute("INSERT INTO agent_runs VALUES ('kept')")
    result = backup(root, tmp_path / "backups", require_runs=True)
    restored = verify_restore(result["archive"], tmp_path / "restore")
    assert restored["run_ledger_restored"] is True
    with sqlite3.connect(tmp_path / "restore/agent_runs.sqlite3") as db:
        assert db.execute("SELECT run_id FROM agent_runs").fetchone()[0] == "kept"
