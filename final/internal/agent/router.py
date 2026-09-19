# router — UnifiedAgent 的模式路由判断
# router — UnifiedAgent 的模式路由判断
#
# 对应 Go 版 internal/agent/router.go。把基于关键词的启发式判断从 agent.py
# 抽出，让主流程更聚焦。
from typing import Dict, Any


# 关键字触发表（精准对齐核心工具定义）
_TOOL_TRIGGERS = [
    ("天气", "get_weather"),
    ("温度", "get_weather"),
    ("气温", "get_weather"),
    ("下雨", "get_weather"),
    ("暴雨", "get_weather"),
    ("雨", "get_weather"),
    ("降水", "get_weather"),
    ("降雨", "get_weather"),
    ("刮大风", "get_weather"),
    ("晴天", "get_weather"),
    ("阴天", "get_weather"),
    ("空气质量", "get_weather"),
    ("aqi", "get_weather"),
    ("几点", "get_time"),
    ("当地时间", "get_time"),
    ("标准时间", "get_time"),
    ("电子钟时间", "get_time"),
    ("时刻", "get_time"),
    ("现在时间", "get_time"),
    ("当前时间", "get_time"),
    ("几分", "get_time"),
    ("时间", "get_time"),
    ("谁获得", "search_web"),
    ("搜", "search_web"),
    ("检索", "search_web"),
    ("全网", "search_web"),
    ("查一下", "search_web"),
    ("查找", "search_web"),
    ("查询", "search_web"),
    ("是什么", "search_web"),
    ("谁是", "search_web"),
    ("知识", "rag_search"),
    ("文档", "rag_search"),
]


def need_tool(query: str) -> bool:
    """判断 query 是否触发单一工具（时间 / 天气 / 搜索 / 查询）。"""
    q = query.lower()
    return (
        ("几点" in q) or ("时间" in q) or ("天气" in q)
        or ("查" in q) or ("搜索" in q) or ("是什么" in q)
        or ("温度" in q) or ("搜" in q) or ("谁" in q)
    )


def need_rag(query: str, rag_loaded: bool) -> bool:
    """知识库已加载且本次不走工具/ReAct 时启用 RAG。"""
    return rag_loaded and not need_tool(query) and not need_react(query)


def need_react(query: str) -> bool:
    """当 query 涉及 2+ 个子需求时触发多步推理。"""
    q = query.lower()
    count = 0
    if ("时间" in q) or ("几点" in q):
        count += 1
    if "天气" in q or "气温" in q or "温度" in q:
        count += 1
    if ("总结" in q) or ("汇总" in q):
        count += 1
    if ("查" in q) or ("搜索" in q) or ("搜" in q):
        count += 1
    return count >= 2


def need_react_from_tools(query: str, tools_map: Dict[str, object]) -> bool:
    """显式指定工具集时直接走 ReAct 路径。"""
    return len(tools_map) > 0


def detect_tool(query: str, tools_map: Any = None):
    """按关键字命中检测应调用的工具名（支持兼容 dict/list/registry）。"""
    q = query.lower()
    for trigger, tool_name in _TOOL_TRIGGERS:
        if trigger in q:
            if tools_map is None:
                return tool_name
            if isinstance(tools_map, dict) and tool_name in tools_map:
                return tool_name
            if isinstance(tools_map, (list, set, tuple)):
                names = [getattr(t, "name", str(t)) for t in tools_map]
                if tool_name in names:
                    return tool_name
            # 当传入基础工具列表时，依然识别对应意图的目标工具
            return tool_name
    return None
