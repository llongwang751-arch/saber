import json
from types import SimpleNamespace

from internal.agent.cancel import CancelToken
from internal.agent.graph_runtime import GraphConfig, GraphRuntime
from internal.agent.planner import rule_plan_nodes, subagent_pipeline_nodes
from internal.agent.subagents import SubAgentTask, register_builtin_subagents
from internal.document.library import Document, DocumentVersion, WriteResult
from internal.graph.task_graph import NodeStatus, NodeType, TaskGraph


def test_register_builtin_subagents_includes_go_report_pipeline():
    agent = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False))

    registry = register_builtin_subagents(agent)

    assert set(registry.snapshot()) == {
        "research_agent",
        "writer_agent",
        "review_agent",
        "doc_agent",
    }


def test_research_agent_uses_rag_history_and_collects_evidence():
    agent = RecordingAgent()
    agent.rag = RecordingRAG()
    research = register_builtin_subagents(agent).get("research_agent")

    result = research.run(
        SubAgentTask(id="n1", goal="调研猪场生物安全", query="生成报告")
    )

    assert agent.rag.calls == [("调研猪场生物安全", ["previous turn"])]
    assert "可核验证据" in result
    assert "研究目标：调研猪场生物安全" in result


def test_research_agent_plans_at_most_three_deduplicated_queries():
    agent = RecordingAgent()
    agent.cfg.is_real_llm = lambda: True
    agent.rag = RecordingRAG()
    generated = iter([
        '```json\n{"queries":["补充查询", "调研目标", "边界查询"]}\n```',
        "结构化研究摘要",
    ])
    agent._llm_generate_fast = lambda _system, _user: next(generated)
    research = register_builtin_subagents(agent).get("research_agent")

    result = research.run(
        SubAgentTask(id="n1", goal="调研目标", query="原始问题")
    )

    assert [call[0] for call in agent.rag.calls] == [
        "调研目标", "补充查询", "边界查询",
    ]
    assert result == "结构化研究摘要"


def test_doc_agent_writes_writer_output_and_review_metadata_to_rag():
    agent = RecordingAgent()
    doc = register_builtin_subagents(agent).get("doc_agent")

    result = doc.run(
        SubAgentTask(
            id="n4",
            goal="保存报告",
            query="生成一份标题为《AGI周报》的报告",
            upstream={
                "n2:writer_agent": "# AGI周报\n\n正文",
                "n3:review_agent": "Review ok",
            },
        )
    )
    data = json.loads(result)

    assert agent.write_calls[0]["req"].title == "AGI周报"
    assert agent.write_calls[0]["req"].content_md == "# AGI周报\n\n正文"
    assert agent.write_calls[0]["req"].metadata["review"] == "Review ok"
    assert agent.write_calls[0]["req"].metadata["sub_agent"] == "doc_agent"
    assert agent.write_calls[0]["ingest"] is True
    assert data["document"]["id"] == "doc_1"


def test_rule_planner_requires_explicit_subagent_permission():
    agent = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False))
    query = "调研 Neo4j 并生成报告保存到文档库"

    ordinary = rule_plan_nodes(agent, query, {}, allow_subagents=False)
    agentic = rule_plan_nodes(agent, query, {}, allow_subagents=True)

    assert all(node.type != NodeType.SUBAGENT for node in ordinary)
    assert [node.tool_name for node in agentic] == [
        "research_agent",
        "writer_agent",
        "review_agent",
        "doc_agent",
    ]
    assert [node.depends_on for node in agentic] == [
        [], ["n1"], ["n2"], ["n2", "n3"],
    ]


def test_non_persistent_research_plan_uses_three_roles_like_go():
    agent = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False))

    nodes = rule_plan_nodes(
        agent,
        "调研 PPO 的优势，两句话即可",
        {},
        allow_subagents=True,
    )

    assert [node.tool_name for node in nodes] == [
        "research_agent", "writer_agent", "review_agent",
    ]


def test_graph_runtime_executes_all_four_roles_and_saves_document():
    agent = RecordingAgent()
    agent.rag = RecordingRAG()
    agent.subagents = register_builtin_subagents(agent)
    graph = TaskGraph(subagent_pipeline_nodes("生成标题为《猪场周报》的报告"))

    result = GraphRuntime(
        graph,
        agent,
        GraphConfig(max_parallel=2),
        {},
        {"query": "生成标题为《猪场周报》的报告"},
    ).execute(CancelToken())

    assert result.interrupted is False
    assert all(node.status == NodeStatus.DONE for node in graph.nodes.values())
    assert agent.write_calls
    assert agent.write_calls[0]["req"].title == "猪场周报"
    assert agent.write_calls[0]["ingest"] is True
    assert set(agent.last_subagent_task.upstream) == {
        "n2:writer_agent", "n3:review_agent",
    }


class RecordingRAG:
    loaded = True

    def __init__(self):
        self.calls = []

    def query_with_history(self, query, history):
        self.calls.append((query, history))
        return "研究结论", [{"content": "可核验证据"}]


class RecordingAgent:
    def __init__(self):
        self.cfg = SimpleNamespace(
            is_real_llm=lambda: False,
            max_retries=1,
            retry_delay_ms=0,
            step_timeout_ms=0,
        )
        self.rag = None
        self.tool_executor = SimpleNamespace(snapshot=lambda: {})
        self.write_calls = []
        self.last_subagent_task = None
        self.snapshots = []

    @staticmethod
    def _recent_history_for_rag():
        return ["previous turn"]

    def write_document(self, req, ingest_to_rag=False):
        self.write_calls.append({"req": req, "ingest": ingest_to_rag})
        doc = Document(
            id="doc_1",
            title=req.title,
            doc_type=req.doc_type,
            source=req.source,
            status="active",
            created_by=req.created_by,
            latest_version=1,
            latest_version_id="ver_1",
        )
        ver = DocumentVersion(
            id="ver_1",
            document_id="doc_1",
            version=1,
            content_md=req.content_md,
            summary=req.summary,
            metadata=req.metadata,
        )
        return WriteResult(document=doc, version=ver, created=True)

    def save_snapshot(self, task):
        self.snapshots.append(task)

    def _llm_generate(self, _system_prompt, user_msg):
        return user_msg
