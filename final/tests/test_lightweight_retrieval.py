from types import SimpleNamespace

import pytest
from sqlalchemy import text

# chromadb 是可选的轻量检索加速组件（见 requirements-lightweight.txt），
# 未安装时整组测试跳过，保持“离线可跑”的默认体验。
pytest.importorskip("chromadb")

from internal.application.store import ApplicationStore
from internal.application.local_repos import LocalRagChunkRepo
from internal.graph.types import Entity, Relation, ExtractResult
from internal.rag.lightweight import LightweightIndex


def row(pid, content):
    return dict(id=pid, content=content, parent_content='', document_id='doc-' + str(pid), version_id='v1', section='')


def test_real_chroma_persistence_isolation_revision_and_dimension(tmp_path):
    rows = [row(1, 'Alpha'), row(2, 'Beta')]
    index = LightweightIndex(tmp_path, ['provider', 'model'], 'alice')
    index.index(rows, lambda s: [1., 0.] if s == 'Alpha' else [0., 1.])
    restarted = LightweightIndex(tmp_path, ['provider', 'model'], 'alice')
    current = restarted.reconcile(rows)
    assert restarted.semantic([1., 0.], 1, current) == [1]
    assert LightweightIndex(tmp_path, ['provider', 'model'], 'bob').collection.count() == 0
    assert LightweightIndex(tmp_path, ['provider', 'other-model'], 'alice').collection.count() == 0
    with pytest.raises(Exception):
        restarted.semantic([1., 0., 0.], 2, current)
    rows[0]['content'] = 'Changed'
    current = restarted.reconcile(rows[:1])
    assert restarted.collection.count() == 0  # changed + deleted projections removed
    assert restarted.semantic([1., 0.], 2, current) == []


def test_graph_multihop_sources_delete_and_retry(tmp_path):
    rows = [row(1, 'Alpha works with Beta'), row(2, 'Beta works with Gamma')]
    def extract(source):
        a, b = ('Alpha', 'Beta') if 'Alpha' in source else ('Beta', 'Gamma')
        return ExtractResult([Entity(a), Entity(b), Entity('Hallucinated')],
                             [Relation(a, b, 'RELATES_TO'), Relation(a, 'Hallucinated', 'RELATES_TO')])
    index = LightweightIndex(tmp_path, 'm', 'alice')
    report = index.index(rows, lambda _: [1., 0.], SimpleNamespace(extract=extract))
    assert report['complete'] and report['relations'] == 2
    current = index.reconcile(rows)
    assert set(index.graph('Alpha', 10, current, max_hops=1)) == {1, 2}
    assert index.graph('Hallucinated', 10, current) == []
    assert index.graph('Alphabet', 10, current) == []
    assert LightweightIndex(tmp_path, 'm', 'bob').graph('Alpha', 10, current) == []
    index.reconcile(rows[1:])
    assert index.graph('Alpha', 10, index.reconcile(rows[1:])) == []
    assert index.status(rows[1:])['relations'] == 1
    def fail(_):
        raise RuntimeError('model unavailable')
    rows.append(row(3, 'Delta'))
    with pytest.raises(RuntimeError):
        index.index(rows[1:], lambda _: [1., 0.], SimpleNamespace(extract=fail))
    assert not index.status(rows[1:])['complete']
    assert index.index(rows[1:], lambda _: [1., 0.], SimpleNamespace(extract=lambda _: ExtractResult()))['complete']


def test_fts_legacy_repair_phrase_precision_and_score_order(tmp_path):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    a = store.create_user('alice', 'hash')['id']
    b = store.create_user('bob', 'hash')['id']
    repo = LocalRagChunkRepo(store)
    first = repo.save_pg('one', 0, '知识库 知识库 知识库', [], user_id=a)
    repo.save_pg('two', 0, '知晓事实 学识丰富 仓库管理', [], user_id=a)
    repo.save_pg('three', 0, '知识库 以及很多其他文字用于解释系统功能', [], user_id=a)
    repo.save_pg('secret', 0, '知识库', [], user_id=b)
    with store.transaction() as s:
        s.execute(text('DELETE FROM agent_rag_chunks_fts'))
    hits = repo.search_fts5('知识库', 10, user_id=a)
    assert len(hits) == 2
    assert hits[0]['pg_id'] == first
    assert hits[0]['score'] >= hits[1]['score'] > 0
    assert repo.repair_fts5(user_id=a) == 0
    assert len(repo.search_fts5('知识库', 10, user_id=b)) == 1
    store.close()


def test_batch_failure_preserves_completed_vectors_for_retry(tmp_path):
    rows = [row(1, 'Alpha'), row(2, 'Beta')]
    index = LightweightIndex(tmp_path, 'batch', 'alice')
    with pytest.raises(ValueError):
        index.index(rows, None, embed_batch=lambda _: [[1., 0.], []])
    assert index.collection.count() == 1
    requested = []
    def batch(texts):
        requested.extend(texts)
        return [[0., 1.] for _ in texts]
    index.index(rows, None, embed_batch=batch)
    assert requested == ['Beta']
    assert index.collection.count() == 2


def test_hybrid_uses_real_three_paths_and_filters_deleted_sources(tmp_path):
    from config.config import APIConfig
    from internal.rag.hybrid import HybridStore
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    a = store.create_user('alice', 'hash')['id']
    repo = LocalRagChunkRepo(store)
    cfg = APIConfig()
    cfg.rag_lightweight_enabled = True
    cfg.rag_lightweight_path = str(tmp_path / 'indexes')
    repo.save_pg('one', 0, 'Alpha works with Beta', [], user_id=a)
    repo.save_pg('two', 0, 'Beta works with Gamma', [], user_id=a)
    hybrid = HybridStore(cfg, SimpleNamespace(repo=SimpleNamespace(ragchunk=repo)), lambda _: [1., 0.], user_id=a)
    hybrid._lightweight_extractor = SimpleNamespace(extract=lambda _: ExtractResult([Entity('Beta')]))
    assert hybrid.rebuild_lightweight()['complete']
    from internal.rag.rag import Engine
    engine = Engine.__new__(Engine)
    engine._hybrid = hybrid
    assert engine.mode() == 'lightweight'
    assert engine.rebuild_indexes()['vectors_written'] == 0
    trace = {}
    assert hybrid.search('Beta', 5, trace)
    assert all(trace['lightweight_paths'][p]['hits'] == 2 for p in ('bm25', 'semantic', 'graph'))
    repo.delete('one', user_id=a)
    assert len(hybrid.search('Beta', 5, {})) == 1
    store.close()
