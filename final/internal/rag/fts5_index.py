"""SQLite FTS5 Full-Text Search Engine for RAG Chunks.

Inverted-index BM25 retrieval for local deployments without Elasticsearch.
Latency depends on corpus size, query shape, and hardware.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def fts_tokenize(text: str) -> str:
    """Tokenize mixed Chinese-English text for SQLite FTS5 unicode61 tokenizer.
    
    Splits CJK characters into individual space-separated tokens while
    preserving alphanumeric words, allowing exact substring/character recall.
    """
    if not text:
        return ""
    tokens: List[str] = []
    ascii_buf = ""
    for char in text.casefold():
        if "\u4e00" <= char <= "\u9fff":
            if ascii_buf:
                tokens.append(ascii_buf)
                ascii_buf = ""
            tokens.append(char)
        elif char.isalnum():
            ascii_buf += char
        elif ascii_buf:
            tokens.append(ascii_buf)
            ascii_buf = ""
    if ascii_buf:
        tokens.append(ascii_buf)
    return " ".join(tokens)


def build_fts_query(query: str) -> str:
    """Sanitize and build an FTS5 MATCH expression from user query."""
    # Keep the existing character index, but query adjacent Chinese characters
    # as phrases. A shared single character is not evidence for a whole term.
    tokens = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[^\W_]+", query.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part) and len(part) > 1:
            tokens.extend(part[i] + " " + part[i + 1] for i in range(len(part) - 1))
        else:
            tokens.append(part)
    tokens = list(dict.fromkeys(tokens))[:64]
    if not tokens:
        return ""
    # Filter out FTS5 reserved keywords/characters
    safe_tokens = [f'"{t}"' for t in tokens if t not in {'AND', 'OR', 'NOT', 'NEAR', '*'}]
    if not safe_tokens:
        return ""
    # Use OR or NEAR logic for flexible recall
    return " OR ".join(safe_tokens)


class FTS5IndexManager:
    """Manages SQLite FTS5 virtual table for RAG chunks."""

    TABLE_NAME = "agent_rag_chunks_fts"

    @classmethod
    def ensure_table(cls, conn: sqlite3.Connection) -> bool:
        """Ensure the FTS5 virtual table exists."""
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {cls.TABLE_NAME} USING fts5(
                    pg_id UNINDEXED,
                    user_id UNINDEXED,
                    doc_hash UNINDEXED,
                    content UNINDEXED,
                    parent_content UNINDEXED,
                    search_text,
                    tokenize = 'unicode61'
                );
            """)
            conn.commit()
            return True
        except Exception:
            return False

    @classmethod
    def index_chunk(
        cls,
        conn: sqlite3.Connection,
        pg_id: int,
        user_id: str,
        doc_hash: str,
        content: str,
        parent_content: str = "",
    ) -> bool:
        """Insert or update a chunk into the FTS5 table."""
        try:
            cls.ensure_table(conn)
            conn.execute(
                f"DELETE FROM {cls.TABLE_NAME} WHERE pg_id = ? AND user_id = ?",
                (str(pg_id), str(user_id)),
            )

            tokenized_search = fts_tokenize(f"{content}\n{parent_content}")

            conn.execute(
                f"""
                INSERT INTO {cls.TABLE_NAME} (pg_id, user_id, doc_hash, content, parent_content, search_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(pg_id), str(user_id), str(doc_hash), content, parent_content, tokenized_search),
            )
            conn.commit()
            return True
        except Exception:
            return False

    @classmethod
    def delete_by_pg_ids(cls, conn: sqlite3.Connection, pg_ids: List[int]) -> None:
        """Delete entries matching given pg_ids."""
        if not pg_ids:
            return
        try:
            placeholders = ",".join("?" for _ in pg_ids)
            conn.execute(
                f"DELETE FROM {cls.TABLE_NAME} WHERE pg_id IN ({placeholders})",
                [str(pid) for pid in pg_ids],
            )
            conn.commit()
        except Exception:
            pass

    @classmethod
    def delete_by_doc_hash(cls, conn: sqlite3.Connection, doc_hash: str, user_id: str) -> None:
        """Delete entries matching doc_hash and user_id."""
        try:
            conn.execute(
                f"DELETE FROM {cls.TABLE_NAME} WHERE doc_hash = ? AND user_id = ?",
                (str(doc_hash), str(user_id)),
            )
            conn.commit()
        except Exception:
            pass

    @classmethod
    def search(
        cls,
        conn: sqlite3.Connection,
        query: str,
        top_k: int = 5,
        user_id: str = "default_user",
    ) -> List[Dict[str, Any]]:
        """Execute BM25 search over FTS5 table."""
        fts_query = build_fts_query(query)
        if not fts_query:
            return []

        limit = max(1, min(int(top_k), 100))
        try:
            cursor = conn.execute(
                f"""
                SELECT pg_id, content, parent_content, bm25({cls.TABLE_NAME}) as rank
                FROM {cls.TABLE_NAME}
                WHERE {cls.TABLE_NAME} MATCH ? AND user_id = ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (f"search_text : ({fts_query})", str(user_id), limit),
            )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                pg_id_str, raw_content, raw_parent, rank = row
                strength = max(0.0, -float(rank)) if rank is not None else 0.0
                score = strength / (1.0 + strength)
                results.append({
                    "pg_id": int(pg_id_str),
                    "content": raw_content,
                    "parent_content": raw_parent,
                    "score": score,
                    "source": "local_keyword",
                    "engine": "fts5",
                })
            return results
        except Exception:
            return []
