"""_PGAdapter 断线重连与熔断行为（不需要真实 PostgreSQL）。"""
import psycopg2
import pytest

from internal.infra.infra import _PGAdapter


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._conn.dead:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        self._conn.executed.append(sql)

    def fetchall(self):
        return [("row",)]

    def fetchone(self):
        return ("row",)


class _FakeConn:
    def __init__(self, dead=False):
        self.dead = dead
        self.executed = []
        self.closed = False
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0

    def close(self):
        self.closed = True

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_pg_adapter_reconnects_bootstrap_connection_on_dead_conn():
    dead = _FakeConn(dead=True)
    fresh = _FakeConn(dead=False)
    adapter = _PGAdapter(dead, connect_fn=lambda: fresh)

    rows = adapter.query("SELECT 1")

    assert rows == [("row",)]
    assert dead.closed is True
    assert adapter.conn is fresh


def test_pg_adapter_breaker_opens_after_consecutive_connection_failures():
    connect_calls = {"n": 0}

    def always_dead():
        connect_calls["n"] += 1
        return _FakeConn(dead=True)

    adapter = _PGAdapter(_FakeConn(dead=True), connect_fn=always_dead)

    for _ in range(3):
        assert adapter.query("SELECT 1") == []  # 每次失败降级为空结果

    snapshot = adapter._breaker.snapshot()
    assert snapshot.state == "open"

    before = connect_calls["n"]
    assert adapter.query("SELECT 1") == []  # 熔断打开后不再尝试连接
    assert connect_calls["n"] == before


def test_pg_adapter_transaction_discards_dead_pool_connection_and_retries():
    dead = _FakeConn(dead=True)
    healthy = _FakeConn(dead=False)
    pool_conns = [dead, healthy]
    discarded = []

    class _FakePool:
        def getconn(self):
            return pool_conns.pop(0)

        def putconn(self, conn, close=False):
            if close:
                discarded.append(conn)

    adapter = _PGAdapter(_FakeConn(dead=False), pool=_FakePool())
    with adapter.transaction() as conn:
        conn.executed.append("INSERT INTO t VALUES (1)")

    assert discarded == [dead]
    assert healthy.commits == 1
    assert any("INSERT INTO t" in sql for sql in healthy.executed)


def test_pg_adapter_transaction_fails_closed_when_no_healthy_connection():
    pool_conns = [_FakeConn(dead=True), _FakeConn(dead=True)]

    class _FakePool:
        def getconn(self):
            return pool_conns.pop(0)

        def putconn(self, conn, close=False):
            pass

    adapter = _PGAdapter(_FakeConn(dead=False), pool=_FakePool())
    with pytest.raises(RuntimeError, match="postgres unavailable"):
        with adapter.transaction() as conn:
            pass
