"""Structural provenance/isolation checks on ONLY isolated Neo4j port 17687."""
import json
import logging
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run():
    from config.config import APIConfig
    from internal.platform.neo4j import Neo4jClient
    from internal.graph.kgstore import KGStore
    from internal.graph.types import Entity
    logging.disable(logging.CRITICAL)
    cfg = APIConfig()
    cfg.kg_enabled = True
    cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password = "bolt://127.0.0.1:17687", "neo4j", "eval-local-only"
    tenant = "eval-" + uuid.uuid4().hex
    result = dict(profile="isolated-real-graph-structure", tenant=tenant, passed=False,
                  limitations=["known entity fixtures; not model extraction or graph answer quality"])
    client = None
    try:
        from neo4j import GraphDatabase
        for attempt in range(20):
            try:
                with GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password),
                        connection_timeout=2, connection_acquisition_timeout=2) as probe:
                    probe.verify_connectivity()
                break
            except Exception:
                if attempt == 19:
                    raise
                time.sleep(1)
        client = Neo4jClient(cfg)
        assert client.available()
        graph = KGStore(cfg, client, user_id=tenant)
        for pid, doc in [(1001, "doc-a"), (1002, "doc-b")]:
            graph._upsert_entity(Entity(name="共享实体", type="Concept", doc_hash=doc, chunk_id=0, pg_id=pid))
        other = KGStore(cfg, client, user_id=tenant+"-other")
        other._upsert_entity(Entity(name="共享实体", type="Concept", doc_hash="private", chunk_id=0, pg_id=9999))
        assert {r.pg_id for r in graph._search_direct(["共享实体"], 10)} == {1001, 1002}
        graph.delete_document("doc-a")
        assert {r.pg_id for r in graph._search_direct(["共享实体"], 10)} == {1002}
        assert {r.pg_id for r in other._search_direct(["共享实体"], 10)} == {9999}
        client.run_cypher("CREATE (:Entity {user_id:$user, name:'旧实体', pg_id:1003, doc_hash:'legacy', chunk_id:0})", {"user":tenant})
        graph._backfill_legacy_provenance()
        assert {r.pg_id for r in graph._search_direct(["旧实体"], 10)} == {1003}
        result.update(passed=True, shared_entity_multiple_sources=True, delete_one_preserves_other=True,
                      tenant_isolation=True, legacy_latest_source_backfill=True)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        if client is not None:
            client.close()
    Path("docs/live-graph-eval.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
