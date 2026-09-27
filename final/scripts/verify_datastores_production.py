"""Live RAG acceptance through the running app, with direct ES/Neo4j corroboration.

Uses a dedicated existing acceptance account. Never opens the live Milvus Lite
file from a second process; semantic retrieval is exercised through the API.
The fixture is explicitly marked and its IDs are persisted for scoped cleanup.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import requests
from neo4j import GraphDatabase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["create", "check", "delete"], required=True)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    fixture_path = args.directory / "rag-fixture.json"
    env = dict(line.strip().split("=", 1) for line in Path("/etc/agi-office/datastores.env").read_text().splitlines() if "=" in line)
    session = requests.Session()
    session.trust_env = False
    base = "http://172.17.0.1:18090"
    account = json.loads(Path("/etc/agi-office/acceptance-account.json").read_text())
    response = session.post(base + "/api/auth/login", json=account, timeout=15)
    response.raise_for_status()
    session.headers["Authorization"] = "Bearer " + response.json()["token"]
    user = session.get(base + "/api/auth/me", timeout=15).json()
    user_id = user["id"]
    es = requests.Session()
    es.auth = (env["AGI_ES_USERNAME"], env["AGI_ES_PASSWORD"])
    graph = GraphDatabase.driver(env["AGI_NEO4J_URI"], auth=(env["AGI_NEO4J_USER"], env["AGI_NEO4J_PASSWORD"]), connection_timeout=5)
    if args.phase == "create":
        assert not fixture_path.exists(), "Existing fixture: check or delete it first"
        marker = "SABERACCEPT" + uuid4().hex[:10]
        content = ("白鹭研究院建立了星桥验收项目，项目负责人是林晓。林晓与白鹭研究院合作建设知识库。"
                   "星桥验收项目使用 Milvus 保存语义向量，Elasticsearch 执行关键词检索，Neo4j 保存实体关系。"
                   "星桥验收项目的验收标识为 " + marker + "。")
        response = session.post(base + "/api/documents", json={
            "title": "生产验收临时文档 " + marker, "doc_type": "acceptance",
            "source": "production-acceptance", "content_md": content, "ingest_to_rag": True,
        }, timeout=120)
        response.raise_for_status()
        body = response.json()
        fixture = {"document_id": body["document"]["id"], "user_id": user_id,
                   "doc_hash": "", "marker": marker}
        fixture_path.write_text(json.dumps(fixture))
    else:
        fixture = json.loads(fixture_path.read_text())
        assert fixture["user_id"] == user_id
    if args.phase == "delete":
        response = session.delete(base + "/api/documents/" + fixture["document_id"], timeout=60)
        response.raise_for_status()
        print(json.dumps({"fixture_deleted": True, "document_id": fixture["document_id"]}))
        return
    with sqlite3.connect("file:/var/lib/agi-office/application.db?mode=ro", uri=True) as database:
        rows = database.execute(
            "SELECT id, doc_hash FROM agent_rag_chunks WHERE user_id=? AND document_id=?",
            (user_id, fixture["document_id"])).fetchall()
        ids = [row[0] for row in rows]
    assert ids, "No durable chunks"
    fixture["doc_hash"] = rows[0][1]
    fixture_path.write_text(json.dumps(fixture))
    es.post(env["AGI_ES_ADDRESSES"] + "/rag_chunks/_refresh", timeout=10).raise_for_status()
    result = es.post(env["AGI_ES_ADDRESSES"] + "/rag_chunks/_search", json={
        "query": {"bool": {"must": [{"match": {"content": fixture["marker"]}}],
                          "filter": [{"term": {"user_id": user_id}}]}}}, timeout=10)
    result.raise_for_status()
    es_hits = result.json()["hits"]["total"]["value"]
    assert es_hits > 0
    deadline, graph_chunks, graph_edges = time.monotonic() + 90, 0, 0
    while time.monotonic() < deadline:
        with graph.session() as db:
            graph_chunks = db.run(
                "MATCH (c:RAGChunk {user_id:$user_id, doc_hash:$doc_hash}) RETURN count(c) AS n",
                user_id=user_id, doc_hash=fixture["doc_hash"]).single()["n"]
            graph_edges = db.run(
                "MATCH (:Entity {user_id:$user_id})-[r {user_id:$user_id,doc_hash:$doc_hash}]->(:Entity) RETURN count(r) AS n",
                user_id=user_id, doc_hash=fixture["doc_hash"]).single()["n"]
        if graph_chunks and graph_edges:
            break
        time.sleep(2)
    assert graph_chunks and graph_edges, "Graph extraction did not produce persisted nodes and relations"
    response = session.post(base + "/api/chat", json={
        "message": "根据知识库，白鹭研究院的星桥验收项目由谁负责，采用哪些数据库？",
        "use_rag": True, "conversation_id": uuid4().hex,
    }, timeout=120)
    response.raise_for_status()
    chat = response.json()
    assert chat["success"] and chat["search_results"]
    paths = [entry.get("retrieval_paths", {}) for entry in chat["rag_trace"]["retrieval"]["query_paths"]]
    checks = {key: any(p.get(key, {}).get("ok") and p.get(key, {}).get("hits") for p in paths)
              for key in ("semantic", "keyword", "knowledge_graph")}
    assert all(checks.values()), "At least one retrieval path had no hits: " + str(checks)
    evidence = {"phase": args.phase, "document_id": fixture["document_id"], "durable_chunks": len(ids),
                "es_hits": es_hits, "neo4j_chunks": graph_chunks, "neo4j_relation_edges": graph_edges,
                "retrieval_paths_with_hits": {key: bool(value) for key, value in checks.items()},
                "response_success": chat["success"]}
    (args.directory / ("rag-" + args.phase + ".json")).write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence), flush=True)
    graph.close()
    session.close()
    es.close()


if __name__ == "__main__":
    main()
