from types import SimpleNamespace
from datetime import timedelta

from internal.agent.agent import ReActStep, Response, UnifiedAgent
from internal.application.local_repos import LocalTraceRepo
from internal.application.store import ApplicationStore
from internal.application.models import AgentTraceRecord, utcnow
from internal.observability import redact_text, sanitize_trace


def test_trace_redaction_removes_secrets_and_personal_data():
    trace = sanitize_trace({
        "api_key": "sk-secret-value",
        "params": {
            "authorization": "Bearer very-secret-token",
            "contact": "手机 13760251527，邮箱 a@example.com",
        },
    })

    assert trace["api_key"] == "[REDACTED]"
    assert trace["params"]["authorization"] == "[REDACTED]"
    assert "13760251527" not in trace["params"]["contact"]
    assert "a@example.com" not in trace["params"]["contact"]
    assert "sk-secret" not in redact_text("token=sk-secret-value")


def test_trace_repository_is_idempotent_and_tenant_scoped(tmp_path):
    store = ApplicationStore(database_url=f"sqlite:///{tmp_path / 'trace.db'}")
    try:
        user_a = store.create_user("trace_user_a", "hash")
        user_b = store.create_user("trace_user_b", "hash")
        repo = LocalTraceRepo(store)
        repo.save("trace-1", user_a["id"], "rag", "问题", "completed", {"decision": "answer"})
        repo.save("trace-1", user_a["id"], "rag", "问题2", "completed", {"decision": "no_answer"})

        assert len(repo.list(user_a["id"])) == 1
        assert repo.get(user_a["id"], "trace-1")["trace"]["decision"] == "no_answer"
        assert repo.get(user_b["id"], "trace-1") is None
    finally:
        store.close()


def test_agent_finalize_persists_a_sanitized_structured_trace(tmp_path):
    store = ApplicationStore(database_url=f"sqlite:///{tmp_path / 'agent-trace.db'}")
    try:
        user = store.create_user("trace_agent", "hash")
        trace_repo = LocalTraceRepo(store)
        agent = UnifiedAgent.__new__(UnifiedAgent)
        agent.inf = SimpleNamespace(repo=SimpleNamespace(ragtrace=trace_repo))
        agent.user_id = user["id"]

        response = Response(
            query="联系电话 13760251527",
            mode="rag",
            trace_id="trace-agent-1",
            rag_trace={"api_key": "sk-private-key", "decision": "answer"},
            steps=[ReActStep(type="Action", content="调用检索")],
            tool_call={"authorization": "Bearer private-token"},
        )
        agent._persist_trace(response.query, response)

        persisted = trace_repo.get(user["id"], response.trace_id)
        assert persisted is not None
        assert "13760251527" not in persisted["query"]
        assert persisted["trace"]["rag"]["api_key"] == "[REDACTED]"
        assert persisted["trace"]["tool_call"]["authorization"] == "[REDACTED]"
        assert persisted["trace"]["steps"][0]["type"] == "Action"
    finally:
        store.close()


def test_trace_retention_and_user_delete_are_tenant_scoped(tmp_path):
    store = ApplicationStore(database_url=f"sqlite:///{tmp_path / 'trace-retention.db'}")
    try:
        user_a = store.create_user("trace_retention_a", "hash")
        user_b = store.create_user("trace_retention_b", "hash")
        repo = LocalTraceRepo(store, retention_days=30)
        repo.save("old-a", user_a["id"], "rag", "旧问题", "completed", {})
        repo.save("old-b", user_b["id"], "rag", "旧问题", "completed", {})
        repo.save("new-a", user_a["id"], "rag", "新问题", "completed", {})
        with store.transaction() as session:
            for trace_id in ("old-a", "old-b"):
                session.get(AgentTraceRecord, trace_id).created_at = utcnow() - timedelta(days=31)

        assert repo.purge_expired(user_a["id"]) == 1
        assert repo.get(user_a["id"], "old-a") is None
        assert repo.get(user_b["id"], "old-b") is not None
        assert repo.delete(user_a["id"], "new-a") is True
        assert repo.delete(user_b["id"], "new-a") is False
    finally:
        store.close()
