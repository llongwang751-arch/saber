"""Built-in workers for the Agentic-RAG report pipeline.

This module mirrors the Go ``research -> writer -> review -> doc`` workflow.
The registry is process-local, while document/RAG persistence remains scoped by
the owning :class:`UnifiedAgent` instance.
"""

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from internal.document.library import DOCUMENT_SOURCE_AGENT, WriteRequest


@dataclass
class SubAgentTask:
    id: str = ""
    goal: str = ""
    query: str = ""
    upstream: Dict[str, str] = field(default_factory=dict)


class SubAgentRegistry:
    def __init__(self):
        self._agents: Dict[str, object] = {}
        self._lock = threading.RLock()

    def register(self, agent) -> None:
        with self._lock:
            self._agents[agent.name()] = agent

    def get(self, name: str):
        with self._lock:
            return self._agents.get(name)

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._agents)


def register_builtin_subagents(agent) -> SubAgentRegistry:
    """Register the workers shipped by the implementation."""

    registry = SubAgentRegistry()
    for subagent in (
        ResearchAgent(agent),
        WriterAgent(agent),
        ReviewAgent(agent),
        DocAgent(agent),
    ):
        registry.register(subagent)
    return registry


def register_medical_subagents(agent, registry: Optional[SubAgentRegistry] = None) -> SubAgentRegistry:
    """Register medical domain subagents."""
    if registry is None:
        registry = SubAgentRegistry()
    for subagent in (
        MedicalTriageAgent(agent),
        DrugSafetyReviewAgent(agent),
    ):
        registry.register(subagent)
    return registry


class ResearchAgent:
    def __init__(self, agent):
        self.agent = agent

    def name(self) -> str:
        return "research_agent"

    def description(self) -> str:
        return "Agentic RAG 研究员：多轮改写、知识库/搜索检索、证据整理。"

    def run(self, task: SubAgentTask) -> str:
        queries = self._plan_queries(task)
        observations: List[str] = []
        evidence: List[str] = []

        for query in queries:
            rag = getattr(self.agent, "rag", None)
            if rag is not None and getattr(rag, "loaded", False):
                history_fn = getattr(self.agent, "_recent_history_for_rag", None)
                history = history_fn() if callable(history_fn) else []
                if hasattr(rag, "query_with_history"):
                    answer, results = rag.query_with_history(query, history)
                else:
                    answer, results = rag.query(query)
                observations.append(f"Query: {query}\nRAG Answer: {answer}")
                for item in results or []:
                    content = _result_content(item)
                    if content:
                        evidence.append("- " + _first_runes(content, 180))
                continue

            tool_executor = getattr(self.agent, "tool_executor", None)
            tools = tool_executor.snapshot() if tool_executor is not None else {}
            search = tools.get("search_web")
            if search is not None:
                try:
                    result = search.func({"query": query})
                except Exception:
                    continue
                observations.append(f"Query: {query}\nSearch Result: {result}")

        if not observations:
            observations.append("未找到可用知识库或搜索结果。")
        observations_text = "\n\n".join(observations)
        evidence_text = "\n".join(evidence)
        user_msg = (
            f"研究目标：{task.goal}\n原始问题：{task.query}\n\n"
            f"观察结果：\n{observations_text}\n\n"
            f"证据片段：\n{evidence_text}"
        )
        if not _is_real_llm(self.agent):
            return "## Research Findings\n\n" + user_msg
        return _generate_fast(
            self.agent,
            "你是 research_agent。请基于观察结果输出结构化研究摘要，包含 Findings、Evidence、Open Questions。不要编造未出现的信息。",
            user_msg,
        )

    def _plan_queries(self, task: SubAgentTask) -> List[str]:
        base = (task.goal or task.query or "").strip()
        if not _is_real_llm(self.agent):
            return [base] if base else []
        raw = _generate_fast(
            self.agent,
            '你是查询规划器。请把研究目标改写成 2-3 条互补检索查询，严格输出 JSON：{"queries":["..."]}',
            base,
        )
        try:
            parsed = json.loads(_clean_json(raw))
            candidates = parsed.get("queries") if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            candidates = None
        if not isinstance(candidates, list) or not candidates:
            return [base] if base else []
        return _dedup_strings([base, *(str(query).strip() for query in candidates)])[:3]


class WriterAgent:
    def __init__(self, agent):
        self.agent = agent

    def name(self) -> str:
        return "writer_agent"

    def description(self) -> str:
        return "将上游研究结果整理为 Markdown 报告。"

    def run(self, task: SubAgentTask) -> str:
        material = _upstream_text(task)
        if not _is_real_llm(self.agent):
            return "# " + _safe_title(task.goal, task.query) + "\n\n" + material
        return _generate_fast(
            self.agent,
            "你是 writer_agent。请把输入整理为清晰 Markdown 报告，包含摘要、分析、建议和下一步。",
            f"写作目标：{task.goal}\n\n材料：\n{material}",
        )


class ReviewAgent:
    def __init__(self, agent):
        self.agent = agent

    def name(self) -> str:
        return "review_agent"

    def description(self) -> str:
        return "检查报告结构、事实一致性、证据覆盖和风险。"

    def run(self, task: SubAgentTask) -> str:
        material = _upstream_text(task)
        if not _is_real_llm(self.agent):
            return "Review: 内容已整理；建议人工确认关键事实。"
        return _generate_fast(
            self.agent,
            "你是 review_agent。请审查输入，输出问题清单、可信度和需要补证据的点。",
            material,
        )


class DocAgent:
    def __init__(self, agent):
        self.agent = agent

    def name(self) -> str:
        return "doc_agent"

    def description(self) -> str:
        return "将上游结果保存到本地文档库，并同步写入 RAG。"

    def run(self, task: SubAgentTask) -> str:
        content = _document_content(task) or task.query
        title = _document_title(content, task.goal, task.query)
        result = self.agent.write_document(
            WriteRequest(
                title=title,
                doc_type="report",
                source=DOCUMENT_SOURCE_AGENT,
                created_by=self.name(),
                content_md=content,
                summary=_first_runes(content, 180),
                metadata={
                    "sub_agent": self.name(),
                    "task_id": task.id,
                    "review": _first_runes(
                        _upstream_by_agent(task, "review_agent"), 1200
                    ),
                },
            ),
            True,
        )
        return json.dumps(_jsonable(result), ensure_ascii=False, indent=2)


def _generate_fast(agent, system_prompt: str, user_msg: str) -> str:
    generate = getattr(agent, "_llm_generate_fast", None)
    if not callable(generate):
        generate = agent._llm_generate
    return str(generate(system_prompt, user_msg))


def _result_content(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("content") or item.get("parent") or "").strip()
    chunk = getattr(item, "chunk", None)
    return str(getattr(chunk, "content", "") or getattr(item, "content", "")).strip()


def _upstream_text(task: SubAgentTask) -> str:
    if not task.upstream:
        return task.query
    return "\n\n".join(
        f"## {key}\n\n{task.upstream[key]}" for key in sorted(task.upstream)
    ).strip()


def _document_content(task: SubAgentTask) -> str:
    writer = _upstream_by_agent(task, "writer_agent")
    if writer.strip():
        return _strip_markdown_fence(writer)
    for key in sorted(task.upstream):
        value = (task.upstream[key] or "").strip()
        if value:
            return _strip_markdown_fence(value)
    return task.query.strip()


def _upstream_by_agent(task: SubAgentTask, agent_name: str) -> str:
    for key in sorted(task.upstream):
        if agent_name in key:
            return task.upstream[key] or ""
    return ""


def _document_title(content: str, goal: str, query: str) -> str:
    for text in (query, goal):
        explicit = _explicit_requested_title(text)
        if explicit:
            return _first_runes(explicit, 80)
    heading = _markdown_title(content)
    if heading:
        return _first_runes(heading, 80)
    return _safe_title("", query or goal)


def _markdown_title(content: str) -> str:
    content = _strip_markdown_fence(content)
    fallback = ""
    in_fence = False
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if not match:
            continue
        title = match.group(2).strip("# \t*_`")
        if _is_generic_heading(title):
            continue
        if len(match.group(1)) == 1:
            return title
        if not fallback:
            fallback = title
    return fallback


def _explicit_requested_title(text: str) -> str:
    text = (text or "").strip()
    pairs = (
        ("标题为《", "》"), ("标题是《", "》"), ("题为《", "》"),
        ('标题为"', '"'), ('标题是"', '"'), ('题为"', '"'),
    )
    for marker, end_marker in pairs:
        start = text.find(marker)
        if start < 0:
            continue
        rest = text[start + len(marker):]
        stop = rest.find(end_marker)
        if stop > 0:
            return rest[:stop].strip()
    return ""


def _strip_markdown_fence(text: str) -> str:
    trimmed = (text or "").strip()
    lines = trimmed.splitlines()
    if (
        len(lines) >= 2
        and lines[0].strip().startswith("```")
        and lines[-1].strip().startswith("```")
    ):
        return "\n".join(lines[1:-1]).strip()
    return trimmed


def _is_generic_heading(title: str) -> bool:
    value = (title or "").strip().lower()
    if not value or (len(value) <= 3 and value.startswith("n")):
        return True
    return value in {
        "摘要", "分析", "建议", "下一步", "结论", "review", "findings",
        "evidence", "open questions", "research findings",
    }


def _safe_title(goal: str, query: str) -> str:
    title = (goal or query or "").strip()
    for prefix in ("生成", "撰写"):
        if title.startswith(prefix):
            title = title[len(prefix):].strip()
    return _first_runes(title or "Agent Report", 60)


def _first_runes(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "..."


def _dedup_strings(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        key = (value or "").strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def _clean_json(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```json", "", text)
    text = re.sub(r"^```", "", text)
    text = re.sub(r"```$", "", text)
    return text.strip()


def _is_real_llm(agent) -> bool:
    fn = getattr(getattr(agent, "cfg", None), "is_real_llm", None)
    return bool(callable(fn) and fn())


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value


class MedicalTriageAgent:
    """分诊评估子智能体：解析主诉与病史，检查急症红线与缺失诊断要素。"""

    def __init__(self, agent):
        self.agent = agent

    def name(self) -> str:
        return "medical_triage_agent"

    def description(self) -> str:
        return "临床分诊专家：解析患者主诉、病程与既往史，筛查急症红线与缺失诊断要素。"

    def run(self, task: SubAgentTask) -> str:
        query = task.query or task.goal
        from internal.application.medical import check_emergency_redline
        emergency = check_emergency_redline(query)
        if emergency:
            return f"🚨【S0急危重症拦截】{emergency['condition_name']}：{emergency['action_guide']}"
        return f"🩺【预问诊分析】已解析患者主诉：'{query}'。建议结合发病时长、伴随症状与既往史展开鉴别诊断。"


class DrugSafetyReviewAgent:
    """用药安全审查子智能体：确定性排查配伍禁忌与过敏冲突。"""

    def __init__(self, agent):
        self.agent = agent

    def name(self) -> str:
        return "drug_safety_agent"

    def description(self) -> str:
        return "临床用药安全审查专家：确定性筛查药物配伍禁忌 (DDI) 与已知过敏原交叉反应。"

    def run(self, task: SubAgentTask) -> str:
        query = task.query or task.goal
        from internal.application.medical import check_drug_safety
        known_drugs = ["华法林", "阿司匹林", "硝酸甘油", "西地那非", "二甲双胍", "奥美拉唑", "氯吡格雷", "螺内酯", "辛伐他汀", "克拉霉素", "青霉素", "头孢", "造影剂"]
        drugs = [d for d in known_drugs if d in query]
        allergies = [a for a in ["青霉素", "头孢", "磺胺"] if a in query and "过敏" in query]
        res = check_drug_safety(drugs, allergies)
        return json.dumps(res, ensure_ascii=False)
