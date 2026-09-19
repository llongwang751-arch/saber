"""Ranking and tenant oracles independent of the optimized search loop."""
from collections import Counter
import math
import random
import re

from sqlalchemy import event

from internal.application.local_repos import LocalRagChunkRepo
from internal.application.store import ApplicationStore


def reference(query, content):
    # Deliberately use a regex implementation, rather than the production
    # character loop. Corpus covers ASCII tokens and individual Chinese chars.
    def counts(value):
        return Counter(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", value.casefold()))
    left, right = counts(query), counts(content)
    if not left or not right:
        return 0
    return sum(a * right.get(t, 0) for t, a in left.items()) / (
        math.sqrt(sum(a*a for a in left.values())) * math.sqrt(sum(b*b for b in right.values())))


def test_streamed_search_matches_independent_ranking_and_excludes_vectors():
    store = ApplicationStore("sqlite:///:memory:")
    try:
        tenant = store.create_user("search-owner", "unused")["id"]
        other = store.create_user("search-other", "unused")["id"]
        repo = LocalRagChunkRepo(store)
        randomizer = random.Random(912)
        corpus = []
        words = ["预算", "启动", "报销", "员工", "apple", "policy", "budget", "320"]
        for i in range(150):
            text = " ".join(randomizer.choices(words, k=6))
            parent = " ".join(randomizer.choices(words, k=3))
            key = repo.save_pg_with_parent(str(i), 0, text, parent, "[0.1, 0.2]", user_id=tenant)
            corpus.append((key, text, parent))
        repo.save_pg_with_parent("private", 0, "secret", "secret", "[]", user_id=other)
        queries = ["预算 320", "policy", "apple apple budget", "启动 员工", "notfound", "!!!", ""]
        sql = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            sql.append(statement)
        event.listen(store.engine, "before_cursor_execute", capture)
        for query in queries:
            for k in [1, 3, 25, 200, 0]:
                expected = [(key, reference(query, f"{body}\n{parent}")) for key, body, parent in corpus]
                expected = sorted((pair for pair in expected if pair[1] > 0), key=lambda pair: (-pair[1], pair[0]))[:max(1, min(k, 100))]
                actual = repo.search_local(query, k, user_id=tenant)
                assert [item["pg_id"] for item in actual] == [pair[0] for pair in expected]
                assert [item["score"] for item in actual] == [pair[1] for pair in expected]
        assert not repo.search_local("secret", 10, user_id=tenant)
        assert sql and all("embedding" not in statement for statement in sql)
        assert all("user_id =" in statement for statement in sql)
    finally:
        store.close()


def test_search_sees_committed_update_and_delete_without_stale_cache():
    store = ApplicationStore("sqlite:///:memory:")
    try:
        tenant = store.create_user("freshness", "unused")["id"]
        repo = LocalRagChunkRepo(store)
        key = repo.save_pg_with_parent("doc", 0, "oldtoken", "", "[]", user_id=tenant)
        assert repo.search_local("oldtoken", 1, user_id=tenant)[0]["pg_id"] == key
        repo.save_pg_with_parent("doc", 0, "newtoken", "", "[]", user_id=tenant)
        assert not repo.search_local("oldtoken", 1, user_id=tenant)
        assert repo.search_local("newtoken", 1, user_id=tenant)[0]["pg_id"] == key
        repo.delete("doc", user_id=tenant)
        assert not repo.search_local("newtoken", 1, user_id=tenant)
    finally:
        store.close()
