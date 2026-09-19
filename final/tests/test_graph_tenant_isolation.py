"""Neo4j 知识图谱与图记忆的确定性租户隔离合同。"""

from types import SimpleNamespace

import pytest

from internal.graph.kgstore import KGStore
from internal.graph.types import ChunkRef
from internal.memory.graph_memory import GraphMemory
from internal.platform.neo4j import Neo4jClient


class _Cfg:
    kg_max_hops = 2
    kg_weight = 0.3


class _RecordingNeo:
    """按 user_id 返回不同结果，并保留完整 Cypher/参数用于安全断言。"""

    def __init__(self):
        self.calls = []

    def is_real(self):
        return True

    def run_cypher(self, query, params=None):
        params = dict(params or {})
        self.calls.append((query, params))
        # 强制走不依赖 APOC 的直接降级路径。
        if "apoc.path.expandConfig" in query:
            raise RuntimeError("APOC unavailable")
        if "RETURN c.chunk_id AS cid" in query:
            rows = {
                "tenant-a": [{"cid": 1, "pgid": 101, "name": "腾讯"}],
                "tenant-b": [{"cid": 2, "pgid": 202, "name": "腾讯"}],
            }
            return rows.get(params.get("user_id"), [])
        if "RETURN DISTINCT n.mem_id AS id" in query:
            rows = {
                "tenant-a": [{"id": 11}],
                "tenant-b": [{"id": 22}],
            }
            return rows.get(params.get("user_id"), [])
        return []


def _extract(_system, _message):
    return (
        '{"entities":[{"name":"腾讯","type":"Organization"},'
        '{"name":"深圳","type":"Location"}],'
        '"relations":[{"from":"腾讯","to":"深圳","rel_type":"LOCATED_IN"}]}'
    )


def test_kg_same_entity_name_is_scoped_for_write_search_and_delete():
    neo = _RecordingNeo()
    kg_a = KGStore(_Cfg(), neo, llm_fn=_extract, user_id="tenant-a")
    kg_b = KGStore(_Cfg(), neo, llm_fn=_extract, user_id="tenant-b")

    ref = ChunkRef(id=1, pg_id=101, content="腾讯位于深圳")
    kg_a.index_document("same-doc", [ref])
    kg_b.index_document(
        "same-doc", [ChunkRef(id=2, pg_id=202, content="腾讯位于深圳")]
    )

    entity_writes = [(q, p) for q, p in neo.calls if "MERGE (e:Entity" in q]
    assert {p["user_id"] for _, p in entity_writes} == {"tenant-a", "tenant-b"}
    assert all("{user_id: $user_id, name: $name}" in q for q, _ in entity_writes)

    relation_writes = [(q, p) for q, p in neo.calls if "[r:LOCATED_IN" in q]
    assert {p["user_id"] for _, p in relation_writes} == {"tenant-a", "tenant-b"}
    assert all("{user_id: $user_id, name: $from}" in q for q, _ in relation_writes)
    assert all("{user_id: $user_id, doc_hash: $doc_hash}" in q for q, _ in relation_writes)

    # 同名实体的直接降级检索只得到当前租户绑定的 PG chunk。
    assert [hit.pg_id for hit in kg_a.search("腾讯", 5)] == [101]
    assert [hit.pg_id for hit in kg_b.search("腾讯", 5)] == [202]
    direct_calls = [(q, p) for q, p in neo.calls if "RETURN c.chunk_id AS cid" in q]
    assert [p["user_id"] for _, p in direct_calls] == ["tenant-a", "tenant-b"]
    assert all("Entity {user_id: $user_id}" in q for q, _ in direct_calls)

    before = len(neo.calls)
    kg_a.delete_document("same-doc")
    delete_calls = neo.calls[before:]
    assert len(delete_calls) == 3
    assert all(p == {"user_id": "tenant-a", "doc_hash": "same-doc"} for _, p in delete_calls)
    assert "(a:Entity {user_id: $user_id})" in delete_calls[0][0]
    assert "(b:Entity {user_id: $user_id})" in delete_calls[0][0]
    assert "(c:RAGChunk {user_id: $user_id, doc_hash: $doc_hash})" in delete_calls[1][0]
    assert "NOT (e)--()" in delete_calls[2][0]


def test_kg_multihop_path_requires_tenant_nodes_and_relationships():
    neo = _RecordingNeo()
    kg = KGStore(_Cfg(), neo, llm_fn=_extract, user_id="tenant-a")
    kg.search("腾讯", 5)

    query, params = next((q, p) for q, p in neo.calls if "apoc.path.expandConfig" in q)
    assert params["user_id"] == "tenant-a"
    assert "neighbor.user_id = $user_id" in query
    assert "node.user_id = $user_id" in query
    assert "rel.user_id = $user_id" in query


def test_graph_memory_same_mem_id_cannot_cross_expand_or_delete():
    neo = _RecordingNeo()
    gm_a = GraphMemory(_Cfg(), neo, user_id="tenant-a")
    gm_b = GraphMemory(_Cfg(), neo, user_id="tenant-b")

    gm_a._upsert_memory_node(7, "A 的偏好", 0.8)
    gm_b._upsert_memory_node(7, "B 的偏好", 0.9)
    gm_a._add_memory_edge(7, 11, "SIMILAR_TO", 0.8)
    gm_b._add_memory_edge(7, 22, "SIMILAR_TO", 0.9)

    assert gm_a.find_related(7) == [11]
    assert gm_b.find_related(7) == [22]
    expand_calls = [(q, p) for q, p in neo.calls if "RETURN DISTINCT n.mem_id" in q]
    assert [p["user_id"] for _, p in expand_calls] == ["tenant-a", "tenant-b"]
    assert all("node.user_id = $user_id" in q for q, _ in expand_calls)
    assert all("rel.user_id = $user_id" in q for q, _ in expand_calls)

    before = len(neo.calls)
    gm_a.delete_from_graph(7)
    delete_calls = neo.calls[before:]
    assert len(delete_calls) == 2
    assert all(
        params == {"user_id": "tenant-a", "id": 7}
        for _, params in delete_calls
    )
    assert "[r {user_id: $user_id}]" in delete_calls[0][0]
    assert "(n:Memory {user_id: $user_id})" in delete_calls[0][0]
    assert "WHERE NOT (m)--() DELETE m" in delete_calls[1][0]

    scoped_calls = [
        (q, p)
        for q, p in neo.calls
        if "Memory" in q and ("MERGE" in q or "MATCH" in q)
    ]
    assert all(p.get("user_id") in {"tenant-a", "tenant-b"} for _, p in scoped_calls)


def test_graph_stores_fail_closed_without_a_tenant():
    neo = _RecordingNeo()
    with pytest.raises(TypeError):
        KGStore(_Cfg(), neo)
    with pytest.raises(ValueError, match="non-empty user_id"):
        KGStore(_Cfg(), neo, user_id=" ")
    with pytest.raises(TypeError):
        GraphMemory(_Cfg(), neo)
    with pytest.raises(ValueError, match="non-empty user_id"):
        GraphMemory(_Cfg(), neo, user_id="")


class _SchemaResult:
    def consume(self):
        return None


class _SchemaSession:
    def __init__(self, calls, *, fail_first=False):
        self.calls = calls
        self.fail_first = fail_first

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, query):
        self.calls.append(query)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("compound constraints unsupported")
        return _SchemaResult()


class _SchemaDriver:
    def __init__(self, *, fail_first=False):
        self.calls = []
        self.fail_first = fail_first

    def session(self, **_kwargs):
        return _SchemaSession(self.calls, fail_first=self.fail_first)


def test_constraint_migration_creates_compound_key_before_dropping_global_name():
    client = object.__new__(Neo4jClient)
    client._driver = _SchemaDriver()
    client.cfg = SimpleNamespace()

    client.ensure_constraints()

    calls = client._driver.calls
    assert "REQUIRE (e.user_id, e.name) IS UNIQUE" in calls[0]
    assert calls[1] == "DROP CONSTRAINT entity_name IF EXISTS"
    assert any("REQUIRE (m.user_id, m.mem_id) IS UNIQUE" in q for q in calls)
    assert not any("REQUIRE e.name IS UNIQUE" in q for q in calls)


def test_constraint_migration_keeps_legacy_constraint_if_compound_create_fails():
    client = object.__new__(Neo4jClient)
    client._driver = _SchemaDriver(fail_first=True)
    client.cfg = SimpleNamespace()

    client.ensure_constraints()

    assert "DROP CONSTRAINT entity_name IF EXISTS" not in client._driver.calls
