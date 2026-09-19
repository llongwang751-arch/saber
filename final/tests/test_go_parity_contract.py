"""Go 主线到 Python 复刻版的可执行行为合同。"""

from types import SimpleNamespace

from config.config import APIConfig
from internal.agent.agent import ChatOptions, Response, UnifiedAgent
from internal.agent.artifact import prepare_artifact_workspace, produce_artifact
from internal.agent.cancel import CancelToken
from internal.agent.graph_runtime import GraphConfig, GraphRuntime
from internal.agent.planner import (
    llm_plan_graph,
    rule_plan_nodes,
    subagent_pipeline_nodes,
)
from internal.agent.subagents import register_builtin_subagents
from internal.graph.task_graph import Node, NodeStatus, NodeType, TaskGraph
from internal.handler.handler import ChatRequest
from internal.tools.tools import Tool, default_tools, new_mcp_tool


def test_chat_request_matches_go_wire_contract():
    assert set(ChatRequest.model_fields) == {"message", "use_rag", "conversation_id"}
    assert set(ChatOptions.__dataclass_fields__) == {"use_rag", "conversation_id"}


def test_default_builtin_tools_match_go_current_set():
    assert [tool.name for tool in default_tools()] == ["search_web"]


def test_route_matrix_matches_go_agentic_rag_contract():
    agent = object.__new__(UnifiedAgent)
    agent.tool_executor = SimpleNamespace(snapshot=lambda: {"search_web": object()})

    agent.rag = SimpleNamespace(loaded=False)
    assert agent._route_decide("普通问题", ChatOptions(use_rag=True))[0] == "react"

    agent.rag.loaded = True
    assert agent._route_decide("上传材料讲了什么", ChatOptions(use_rag=True))[0] == "rag"
    for keyword in ["研究", "调研", "总结", "报告", "文档", "方案", "分析"]:
        assert agent._route_decide(
            f"请{keyword}这份材料", ChatOptions(use_rag=True)
        )[0] == "rag_agent"
    assert agent._route_decide(
        "根据材料生成 Markdown 报告并保存到本地文档库",
        ChatOptions(use_rag=True),
    )[0] == "rag_agent"
    assert agent._route_decide("生成报告", ChatOptions(use_rag=False))[0] == "react"


def test_regular_react_never_hard_routes_to_subagents():
    agent = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False))
    nodes = rule_plan_nodes(
        agent, "调研并生成报告", {"search_web": Tool("search_web", "", [], lambda _p: "ok")},
    )
    assert all(node.type != NodeType.SUBAGENT for node in nodes)


def test_regular_react_hides_and_rejects_llm_subagents():
    class LLM:
        prompt = ""

        def chat_fast(self, messages, system_prompt=""):
            self.prompt = messages[0].content
            return '[{"id":"n1","type":"sub_agent","agent":"research_agent"}]'

    llm = LLM()
    agent = SimpleNamespace(
        cfg=SimpleNamespace(is_real_llm=lambda: True),
        llm=llm,
    )

    nodes = llm_plan_graph(agent, "生成报告", {}, "", allow_subagents=False)

    assert nodes == []
    assert "子 Agent" not in llm.prompt


def test_agentic_rag_fixed_graph_matches_go_roles_and_dependencies():
    nodes = subagent_pipeline_nodes("根据材料生成报告")

    assert [node.tool_name for node in nodes] == [
        "research_agent", "writer_agent", "review_agent", "doc_agent",
    ]
    assert [node.depends_on for node in nodes] == [
        [], ["n1"], ["n2"], ["n2", "n3"],
    ]


def test_builtin_registry_matches_go_current_set():
    agent = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False))
    assert set(register_builtin_subagents(agent).snapshot()) == {
        "research_agent", "writer_agent", "review_agent", "doc_agent",
    }


def test_dispatch_enables_fixed_subagents_only_for_rag_agent():
    calls = []
    agent = object.__new__(UnifiedAgent)

    def run(*_args, **kwargs):
        calls.append(kwargs)
        return "ok", [], None

    agent._run_react_with_tools = run
    agent._apply_graph_tool_calls = lambda _response: None
    base = {
        "query": "生成报告",
        "route_tools": {},
        "mem_prefix": "",
        "hist_msgs": [],
        "extracted": "",
        "runtime_overrides": {},
    }

    agent._dispatch_mode(
        {**base, "mode": "react"}, Response(query="生成报告"), CancelToken()
    )
    agent._dispatch_mode(
        {**base, "mode": "rag_agent"}, Response(query="生成报告"), CancelToken()
    )

    assert calls[0]["allow_subagents"] is False
    assert "force_subagent_plan" not in calls[0]
    assert calls[1]["allow_subagents"] is True
    assert calls[1]["force_subagent_plan"] is True


def test_graph_runtime_plan_and_react_appends_valid_node():
    class LLM:
        def chat_fast(self, _messages, system_prompt=""):
            return '[{"id":"r1","type":"tool","tool":"second","params":{},"reason":"补充","depends_on":["n1"]}]'

    class Agent:
        cfg = SimpleNamespace(is_real_llm=lambda: True, max_retries=1, retry_delay_ms=0)
        llm = LLM()
        subagents = SimpleNamespace(snapshot=lambda: {}, get=lambda _name: None)

        @staticmethod
        def save_snapshot(_task):
            return None

    events = []
    graph = TaskGraph([Node(id="n1", tool_name="first")])
    tools = {
        "first": Tool("first", "", [], lambda _p: "one"),
        "second": Tool("second", "", [], lambda _p: "two"),
    }
    runtime = GraphRuntime(
        graph, Agent(), GraphConfig(replan_enabled=True, max_replan=1),
        tools, {"query": "q"}, on_event=events.append,
    )
    runtime.set_replan_context("q", "", False)
    result = runtime.execute(CancelToken())

    assert graph.nodes["r1"].status == NodeStatus.DONE
    assert result.observations == ["one", "two"]
    assert [event for event in events if event["type"] == "replan"][0]["data"]["used_count"] == 1


def test_artifact_workspace_and_materialization_match_go_fallback(tmp_path):
    cfg = APIConfig()
    cfg.sandbox_artifact_host_dir = str(tmp_path)
    agent = SimpleNamespace(
        cfg=cfg, user_id="user/../1", sandbox=None,
        llm=SimpleNamespace(),
    )
    workspace = prepare_artifact_workspace(agent, "task/../1")
    step = produce_artifact(agent, "生成一份测试报告", "# 正文", workspace)

    assert step is not None
    assert "已生成文件" in step["content"]
    files = list((tmp_path / "user___1" / "task___1").glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "# 正文"


def test_mcp_tool_posts_json_to_registered_endpoint(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = '{"ok":true}'

    def post(url, json, timeout):
        captured.update(url=url, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("internal.tools.tools.requests.post", post)
    tool = new_mcp_tool("remote", "", [], endpoint="https://mcp.example/tool")

    assert tool.func({"input": "x"}) == '{"ok":true}'
    assert captured == {
        "url": "https://mcp.example/tool", "json": {"input": "x"}, "timeout": 30,
    }
