from types import SimpleNamespace

from internal.agent.agent import ChatOptions, Response, UnifiedAgent
from internal.agent.cancel import CancelRegistry


class _LLM:
    def __init__(self):
        self.chat_called = False
        self.stream_called = False

    def chat(self, _messages, system_prompt=""):
        self.chat_called = True
        return "sync-answer"

    def chat_stream_context(self, _ctx, _system_prompt, _messages, on_token=None):
        self.stream_called = True
        if on_token:
            on_token("真")
            on_token("流")
        return "真流"


class _STM:
    def __init__(self):
        self.items = []

    def add(self, role, content):
        self.items.append({"role": role, "content": content})

    def get(self):
        return list(self.items)

    def count(self):
        return len(self.items)


class _Preference:
    def get_all(self):
        return {}


class _LTM:
    items = []

    def recall(self, _query, _top_k):
        return []


class _MemoryWriter:
    def submit(self, _fn):
        pass


class _Events:
    def publish(self, _event, _payload):
        pass


def _agent():
    agent = object.__new__(UnifiedAgent)
    agent.cfg = SimpleNamespace(
        long_term_top_k=3, snapshot_every_turns=99,
        is_real_llm=lambda: False,
    )
    agent.llm = _LLM()
    agent.stm = _STM()
    agent.ltm = _LTM()
    agent.preference = _Preference()
    agent.memory_writer = _MemoryWriter()
    agent.chat_repo = None
    agent.rag = None
    agent.tool_executor = SimpleNamespace(snapshot=lambda: {})
    agent._build_context_prefix = lambda _query, _mode: ""
    agent.inf = SimpleNamespace(repo=SimpleNamespace(events=_Events()))
    agent._cancel_registry = CancelRegistry()
    agent._turn_count = 0
    agent._snapshot_every = 99
    return agent


def test_process_stream_chat_mode_uses_llm_stream_context():
    agent = _agent()
    events = []

    resp = agent.process_stream("你好", ChatOptions(), events.append)

    assert resp.answer == "真流"
    assert agent.llm.stream_called is True
    assert agent.llm.chat_called is False
    assert [e for e in events if e["type"] == "token"] == [
        {"type": "token", "data": {"content": "真"}},
        {"type": "token", "data": {"content": "流"}},
    ]
    assert events[-1]["type"] == "done"


def test_saved_report_without_rag_routes_to_regular_react(monkeypatch):
    agent = _agent()
    agent.rag = SimpleNamespace(loaded=True)
    all_tools = {"write_document": object(), "rag_search": object()}
    agent.tool_executor = SimpleNamespace(snapshot=lambda: all_tools)
    monkeypatch.setattr("internal.agent.agent.async_update_memory", lambda *_args, **_kwargs: None)

    prepared = agent._prepare(
        "根据上传文档生成 Markdown 报告并保存到本地文档库",
        ChatOptions(use_rag=False),
    )

    assert prepared["mode"] == "react"
    assert prepared["route_tools"] == all_tools


def test_rag_mode_emits_structured_trace_before_results():
    agent = _agent()

    class _RAG:
        loaded = True

        @staticmethod
        def query_with_history_trace(query, _history):
            return "answer", [{"pg_id": 1, "content": "evidence", "score": 0.9}], {
                "original_query": query,
                "rewritten_queries": [query],
                "decision": "answer",
                "selected_evidence": [{"pg_id": 1, "score": 0.9}],
            }

    agent.rag = _RAG()
    response = Response(query="q", mode="rag")
    events = []
    token = SimpleNamespace(is_cancelled=lambda: False)

    agent._dispatch_mode(
        {
            "query": "q", "mode": "rag", "route_tools": None,
            "mem_prefix": "", "hist_msgs": [], "extracted": "",
        },
        response,
        token,
        events.append,
    )

    assert response.rag_trace["decision"] == "answer"
    assert [event["type"] for event in events] == ["rag_trace", "rag_result", "token"]
