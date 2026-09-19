"""Regression tests for Milvus insert/upsert adapter compatibility."""

from internal.infra.infra import _MilvusAdapter
from internal.platform.milvus import MilvusClientWrapper


class _Client:
    def __init__(self, *, native_upsert: bool = True):
        self.native_upsert = native_upsert
        self.calls = []

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))

    def upsert(self, **kwargs):
        if not self.native_upsert:
            raise AttributeError("upsert unavailable")
        self.calls.append(("upsert", kwargs))

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))


def test_platform_wrapper_insert_still_executes_after_upsert_was_added():
    client = _Client()
    wrapper = MilvusClientWrapper.__new__(MilvusClientWrapper)
    wrapper._client = client

    assert wrapper.insert("rag_chunks", [{"pg_id": 1}]) is True
    assert client.calls == [("insert", {"collection_name": "rag_chunks", "data": [{"pg_id": 1}]})]


def test_platform_and_infrastructure_adapters_expose_native_upsert():
    for wrapper in (MilvusClientWrapper.__new__(MilvusClientWrapper), _MilvusAdapter(_Client())):
        if isinstance(wrapper, MilvusClientWrapper):
            wrapper._client = _Client()
        assert wrapper.upsert(
            "rag_chunks",
            [{"pg_id": 1, "user_id": "tenant_alpha", "embedding": [0.1]}],
        ) is True
        assert wrapper._client.calls[0][0] == "upsert"
