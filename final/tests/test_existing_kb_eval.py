import sqlite3

import pytest

from scripts.run_existing_kb_eval import GROUPS, cases_for, snapshot


def test_questions_require_gold_evidence_and_keep_paraphrases_grouped():
    rows = [dict(id=i, content=anchor, parent_content="")
            for i, (_, anchor, _) in enumerate(GROUPS, 1)]
    cases = cases_for(rows)
    assert len(cases) == 50
    assert sum(c["answerable"] for c in cases) == 36
    for case in cases:
        assert bool(case["source_ids"]) == case["answerable"]
    assert cases[0]["group"] == cases[1]["group"]
    with pytest.raises(ValueError, match="Source anchor absent"):
        cases_for([])


def test_snapshot_is_read_only_and_digest_detects_changed_source(tmp_path):
    path = tmp_path / "corpus.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_rag_chunks (id INTEGER, content TEXT)")
        conn.execute("INSERT INTO agent_rag_chunks VALUES (1, 'original')")
    before = path.read_bytes()
    rows, digest = snapshot(path)
    assert rows == [{"id": 1, "content": "original"}]
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE agent_rag_chunks SET content='corrected'")
    assert snapshot(path)[1] != digest


def test_snapshot_missing_source_does_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        snapshot(path)
    assert not path.exists()
