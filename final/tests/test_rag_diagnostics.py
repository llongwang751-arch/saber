from types import SimpleNamespace

from internal.rag.evidence import render_claims
from internal.rag.hybrid import HybridResult, HybridStore
from internal.rag.rag import Engine


def test_abstention_is_distinct_from_invalid_json_and_invalid_quotes():
    evidence = [{"evidence_id": "E1", "content": "确切证据原文"}]
    for raw, expected in [('{"claims":[]}', 'model_abstained'),
                          ('[]', 'invalid_claims_schema'), ('not json', 'invalid_claims_json'),
                          ('{"claims":[{"text":"结论","citations":[{"evidence_id":"E1","quote":"并非证据原文"}]}]}',
                           'invalid_citation_source_or_quote')]:
        trace = {}
        assert render_claims(raw, evidence, trace) == (None, [])
        assert trace["reason"] == expected


def test_evidence_gate_records_rejection_without_calling_model():
    engine = Engine.__new__(Engine)
    engine.cfg = SimpleNamespace(rag_fallback_min_overlap=.9)
    engine.set_generate_fn(lambda *_: (_ for _ in ()).throw(AssertionError("must not generate")))
    trace = {}
    _, results = engine._compose_answer("完全无关", [{"pg_id": 7, "content": "预算二十万元"}], diagnostics=trace)
    assert not results
    assert trace["reason"] == "evidence_below_threshold"
    assert trace["evidence_gates"][0]["accepted"] is False
    assert "content" not in trace["evidence_gates"][0]


def test_parent_duplicates_do_not_exhaust_top_k_but_versions_remain_distinct():
    store = HybridStore.__new__(HybridStore)
    store._reranker = None
    hits = [HybridResult(pg_id=1, parent="同一个父段落", document_id="doc", version_id="v1"),
            HybridResult(pg_id=2, parent="同一个父段落", document_id="doc", version_id="v1"),
            HybridResult(pg_id=3, parent="另一条事实", document_id="doc", version_id="v1"),
            HybridResult(pg_id=4, parent="同一个父段落", document_id="doc", version_id="v2")]
    assert [h.pg_id for h in store._finalize("问题", hits, 3)] == [1, 3, 4]
