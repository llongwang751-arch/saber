"""Production repository contracts against ONLY the isolated evaluation PostgreSQL.

Writes randomized test tenants, optionally restarts only agisaber-eval postgres.
Never reads configured production database credentials or modifies its data.
"""
import argparse
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(output, restart=False):
    from config.config import APIConfig
    from internal.platform.postgres import PostgresClient
    from internal.repo.ragchunk import Store
    from internal.repo.preference import PGRepo as Preferences
    from internal.repo.chathistory import PGRepo as History
    from internal.repo.longterm import PGRepo as Memories
    from internal.repo.snapshot import PGRepo as Snapshots
    from internal.memory.consistency import MemoryVersionConflict
    logging.disable(logging.CRITICAL)
    cfg = APIConfig()
    cfg.pg_host, cfg.pg_port = "127.0.0.1", 15432
    cfg.pg_user, cfg.pg_password, cfg.pg_database = "eval", "eval-local-only", "agisaber_eval"
    tenant = "eval-" + uuid.uuid4().hex
    other = tenant + "-other"
    checks = []
    client = None
    def record(name, action):
        started = time.perf_counter()
        try:
            detail = action()
            checks.append(dict(name=name, passed=True, evidence=detail))
        except Exception as exc:
            checks.append(dict(name=name, passed=False, error_type=type(exc).__name__))
        checks[-1]["latency_ms"] = round(1000*(time.perf_counter()-started), 2)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(dict(profile="isolated-real-persistence", tenant=tenant,
            checks=checks, passed=all(r["passed"] for r in checks)), ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps(checks[-1]), flush=True)
    def connect():
        nonlocal client
        client = PostgresClient(cfg)
        assert client.is_real()
        assert client.query_one("SELECT to_regclass('rag_chunks')")[0]
        return {"pooled_schema_bootstrap": True}
    record("connect_and_bootstrap", connect)
    if not checks[-1]["passed"]:
        return 1
    def contracts():
        repo = Store(client, None, None)
        first = repo.save_pg_with_parent(tenant, 0, "source", "parent", "[]", user_id=tenant,
                                        document_id="doc-a", version_id="v1", section="intro")
        second = repo.save_pg_with_parent(tenant, 0, "other source", "other parent", "[]", user_id=other)
        assert first > 0 and second > 0 and first != second
        rows = repo.load_by_ids_with_parent([first, second], tenant)
        assert len(rows) == 1 and rows[0]["document_id"] == "doc-a" and rows[0]["version_id"] == "v1"
        assert rows[0]["section"] == "intro"
        assert repo.save_pg_with_parent(tenant, 0, "updated", "parent", "[]", user_id=tenant) == first
        assert repo.load_by_ids_with_parent([first], tenant)[0]["document_id"] == "doc-a"
        Preferences(client).save(tenant, "language", "zh")
        assert Preferences(client).load(other) == {}
        history = History(client)
        history.save("user", "session one", tenant, "c1")
        history.save("user", "session two", tenant, "c2")
        assert [r.content for r in history.load(10, tenant, "c1")] == ["session one"]
        assert history.load(10, other, "c1") == []
        snapshots = Snapshots(client)
        snapshots.save(tenant, '{"stage":"paused"}', user_id=tenant)
        try:
            snapshots.save(tenant, '{"stage":"stolen"}', user_id=other)
        except RuntimeError:
            pass
        else:
            raise AssertionError("snapshot tenant takeover was accepted")
        assert snapshots.list(user_id=tenant)[0]["state"]["stage"] == "paused"
        assert snapshots.list(user_id=other) == []
        return {"rag_tenant_isolation": True, "source_metadata": True, "upsert_metadata_retained": True,
                "preferences_isolation": True, "conversation_isolation": True, "snapshot_takeover_blocked": True}
    record("repository_read_write_and_isolation", contracts)
    def memory():
        repo = Memories(client)
        first = repo.create_committed("职业=学生", .8, [], user_id=tenant)
        updated = repo.update_committed(first.memory_id, "职业=工程师", .8, [], expected_version=1, user_id=tenant)
        assert updated.version == 2
        try:
            repo.update_committed(first.memory_id, "过期写入", .8, [], expected_version=1, user_id=tenant)
        except MemoryVersionConflict:
            pass
        else:
            raise AssertionError("stale write committed")
        rows = repo.load_committed(tenant)
        assert len(rows) == 1 and rows[0].content == "职业=工程师" and rows[0].version == 2
        assert repo.load_committed(other) == []
        assert client.query_one("SELECT count(*) FROM memory_outbox WHERE user_id=%s", (tenant,))[0] == 6
        # Induce a real transaction failure in only this randomized tenant,
        # then verify the authoritative row AND its projection events roll back.
        marker = "rollback-" + uuid.uuid4().hex
        from unittest.mock import patch
        import internal.repo.longterm as module
        original = module._insert_projection_events
        def failing(cur, record):
            original(cur, record)
            cur.execute("SELECT 1 / 0")
        try:
            with patch.object(module, "_insert_projection_events", failing):
                repo.create_committed(marker, .5, [], user_id=tenant)
        except Exception:
            pass
        else:
            raise AssertionError("fault injection did not fail")
        assert client.query_one("SELECT count(*) FROM long_term_memory WHERE user_id=%s", (tenant,))[0] == 1
        assert client.query_one("SELECT count(*) FROM memory_outbox WHERE user_id=%s", (tenant,))[0] == 6
        return {"version_cas": True, "outbox_atomic_rollback": True,
                "failure_injection": "real SQL division by zero after outbox insertion", "events": 6}
    record("memory_correction_and_transaction_rollback", memory)
    def recover():
        nonlocal client
        client.close()
        subprocess.run(["docker", "compose", "-f", "docker-compose.eval.yml", "restart", "postgres"],
                       cwd=Path(__file__).resolve().parents[1], check=True, timeout=45, capture_output=True)
        import psycopg2
        for _ in range(15):
            try:
                with psycopg2.connect(host="127.0.0.1", port=15432, user="eval", password="eval-local-only",
                                      dbname="agisaber_eval", connect_timeout=2):
                    break
            except psycopg2.OperationalError:
                time.sleep(1)
        client = PostgresClient(cfg)
        assert Preferences(client).load(tenant)["language"] == "zh"
        assert History(client).load(10, tenant, "c1")[0].content == "session one"
        assert Memories(client).load_committed(tenant)[0].content == "职业=工程师"
        return {"preferences_history_memory_survived_database_restart": True}
    if restart:
        record("database_restart_recovery", recover)
    client.close()
    return 0 if all(c["passed"] for c in checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/live-persistence-eval.json"))
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args.output, args.restart))
