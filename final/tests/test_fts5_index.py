import sqlite3
import pytest
from internal.rag.fts5_index import FTS5IndexManager, fts_tokenize, build_fts_query


def test_fts_tokenize():
    text = "RAG知识库检索与Tencent WeKnora对比"
    tokenized = fts_tokenize(text)
    # 中文字符被空格分开，英文字词保持完整
    assert "rag" in tokenized
    assert "知 识 库" in tokenized
    assert "tencent" in tokenized
    assert "weknora" in tokenized


def test_fts5_crud_and_search():
    conn = sqlite3.connect(":memory:")
    assert FTS5IndexManager.ensure_table(conn) is True

    # 插入几条记录
    FTS5IndexManager.index_chunk(
        conn,
        pg_id=1,
        user_id="user_a",
        doc_hash="doc_1",
        content="企业级知识库与混合检索方案",
        parent_content="章节一：引言",
    )
    FTS5IndexManager.index_chunk(
        conn,
        pg_id=2,
        user_id="user_a",
        doc_hash="doc_1",
        content="大语言模型微调与量化技术",
        parent_content="章节二：模型",
    )
    FTS5IndexManager.index_chunk(
        conn,
        pg_id=3,
        user_id="user_b",
        doc_hash="doc_2",
        content="企业级知识库管理系统",
        parent_content="权限隔离测试",
    )

    # 1. 搜索“知识库” (user_a)
    hits_a = FTS5IndexManager.search(conn, query="知识库", top_k=5, user_id="user_a")
    assert len(hits_a) == 1
    assert hits_a[0]["pg_id"] == 1
    assert hits_a[0]["score"] > 0
    assert hits_a[0]["source"] == "local_keyword"
    assert hits_a[0]["engine"] == "fts5"

    # 2. 租户隔离校验：user_b 搜索“知识库”只能搜到 pg_id=3
    hits_b = FTS5IndexManager.search(conn, query="知识库", top_k=5, user_id="user_b")
    assert len(hits_b) == 1
    assert hits_b[0]["pg_id"] == 3

    # 3. 删除验证
    FTS5IndexManager.delete_by_doc_hash(conn, doc_hash="doc_1", user_id="user_a")
    hits_a_after = FTS5IndexManager.search(conn, query="知识库", top_k=5, user_id="user_a")
    assert len(hits_a_after) == 0

    conn.close()
