from types import SimpleNamespace

from internal.rag.rag import Engine


def test_parent_near_duplicates_are_removed_but_distinct_evidence_remains():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(rag_parent_dedup_threshold=0.85)
    results = [
        {"pg_id": 1, "content": "第二年度项目预算为八百万元，资金主要用于设备采购和软件开发。"},
        {"pg_id": 2, "content": "第二年度项目预算为八百万元，资金主要用于设备采购和软件研发。"},
        {"pg_id": 3, "content": "项目负责人是张三，交付时间为十二月。"},
    ]

    deduped = engine._dedupe_results_by_content(results)

    assert [item["pg_id"] for item in deduped] == [1, 3]


def test_parent_dedup_threshold_can_preserve_similar_but_nonidentical_evidence():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(rag_parent_dedup_threshold=0.99)
    results = [
        {"pg_id": 1, "content": "项目预算为八百万元，主要用于设备采购。"},
        {"pg_id": 2, "content": "项目预算为八百万元，主要用于设备研发。"},
    ]

    assert len(engine._dedupe_results_by_content(results)) == 2
