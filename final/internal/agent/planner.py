# planner — UnifiedAgent 的工具规划器
#
# 对应 Go 版 internal/agent/planner.go：在 ReAct 模式下由 Planner LLM 根据
# 可用工具集和用户问题产出一组 PlanItem，Harness 再逐项重试执行。
# LLM 不可用或解析失败时降级到 rule_plan_items 关键字规则。
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List

from internal.graph.task_graph import Node, NodeType
from internal.llm.llm import Message

logger = logging.getLogger(__name__)


@dataclass
class PlanItem:
    """Planner LLM 输出的单个工具调用计划。"""
    tool: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def llm_plan_steps(agent, query: str, tools_map: Dict[str, Any], mem_prefix: str) -> List[PlanItem]:
    """调用 Planner LLM 选择需要调用的工具及参数。

    LLM 不可用或解析失败时降级到关键字规则。
    """
    if not agent.cfg.is_real_llm():
        return rule_plan_items(agent, query, tools_map)

    # 构造工具描述
    tool_lines: List[str] = []
    for name, t in tools_map.items():
        p_descs: List[str] = []
        for p in getattr(t, "params", []) or []:
            req = "（必填）" if p.get("required") else ""
            p_descs.append(f"{p.get('name','')}({p.get('type','string')}){req}")
        params = ", ".join(p_descs) if p_descs else "无"
        tool_lines.append(f"- {name}: {t.description} [参数: {params}]")

    plan_prompt = (
        "你是一个任务规划器。\n"
        "根据用户问题，从可用工具中选出真正需要调用的工具（不要为了用工具而用工具，按需选择）。\n"
        f"用户问题：{query}\n"
        f"可用工具：\n{chr(10).join(tool_lines)}\n"
        "请以 JSON 数组格式输出执行计划，格式如下：\n"
        '[{"tool":"工具名","params":{"参数名":"参数值"},"reason":"一句话说明为什么调用这个工具"}]\n'
        "如果无需工具直接回答，输出 []。只输出 JSON，不要其他内容。"
    )

    planner_base = "你是一个精准的任务规划器，只在必要时才调用工具，不做无意义的调用。"
    if mem_prefix:
        planner_base = mem_prefix + "\n\n" + planner_base + "\n注意：用户偏好可能影响工具参数选择（如城市、时区等），请在参数中体现。"

    try:
        # 规划属于内部步骤：优先快模型（未配置时 chat_fast 回退主模型）。
        raw = agent.llm.chat_fast(
            [Message(role="user", content=plan_prompt)],
            system_prompt=planner_base,
        )
    except Exception as e:
        logger.warning("Planner LLM 调用失败: %s，降级到规则", e)
        return rule_plan_items(agent, query, tools_map)

    # 清洗 LLM 输出
    raw = (raw or "").strip()
    # 剥离模型输出的 <|FunctionCallBegin|>...<|FunctionCallEnd|> 包装
    if "<|FunctionCallBegin|>" in raw:
        idx = raw.index("<|FunctionCallBegin|>") + len("<|FunctionCallBegin|>")
        raw = raw[idx:]
        if "<|FunctionCallEnd|>" in raw:
            raw = raw[: raw.index("<|FunctionCallEnd|>")]
    raw = re.sub(r"^```json", "", raw)
    raw = re.sub(r"^```", "", raw)
    raw = re.sub(r"```$", "", raw)
    raw = raw.strip()

    # 尝试解析为 [{"tool":...,"params":...}] 格式
    items: List[PlanItem] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for d in data:
                if not isinstance(d, dict):
                    continue
                if "tool" in d:
                    items.append(PlanItem(
                        tool=str(d.get("tool", "")),
                        params=dict(d.get("params") or {}),
                        reason=str(d.get("reason", "")),
                    ))
                elif "name" in d:
                    # 兼容部分模型的 function-calling 格式 [{"name":...,"parameters":...}]
                    items.append(PlanItem(
                        tool=str(d.get("name", "")),
                        params=dict(d.get("parameters") or {}),
                        reason="LLM 规划调用",
                    ))
    except Exception as e:
        logger.warning("⚠️  Planner LLM 解析失败 (%s)，降级到规则规划。原始: %s", e, raw)
        return rule_plan_items(agent, query, tools_map)

    # 过滤：只保留工具集中实际存在的工具
    valid: List[PlanItem] = []
    for item in items:
        if item.tool in tools_map and _tool_matches_query(item.tool, tools_map[item.tool], query, llm_selected=True):
            valid.append(item)
    return valid


def llm_plan_graph(
    agent, query: str, tools_map: Dict[str, Any], mem_prefix: str,
    allow_subagents: bool = False,
) -> List[Node]:
    """调用 Planner LLM 产出图节点，支持 depends_on 和 race_group。"""
    # 与 Go 主线一致：一旦显式允许子 Agent 且命中报告意图，使用确定性
    # DAG，避免 Planner LLM 漏掉审查/保存节点或改变依赖关系。
    if allow_subagents and needs_subagent_plan(query):
        return rule_plan_nodes(
            agent, query, tools_map, allow_subagents=allow_subagents
        )
    if not agent.cfg.is_real_llm():
        return rule_plan_nodes(agent, query, tools_map, allow_subagents=allow_subagents)

    tool_lines: List[str] = []
    for name, t in tools_map.items():
        p_descs: List[str] = []
        for p in getattr(t, "params", []) or []:
            req = "（必填）" if p.get("required") else ""
            p_descs.append(f"{p.get('name','')}({p.get('type','string')}){req}")
        params = ", ".join(p_descs) if p_descs else "无"
        tool_lines.append(f"- {name}: {getattr(t, 'description', '')} [参数: {params}]")

    agent_section = ""
    planner_scope = "可用工具"
    node_type_rule = '- type 只能是 "tool"。工具节点填写 tool 和 params。\n'
    if allow_subagents:
        descriptions = [f"- {name}: {sa.description()}" for name, sa in agent.subagents.snapshot().items()]
        planner_scope = "可用工具和子 Agent"
        node_type_rule = (
            '- type 只能是 "tool" 或 "sub_agent"；子 Agent 节点填写 '
            'agent 和 goal。\n'
        )
        agent_section = (
            "\n可用子 Agent：\n" + "\n".join(descriptions) +
            "\n需要研究、总结、报告或写文档时，优先组合使用内置角色；"
            "doc_agent 负责保存并写入 RAG。"
        )
    plan_prompt = (
        f"你是一个任务规划器。根据用户问题，从{planner_scope}中选出节点，并标注依赖关系。\n"
        "- 给每个工具调用分配唯一 id，如 n1、n2。\n"
        f"{node_type_rule}"
        "- 如果工具 B 需要工具 A 的输出，则 B 的 depends_on 包含 A 的 id。\n"
        "- 如果两个工具功能类似，可设置相同 race_group，系统会并行竞速。\n"
        f"用户问题：{query}\n"
        f"可用工具：\n{chr(10).join(tool_lines)}{agent_section}\n"
        '请只输出 JSON 数组：[{"id":"n1","tool":"工具名","params":{},'
        '"reason":"原因","depends_on":[],"optional_depends_on":[],"race_group":""}]。'
        "depends_on 默认是必需依赖；只有缺失后仍可安全继续的材料才放 optional_depends_on。无需节点则输出 []。"
    )
    planner_base = "你是一个精准的任务规划器，只在必要时才调用工具。"
    if mem_prefix:
        planner_base = mem_prefix + "\n\n" + planner_base
    try:
        chat = getattr(agent.llm, "chat_fast", None) or agent.llm.chat
        raw = chat([Message(role="user", content=plan_prompt)], system_prompt=planner_base)
        data = json.loads(_clean_json(raw))
    except Exception as e:
        logger.warning("⚠️  Planner LLM 图解析失败 (%s)，降级到规则规划。", e)
        return rule_plan_nodes(agent, query, tools_map, allow_subagents=allow_subagents)

    nodes: List[Node] = []
    if not isinstance(data, list):
        return nodes
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        node_type = str(item.get("type") or "tool")
        agent_name = str(item.get("agent") or "")
        tool = str(item.get("tool") or item.get("name") or "")
        if node_type in {"sub_agent", "subagent"} or agent_name:
            if not allow_subagents or agent.subagents.get(agent_name) is None:
                continue
            executor = agent_name
            graph_type = NodeType.SUBAGENT
        else:
            if tool not in tools_map or not _tool_matches_query(tool, tools_map[tool], query, llm_selected=True):
                continue
            executor = tool
            graph_type = NodeType.TOOL
        params = item.get("params")
        if params is None:
            params = item.get("parameters")
        if not isinstance(params, dict):
            params = {}
        if graph_type == NodeType.SUBAGENT and not params.get("goal"):
            params["goal"] = str(item.get("goal") or item.get("reason") or "")
        node_id = str(item.get("id") or f"n{idx + 1}")
        depends = item.get("depends_on") or []
        if not isinstance(depends, list):
            depends = []
        optional_depends = item.get("optional_depends_on") or []
        if not isinstance(optional_depends, list):
            optional_depends = []
        nodes.append(Node(
            id=node_id,
            type=graph_type,
            name=str(item.get("reason") or "LLM 规划调用"),
            tool_name=executor,
            params=dict(params),
            depends_on=[str(dep) for dep in depends],
            optional_depends_on=[str(dep) for dep in optional_depends],
            race_group=str(item.get("race_group") or ""),
        ))
    return nodes


def rule_plan_items(agent, query: str, tools_map: Dict[str, Any]) -> List[PlanItem]:
    """关键字规则降级规划（无真实 LLM 时使用）。"""
    q = query.lower()
    items: List[PlanItem] = []
    custom_matches = _matched_custom_tools(query, tools_map)

    if "get_time" in tools_map:
        if ("时间" in q) or ("几点" in q) or ("现在" in q):
            params: Dict[str, str] = {}
            if "东京" in q:
                params["timezone"] = "Asia/Tokyo"
            items.append(PlanItem(tool="get_time", params=params, reason="查询当前时间"))

    if "get_weather" in tools_map:
        if "天气" in q:
            city = "北京"
            for c in ["东京", "北京", "上海", "广州", "深圳", "纽约", "伦敦"]:
                if c in q:
                    city = c
                    break
            items.append(PlanItem(tool="get_weather", params={"city": city}, reason=f"查询{city}天气"))

    if "search_web" in tools_map:
        if not custom_matches and any(k in q for k in ["搜索", "查询", "介绍", "是什么", "怎么", "如何"]):
            items.append(PlanItem(tool="search_web", params={"query": query}, reason="搜索相关信息"))

    if "exec_command" in tools_map:
        if any(k in q for k in ["执行", "运行", "命令", "终端", "lscpu", "cpu", "磁盘", "内存", "系统信息"]):
            from .init_sandbox import extract_shell_command
            cmd = extract_shell_command(query)
            items.append(PlanItem(tool="exec_command", params={"command": cmd}, reason="执行终端命令"))

    if "rag_search" in tools_map:
        items.append(PlanItem(tool="rag_search", params={"query": query}, reason="检索个人知识库"))

    # MCP / 自定义工具：只有 matcher 命中或用户明确说出工具名才调用。
    # 旧逻辑会在每轮 Mock 对话里调用所有自定义工具，既危险也会制造假 Trace。
    builtins = {"get_time", "get_weather", "search_web", "rag_search", "exec_command"}
    for name, t in tools_map.items():
        if name in builtins or name not in custom_matches:
            continue
        params: Dict[str, str] = {}
        for p in getattr(t, "params", []) or []:
            if p.get("required") and p.get("name") not in params:
                params[p.get("name", "")] = query
                break
        items.append(PlanItem(tool=name, params=params, reason=f"调用工具 {name}"))

    return items


def rule_plan_nodes(
    agent, query: str, tools_map: Dict[str, Any], allow_subagents: bool = False,
) -> List[Node]:
    """关键字规则降级规划，返回图节点。"""
    q = query.lower()
    nodes: List[Node] = []
    if allow_subagents and needs_subagent_plan(q):
        nodes = subagent_pipeline_nodes(query, include_document=False)
        if wants_document_write(q):
            nodes.append(_document_pipeline_node())
        return nodes

    custom_matches = _matched_custom_tools(query, tools_map)
    if "search_web" in tools_map and not custom_matches and any(k in q for k in ["搜索", "查询", "介绍", "是什么", "怎么", "如何"]):
        nodes.append(Node(
            id="n1",
            type=NodeType.TOOL,
            name="搜索相关信息",
            tool_name="search_web",
            params={"query": query},
            depends_on=[],
            race_group="search",
        ))
    # 保留已知内置能力；其它 MCP/skill/领域工具必须显式匹配，不能全量调用。
    known_tools = {"search_web", "rag_search", "get_time", "get_weather", "exec_command"}
    for name, tool in tools_map.items():
        if name == "search_web":
            continue
        if name not in known_tools and name not in custom_matches:
            continue
        if name == "rag_search":
            matched = True
        elif name in {"get_time", "get_weather", "exec_command"}:
            matched = _tool_matches_query(name, tool, query, llm_selected=False)
        else:
            matched = name in custom_matches
        if not matched:
            continue
        params: Dict[str, str] = {}
        for p in getattr(tool, "params", []) or []:
            if p.get("required") and p.get("name") not in params:
                params[str(p.get("name") or "")] = query
                break
        nodes.append(Node(
            id=f"n{len(nodes) + 1}", type=NodeType.TOOL,
            name=f"调用工具 {name}", tool_name=name, params=params,
            depends_on=[], race_group="",
        ))
    return nodes


def needs_subagent_plan(query: str) -> bool:
    """Whether the query asks for a composed research deliverable."""

    value = (query or "").lower()
    # Organization/role names are not requests to conduct research. Otherwise
    # a factual question containing "研究院" incorrectly launches the report DAG.
    value = re.sub(r"研究(?:院|所|中心|机构|员|生)", "", value)
    return any(
        keyword in value
        for keyword in ("研究", "调研", "总结", "报告", "文档", "方案", "分析")
    )


# 可选的 LLM 意图复核：只兜关键词漏网的灰区消息，默认由调用方开关控制。
# 超时/异常/非法输出一律回退"非报告"，路由永远保留确定性 fallback。
_INTENT_LLM_TIMEOUT_SECONDS = 3.0
_INTENT_LLM_MIN_QUERY_CHARS = int(os.getenv("AGI_INTENT_LLM_MIN_CHARS", "12") or 12)


def _run_with_timeout(fn, timeout_seconds: float):
    """daemon 线程执行一次 fn，超时抛 TimeoutError（与 graph_runtime._call_with_timeout 同模式）。"""
    box: Dict[str, Any] = {}

    def runner():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - 异常原样转交主线程
            box["error"] = exc

    thread = threading.Thread(target=runner, name="intent-llm-refine", daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(f"intent refine exceeded {timeout_seconds}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def refine_intent_with_llm(agent, query: str) -> bool:
    """用快模型复核关键词未命中的消息是否需要报告类交付物。

    两级漏斗的第二级：`needs_subagent_plan` 关键词命中时调用方直接短路，
    不会走到这里；本函数只为"像报告但没命中关键词"的灰区消息兜底。
    任何失败（无 LLM / 超时 / 异常 / 非 JSON 输出 / 过短消息）都返回 False，
    调用方据此维持关键词结果（rag），不影响主链路可用性。
    """
    llm = getattr(agent, "llm", None)
    chat_fast = getattr(llm, "chat_fast", None) if llm is not None else None
    text = str(query or "").strip()
    if chat_fast is None or not text:
        return False
    if len(text) < _INTENT_LLM_MIN_QUERY_CHARS:
        return False

    prompt = (
        "判断下面这条用户消息是否需要产出研究/报告类交付物"
        "（需要多步检索、整理并写成文档的任务）。\n"
        '只输出 JSON：{"needs_report": true} 或 {"needs_report": false}，'
        "不要输出其他内容。\n\n"
        f"用户消息：{text}"
    )

    def _ask() -> str:
        return chat_fast(
            [Message(role="user", content=prompt)],
            system_prompt="你是意图分类器，只输出指定格式的 JSON。",
        )

    try:
        raw = str(_run_with_timeout(_ask, _INTENT_LLM_TIMEOUT_SECONDS) or "").strip()
    except Exception as exc:
        logger.debug("意图复核失败，按非报告处理: %s", exc)
        return False

    for prefix in ("```json", "```"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if isinstance(parsed, dict):
        return parsed.get("needs_report") is True
    return False


def wants_document_write(query: str) -> bool:
    value = (query or "").lower()
    return any(
        keyword in value
        for keyword in (
            "保存", "落库", "写入", "文档库", "知识库", "生成报告", "报告",
        )
    )


def subagent_pipeline_nodes(
    _query: str, *, include_document: bool = True,
) -> List[Node]:
    """Return the fixed Agentic-RAG DAG used by ``rag_agent`` mode."""

    nodes = [
        Node(
            id="n1",
            type=NodeType.SUBAGENT,
            name="子 Agent 研究与证据收集",
            tool_name="research_agent",
            params={"goal": "围绕用户任务在知识库中多轮检索并整理证据"},
            depends_on=[],
        ),
        Node(
            id="n2",
            type=NodeType.SUBAGENT,
            name="子 Agent 生成报告",
            tool_name="writer_agent",
            params={"goal": "基于研究结果生成 Markdown 报告"},
            depends_on=["n1"],
        ),
        Node(
            id="n3",
            type=NodeType.SUBAGENT,
            name="子 Agent 审查报告",
            tool_name="review_agent",
            params={"goal": "检查报告质量、风险和证据缺口"},
            depends_on=["n2"],
        ),
    ]
    if include_document:
        nodes.append(_document_pipeline_node())
    return nodes


def _document_pipeline_node() -> Node:
    return Node(
        id="n4",
        type=NodeType.SUBAGENT,
        name="子 Agent 保存文档",
        tool_name="doc_agent",
        params={"goal": "保存报告到本地文档库并写入 RAG"},
        depends_on=["n2", "n3"],
    )


def llm_replan(
    agent, query: str, reason: str, graph, tools_map: Dict[str, Any],
    mem_prefix: str, allow_subagents: bool, failed=None,
) -> List[Node]:
    """基于已执行图追加最多三个节点；无真实 LLM 时不重规划。"""
    if not agent.cfg.is_real_llm():
        return []
    snapshot = []
    for node in graph.nodes.values():
        snapshot.append({
            "id": node.id, "executor": node.tool_name, "goal": node.name,
            "status": str(node.status), "result": (node.result or "")[:200],
            "error": node.error,
        })
    failed_hint = ""
    if failed is not None:
        failed_hint = f"失败节点：id={failed.id} executor={failed.tool_name} error={failed.error}\n"
    tool_lines = [f"- {name}: {getattr(tool, 'description', '')}" for name, tool in tools_map.items()]
    agent_lines: List[str] = []
    if allow_subagents:
        agent_lines = [f"- {name}: {sa.description()}" for name, sa in agent.subagents.snapshot().items()]
    agent_section = ""
    if agent_lines:
        agent_section = f"可用子 Agent：\n{chr(10).join(agent_lines)}\n"
    prompt = (
        "你是一个任务重规划器。判断现有观察是否足够；足够就返回 []，否则追加最多 3 个节点。\n"
        f"用户问题：{query}\n触发原因：{reason}\n{failed_hint}"
        f"当前图状态：{json.dumps(snapshot, ensure_ascii=False)}\n"
        f"可用工具：\n{chr(10).join(tool_lines)}\n"
        f"{agent_section}"
        "depends_on 可引用已有节点；不要重复已有工作。严格输出 JSON 数组。"
    )
    system = "你只在现有观察不足时追加节点。"
    if mem_prefix:
        system = mem_prefix + "\n\n" + system
    chat = getattr(agent.llm, "chat_fast", None) or agent.llm.chat
    try:
        raw = chat([Message(role="user", content=prompt)], system_prompt=system)
        data = json.loads(_clean_json(raw))
    except Exception as exc:
        logger.warning("Replanner 解析失败: %s", exc)
        return []
    if not isinstance(data, list):
        return []

    existing = set(graph.nodes)
    result: List[Node] = []
    for index, item in enumerate(data[:3]):
        if not isinstance(item, dict):
            continue
        agent_name = str(item.get("agent") or "")
        tool_name = str(item.get("tool") or "")
        raw_type = str(item.get("type") or "tool")
        if raw_type in {"sub_agent", "subagent"} or agent_name:
            if not allow_subagents or agent.subagents.get(agent_name) is None:
                continue
            node_type, executor = NodeType.SUBAGENT, agent_name
        else:
            if tool_name not in tools_map or not _tool_matches_query(
                tool_name, tools_map[tool_name], query, llm_selected=True
            ):
                continue
            node_type, executor = NodeType.TOOL, tool_name
        node_id = str(item.get("id") or "")
        if not node_id or node_id in existing:
            node_id = f"r{len(result) + 1}-{index}"
        depends = item.get("depends_on") or []
        if not isinstance(depends, list):
            depends = []
        optional_depends = item.get("optional_depends_on") or []
        if not isinstance(optional_depends, list):
            optional_depends = []
        params = item.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if node_type == NodeType.SUBAGENT and not params.get("goal"):
            params["goal"] = str(item.get("goal") or item.get("reason") or "")
        result.append(Node(
            id=node_id, type=node_type, tool_name=executor,
            name=str(item.get("reason") or item.get("goal") or "重规划节点"),
            params=dict(params),
            depends_on=[str(dep) for dep in depends],
            optional_depends_on=[str(dep) for dep in optional_depends],
            race_group=str(item.get("race_group") or ""),
        ))
        existing.add(node_id)
    return result


def _clean_json(raw: str) -> str:
    raw = (raw or "").strip()
    if "<|FunctionCallBegin|>" in raw:
        raw = raw[raw.index("<|FunctionCallBegin|>") + len("<|FunctionCallBegin|>"):]
        if "<|FunctionCallEnd|>" in raw:
            raw = raw[: raw.index("<|FunctionCallEnd|>")]
    raw = re.sub(r"^```json", "", raw)
    raw = re.sub(r"^```", "", raw)
    raw = re.sub(r"```$", "", raw)
    return raw.strip()


def _matched_custom_tools(query: str, tools_map: Dict[str, Any]) -> set[str]:
    known = {"get_time", "get_weather", "search_web", "rag_search", "exec_command"}
    return {
        name for name, tool in tools_map.items()
        if name not in known and _tool_matches_query(name, tool, query, llm_selected=False)
    }


def _tool_matches_query(name: str, tool: Any, query: str, *, llm_selected: bool) -> bool:
    """Apply a deterministic safety gate after both rule and LLM planning.

    A tool-provided matcher is authoritative.  Tools without one may still be
    chosen by a real LLM, while the no-LLM path requires the user to explicitly
    mention the registered tool name.
    """
    matcher = getattr(tool, "matcher", None)
    if callable(matcher):
        try:
            return bool(matcher(query))
        except Exception:
            return False
    if llm_selected:
        return True
    normalized_name = str(name or "").strip().casefold()
    return bool(normalized_name and normalized_name in str(query or "").casefold())
