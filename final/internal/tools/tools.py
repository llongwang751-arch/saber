# tools — 工具定义、调用与注册（Python 版与 main 分支 Go 版 tools.go 对齐）
#
# 内置工具集合与 Go 845e8f7 的 toolimpl.DefaultTools 对齐：默认仅 search_web。
# get_time/get_weather 保留为可选工具函数，但不自动注册；RAG 与文档库是领域链路，
# 不伪装成普通内置工具。
#
# 此外提供以下能力：
#   - exec_command：通过 sandbox 在隔离环境执行终端命令
#   - tavily：调用 Tavily Search API（search_web 双层降级：tavily → LLM → mock）
#   - decide：基于关键字的简单工具选择器（对应 Go 版 tools.Decide）
import ipaddress
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

MCP_DEFAULT_TIMEOUT_SECONDS = 30
# 本地开发/内网部署时可显式放开对私网 MCP 端点的限制。
ALLOW_PRIVATE_MCP_ENV = "AGI_ALLOW_PRIVATE_MCP_ENDPOINTS"


def _reject_private_ip(ip: ipaddress._BaseAddress, endpoint: str) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(
            f"为防止 SSRF，MCP endpoint 不允许指向内网地址: {endpoint!r}"
            f"（确需内网端点可设置 {ALLOW_PRIVATE_MCP_ENV}=1）"
        )


def validate_mcp_endpoint(endpoint: str) -> None:
    """SSRF 防护：MCP 端点只允许 http(s)，且默认拒绝指向内网/回环/链路本地地址。

    只校验 IP 字面量。域名不做注册期解析：fake-ip/TUN 代理等环境会把所有域名
    解析到保留网段，注册期解析无法区分代理与真实内网目标；
    域名解析后的指向由部署层出口网络策略负责。
    """
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"MCP endpoint 必须是 http/https URL: {endpoint!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("MCP endpoint 缺少主机名")
    if os.getenv(ALLOW_PRIVATE_MCP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    _reject_private_ip(ip, endpoint)


# ─────────────────────────────── 数据结构 ────────────────────────────────────

@dataclass
class Tool:
    name: str
    description: str
    params: List[Dict[str, str]]
    func: Callable[[Dict[str, Any]], str]
    is_mcp: bool = False
    # Optional deterministic router used by the no-LLM planner.  Tools without
    # a matcher are never called merely because they were registered.
    matcher: Optional[Callable[[str], bool]] = None
    # Go Tool.ExecuteCtx / ExecuteStructured 的 Python 对应物。参数顺序追加在
    # 末尾，保持现有 Tool(name, description, params, func, ...) 调用兼容。
    execute_ctx: Optional[Callable[["ToolCallContext", Dict[str, Any]], str]] = None
    execute_structured: Optional[
        Callable[["ToolCallContext", Dict[str, Any]], "ToolResult"]
    ] = None
    # Unclassified external tools are conservatively treated as writes.
    side_effecting: bool = False
    idempotent: bool = False
    requires_approval: bool = False


class ToolError(Exception):
    """可供调度器可靠判定重试语义的结构化工具错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        cause: Optional[BaseException] = None,
    ):
        self.code = str(code or "internal")
        self.message = str(message or "工具执行失败")
        self.retryable = bool(retryable)
        self.cause = cause
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.code else self.message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass
class ToolResult:
    """与 Go ToolResult 对齐的结构化调用结果；duration 单位为秒。"""

    success: bool
    payload: str = ""
    payload_json: Optional[Dict[str, Any]] = None
    error: Optional[ToolError] = None
    duration: float = 0.0
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": self.success,
            "payload": self.payload,
            "duration": self.duration,
            "metadata": dict(self.metadata),
        }
        if self.payload_json is not None:
            result["payload_json"] = self.payload_json
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result


class ToolCallContext:
    """协作式取消/截止时间上下文。

    Python 线程不能强杀已经进入第三方阻塞函数的调用。此对象让支持上下文的
    工具在调用前后检查取消，并把剩余截止时间传给网络客户端；GraphRuntime
    仍可及时停止等待不支持上下文的旧工具，但其后台调用可能继续到自身返回。
    """

    def __init__(self, token: Any = None, timeout_seconds: float = 0.0):
        self.token = token
        timeout = max(0.0, float(timeout_seconds or 0.0))
        self._timeout_seconds = timeout
        self.deadline = time.monotonic() + timeout if timeout > 0 else None

    def is_cancelled(self) -> bool:
        return bool(
            self.token is not None
            and callable(getattr(self.token, "is_cancelled", None))
            and self.token.is_cancelled()
        )

    def is_timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def remaining_seconds(self, default: float = MCP_DEFAULT_TIMEOUT_SECONDS) -> float:
        if self.deadline is None:
            return float(default)
        # Subtracting a large monotonic timestamp may round above the original
        # timeout; never pass a larger budget to the downstream HTTP client.
        return min(self._timeout_seconds, max(0.001, self.deadline - time.monotonic()))

    def failure(self) -> Optional[ToolError]:
        if self.is_cancelled():
            return ToolError("cancelled", "工具调用被用户中断", retryable=False)
        if self.is_timed_out():
            return ToolError("timeout", "工具调用超过截止时间", retryable=True)
        return None


def classify_tool_exception(
    exc: BaseException, *, default_retryable: bool = False
) -> ToolError:
    """把 Python/requests 异常转换成稳定的 Go 风格重试分类。"""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 500:
            return ToolError("http_5xx", f"HTTP 返回 {status}", retryable=True, cause=exc)
        if status >= 400:
            return ToolError("http_4xx", f"HTTP 返回 {status}", retryable=False, cause=exc)
    invalid_request_types = tuple(
        error_type
        for error_type in (
            getattr(requests, "InvalidURL", None),
            getattr(requests, "MissingSchema", None),
            getattr(requests, "InvalidSchema", None),
            getattr(requests.exceptions, "InvalidJSONError", None),
        )
        if isinstance(error_type, type)
    )
    if invalid_request_types and isinstance(exc, invalid_request_types):
        return ToolError("param", str(exc) or "MCP 请求参数错误", retryable=False, cause=exc)
    if isinstance(exc, (TimeoutError, requests.Timeout)):
        return ToolError("timeout", str(exc) or "工具调用超时", retryable=True, cause=exc)
    if isinstance(exc, requests.ConnectionError):
        return ToolError("network", str(exc) or "网络连接失败", retryable=True, cause=exc)
    if isinstance(exc, requests.RequestException):
        return ToolError("network", str(exc) or "网络请求失败", retryable=True, cause=exc)
    if isinstance(exc, (TypeError, ValueError)):
        return ToolError("param", str(exc) or "工具参数错误", retryable=False, cause=exc)
    if isinstance(exc, (InterruptedError, KeyboardInterrupt)):
        return ToolError("cancelled", str(exc) or "工具调用被中断", retryable=False, cause=exc)
    return ToolError(
        "internal", str(exc) or exc.__class__.__name__,
        retryable=default_retryable, cause=exc,
    )


@dataclass
class CallResult:
    success: bool
    content: str
    error: Optional[str] = None
    # 兼容 Go 版 CallResult 字段（部分调用方期望）
    tool_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    structured_result: Optional[ToolResult] = None


# ─────────────────────────────── 内置工具 ────────────────────────────────────

def get_time(args: Dict[str, Any]) -> str:
    """返回当前时间，支持可选时区参数（与 Go 版 GetTime 对齐）。"""
    tz = args.get("timezone") if isinstance(args, dict) else None
    if tz:
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def get_weather(args: Dict[str, Any]) -> str:
    """模拟天气查询（与 Go 版 GetWeather 对齐，含小型词表）。"""
    db = {
        "北京": "晴天 22°C",
        "东京": "多云 18°C 湿度65%",
        "上海": "小雨 20°C",
        "纽约": "晴天 15°C",
        "伦敦": "阴天 12°C",
        "广州": "晴天 28°C",
        "深圳": "晴天 26°C",
    }
    city = (args.get("city") or "北京").strip() if isinstance(args, dict) else "北京"
    if city in db:
        return f"{city}：{db[city]}"
    return f"{city}：晴天 20°C（模拟）"


def _mock_search(query: str) -> str:
    """search 工具的 mock 兜底实现（与 Go 版 SearchWeb 对齐）。"""
    db = {
        "AI应用工程师": "AI 应用工程师是将 AI 技术落地到业务的工程师，需具备 ML 基础、API 开发、Prompt 工程等能力。",
        "Go语言": "Go 是 Google 开发的开源编程语言，适用于高并发服务端应用。Docker 即用 Go 开发。",
    }
    for k, v in db.items():
        if k in query:
            return v
    return f"关于「{query}」的搜索结果（模拟）"


def search_web_factory(cfg=None, llm=None) -> Callable[[Dict[str, Any]], str]:
    """构造 search_web 工具的执行函数：

      1) 已配置 search_api_key → 调 Tavily 真实搜索
      2) Tavily 失败但 llm 可用 → 用 LLM 知识库回答
      3) 否则 → mock 搜索结果
    """
    from .tavily import tavily_search  # 延迟导入避免循环

    def _execute(args: Dict[str, Any]) -> str:
        query = args.get("query", "") if isinstance(args, dict) else ""
        if not query:
            return "请提供搜索关键词"

        api_key = getattr(cfg, "search_api_key", "") if cfg is not None else ""
        api_url = getattr(cfg, "search_api_url", "") if cfg is not None else ""

        # 1. Tavily 真实搜索
        if api_key:
            try:
                return tavily_search(query, api_key, api_url)
            except Exception as e:
                logger.warning("Tavily 搜索失败，降级: %s", e)

        # 2. LLM 知识库降级
        if llm is not None:
            try:
                from internal.llm.llm import Message  # 延迟导入

                resp = llm.chat(
                    [Message(role="user", content=f"请用简洁中文回答：{query}")],
                    system_prompt="你是搜索助手，基于已知知识简明回答用户问题。",
                )
                if resp:
                    return resp
            except Exception as e:
                logger.warning("LLM 降级搜索失败: %s", e)

        # 3. mock 兜底
        return _mock_search(query)

    return _execute


def search_web(args: Dict[str, Any]) -> str:
    """无依赖的 search_web 默认实现（仅 mock）。"""
    return _mock_search(args.get("query", "") if isinstance(args, dict) else "")


# ─────────────────────────────── tavily / exec_command 工具 ──────────────────

def build_tavily_tool(cfg=None) -> Tool:
    """单独以 'tavily' 名称暴露的工具：仅调用 Tavily（未配置时降级 mock）。"""
    from .tavily import tavily_search  # 延迟导入

    def _execute(args: Dict[str, Any]) -> str:
        query = args.get("query", "") if isinstance(args, dict) else ""
        if not query:
            return "请提供搜索关键词"
        api_key = getattr(cfg, "search_api_key", "") if cfg is not None else ""
        api_url = getattr(cfg, "search_api_url", "") if cfg is not None else ""
        if not api_key:
            return _mock_search(query)
        try:
            return tavily_search(query, api_key, api_url)
        except Exception as e:
            logger.warning("Tavily 调用失败: %s", e)
            return _mock_search(query)

    return Tool(
        name="tavily",
        description="使用 Tavily Search API 进行真实互联网搜索（需要配置 search_api_key）。",
        params=[{"name": "query", "type": "string", "description": "搜索关键词"}],
        func=_execute,
    )


def build_exec_command_tool(sandbox) -> Optional[Tool]:
    """构造 exec_command 工具（要求传入 sandbox 实例；为 None 时返回 None）。"""
    if sandbox is None:
        return None
    from .exec_command import exec_command_tool_factory  # 延迟导入

    return Tool(
        name="exec_command",
        description=(
            "在隔离沙箱中执行终端命令。支持 ls/cat/echo/python3/node 等常见操作；"
            "危险命令（rm -rf、sudo、网络外联等）会被自动拒绝；"
            "涉及删除/安装/管道等中等风险命令需通过 confirm=true 二次确认。"
        ),
        params=[
            {"name": "command", "type": "string", "description": "要执行的 Shell 命令（单条，禁止命令链）"},
            {"name": "confirm", "type": "boolean", "description": "对 warn 级命令的二次确认；默认 false"},
        ],
        func=exec_command_tool_factory(sandbox),
        side_effecting=True,
        requires_approval=True,
    )


# ─────────────────────────────── 默认工具集 ──────────────────────────────────

def default_tools(cfg=None, llm=None, sandbox=None) -> List[Tool]:
    """返回默认工具集合（与 Go 版 toolimpl.DefaultTools 对齐）。

    Args:
        cfg:     APIConfig 实例。提供后 search_web 将启用 Tavily / LLM 双层降级。
        llm:     LLM 客户端（提供 chat 方法）。用于 search_web 的 LLM 降级。
        sandbox: sandbox.Sandbox 实例。提供后会自动注册 exec_command 工具。

    与 Go 当前版严格对齐：默认只暴露 ``search_web``。时间、天气、RAG 和
    文档库都不是普通内置工具；``exec_command`` 仅在沙箱成功初始化后注册。
    """
    search_func = search_web_factory(cfg=cfg, llm=llm) if (cfg is not None or llm is not None) else search_web

    tools: List[Tool] = [
        Tool(
            name="search_web",
            description="执行网络搜索（Tavily → LLM 知识库 → mock 三层降级）",
            params=[{"name": "query", "type": "string", "description": "搜索关键词"}],
            func=search_func,
        ),
    ]

    # sandbox 可用时注册 exec_command
    if sandbox is not None:
        tool = build_exec_command_tool(sandbox)
        if tool is not None:
            tools.append(tool)

    return tools


# ─────────────────────────────── 工具调用器 ──────────────────────────────────

class ToolExecutor:
    """与 main 分支 Go 版接口对齐的工具执行器。

    内部用 ``threading.RLock`` 串行化 ``_tool_map`` 的读写。多路并发调用方：
      - register / add_tool（写）
      - call / get_tool_descriptions / snapshot / filter_tools / 路由侧
        ``self.tool_executor._tool_map`` 直读（读）

    与 main toolRegistry 对齐：写持锁；读经 ``snapshot``/``filter_tools``
    返回浅拷贝供调用方无锁遍历。``call`` 内部以一次 snapshot 查询 + 锁外
    执行 ``tool.func`` 的方式，避免长时占锁。
    """

    def __init__(self, tools: Optional[List[Tool]] = None):
        self.tools = tools if tools else default_tools()
        self._lock = threading.RLock()
        self._tool_map = {t.name: t for t in self.tools}

    def call(self, tool_name: str, args: Dict[str, Any]) -> CallResult:
        with self._lock:
            tool = self._tool_map.get(tool_name)
        if tool is None:
            return CallResult(
                success=False, content="", error=f"工具 {tool_name} 不存在",
                tool_name=tool_name, params=args or {},
            )
        try:
            if callable(tool.execute_structured):
                structured = tool.execute_structured(
                    ToolCallContext(timeout_seconds=MCP_DEFAULT_TIMEOUT_SECONDS),
                    args or {},
                )
                if not structured.success:
                    return CallResult(
                        success=False,
                        content=structured.payload,
                        error=str(structured.error or "工具执行失败"),
                        tool_name=tool_name,
                        params=args or {},
                        structured_result=structured,
                    )
                return CallResult(
                    success=True,
                    content=structured.payload,
                    tool_name=tool_name,
                    params=args or {},
                    structured_result=structured,
                )
            result = tool.func(args or {})
            return CallResult(success=True, content=str(result), tool_name=tool_name, params=args or {})
        except Exception as e:
            logger.error("工具调用失败: %s", e)
            return CallResult(
                success=False, content="", error=str(e),
                tool_name=tool_name, params=args or {},
            )

    def get_tool_descriptions(self) -> List[Dict[str, Any]]:
        with self._lock:
            tools = list(self.tools)
        out: List[Dict[str, Any]] = []
        for tool in tools:
            out.append({
                "name": tool.name,
                "description": tool.description,
                "parameters": [
                    {
                        "name": p["name"],
                        "type": p["type"],
                        "description": p.get("description", ""),
                    }
                    for p in tool.params
                ],
                "is_mcp": tool.is_mcp,
            })
        return out

    def add_tool(self, tool: Tool) -> None:
        with self._lock:
            existed = tool.name in self._tool_map
            self._tool_map[tool.name] = tool
            if existed:
                # 覆盖既有同名工具：保持 self.tools 唯一性
                self.tools = [t for t in self.tools if t.name != tool.name]
            self.tools.append(tool)

    def remove_tool(self, name: str) -> bool:
        """Remove a dynamically registered tool without mutating built-ins."""
        with self._lock:
            if name not in self._tool_map:
                return False
            self._tool_map.pop(name, None)
            self.tools = [tool for tool in self.tools if tool.name != name]
            return True

    def snapshot(self) -> Dict[str, Tool]:
        """返回 _tool_map 的浅拷贝，供调用方无锁遍历（对应 main snapshot）。"""
        with self._lock:
            return dict(self._tool_map)

    def filter_tools(self, names: List[str]) -> Dict[str, Tool]:
        """按名单返回 _tool_map 的子集（对应 main filter）。"""
        with self._lock:
            return {n: self._tool_map[n] for n in names if n in self._tool_map}


# ─────────────────────────────── 工具选择器 ──────────────────────────────────

def decide(query: str, ts: Dict[str, Tool]) -> Optional[CallResult]:
    """基于关键字推断应调用的工具及参数（对应 Go 版 tools.Decide）。

    只会返回 ts 中实际存在的工具；若无任何工具命中，则取首个工具兜底。
    """
    if not ts:
        return None
    q = query.lower()

    if ("几点" in q) or ("时间" in q):
        if "get_time" in ts:
            params: Dict[str, Any] = {}
            if "东京" in q:
                params["timezone"] = "Asia/Tokyo"
            return CallResult(success=True, content="", tool_name="get_time", params=params)

    if "天气" in q:
        if "get_weather" in ts:
            city = "北京"
            for c in ["东京", "北京", "上海", "纽约", "伦敦", "广州", "深圳"]:
                if c in q:
                    city = c
                    break
            return CallResult(success=True, content="", tool_name="get_weather", params={"city": city})

    if ("查" in q) or ("搜索" in q) or ("是什么" in q):
        if "search_web" in ts:
            return CallResult(success=True, content="", tool_name="search_web", params={"query": query})

    if "exec_command" in ts:
        return CallResult(success=True, content="", tool_name="exec_command", params={"command": query})

    # 兜底：取集合中第一个工具，使用首个必填参数名（缺省 'query'）
    for name, t in ts.items():
        param_name = "query"
        for p in t.params:
            param_name = p.get("name", "query")
            break
        return CallResult(success=True, content="", tool_name=name, params={param_name: query})

    return None


# ─────────────────────────────── MCP 工具 ────────────────────────────────────

def new_mcp_tool(
    name: str,
    description: str,
    params: List[Dict[str, str]],
    func: Optional[Callable[[Dict[str, Any]], str]] = None,
    endpoint: str = "",
) -> Tool:
    """创建带 Go 等价结构化结果和错误分类的 MCP 工具。

    ``func`` 仍作为字符串版兼容入口；``execute_structured`` 供 GraphRuntime
    优先调用。HTTP 4xx/参数/取消不可重试，5xx/网络/超时可重试。
    """
    structured: Callable[[ToolCallContext, Dict[str, Any]], ToolResult]

    if endpoint:
        validate_mcp_endpoint(endpoint)

    if func is None and endpoint:
        def _http_structured(ctx: ToolCallContext, p: Dict[str, Any]) -> ToolResult:
            started = time.perf_counter()
            metadata = {"backend": "mcp", "endpoint": endpoint}
            early_failure = ctx.failure()
            if early_failure is not None:
                return ToolResult(
                    success=False, error=early_failure,
                    duration=time.perf_counter() - started, metadata=metadata,
                )

            try:
                # requests 会自行编码 JSON；这里先验证一次，使序列化失败稳定归类为 param。
                json.dumps(p)
            except Exception as exc:
                error = ToolError(
                    "param", f"序列化参数失败: {exc}", retryable=False, cause=exc
                )
                return ToolResult(
                    success=False, error=error,
                    duration=time.perf_counter() - started, metadata=metadata,
                )

            try:
                timeout = ctx.remaining_seconds(MCP_DEFAULT_TIMEOUT_SECONDS)
                # 没有显式 deadline 时保留整数 30，兼容旧调用及 requests 合同。
                request_timeout = (
                    MCP_DEFAULT_TIMEOUT_SECONDS if ctx.deadline is None else timeout
                )
                response = requests.post(endpoint, json=p, timeout=request_timeout)
            except requests.Timeout as exc:
                context_failure = ctx.failure()
                error = context_failure or ToolError(
                    "timeout", f"MCP 请求超时 [{endpoint}]", retryable=True, cause=exc
                )
                return ToolResult(
                    success=False, error=error,
                    duration=time.perf_counter() - started, metadata=metadata,
                )
            except requests.RequestException as exc:
                context_failure = ctx.failure()
                error = context_failure or classify_tool_exception(
                    exc, default_retryable=True
                )
                return ToolResult(
                    success=False, error=error,
                    duration=time.perf_counter() - started, metadata=metadata,
                )
            except Exception as exc:
                error = ctx.failure() or classify_tool_exception(
                    exc, default_retryable=False
                )
                return ToolResult(
                    success=False, error=error,
                    duration=time.perf_counter() - started, metadata=metadata,
                )

            metadata["status_code"] = str(response.status_code)
            try:
                payload = str(getattr(response, "text", ""))
            except Exception as exc:
                error = ToolError(
                    "network", f"读取 MCP 响应失败: {exc}",
                    retryable=True, cause=exc,
                )
                return ToolResult(
                    success=False, error=error,
                    duration=time.perf_counter() - started, metadata=metadata,
                )
            context_failure = ctx.failure()
            if context_failure is not None:
                return ToolResult(
                    success=False, payload=payload, error=context_failure,
                    duration=time.perf_counter() - started, metadata=metadata,
                )

            if response.status_code >= 400:
                is_server_error = response.status_code >= 500
                error = ToolError(
                    "http_5xx" if is_server_error else "http_4xx",
                    f"MCP 返回 {response.status_code}",
                    retryable=is_server_error,
                )
                return ToolResult(
                    success=False, payload=payload, error=error,
                    duration=time.perf_counter() - started, metadata=metadata,
                )

            payload_json = None
            try:
                candidate = json.loads(payload)
                if isinstance(candidate, dict):
                    payload_json = candidate
            except (TypeError, json.JSONDecodeError):
                pass
            return ToolResult(
                success=True, payload=payload, payload_json=payload_json,
                duration=time.perf_counter() - started, metadata=metadata,
            )

        structured = _http_structured

        def _http_call(p: Dict[str, Any]) -> str:
            result = structured(ToolCallContext(), p)
            if not result.success:
                raise result.error or ToolError("internal", "MCP 请求失败")
            return result.payload

        func = _http_call
    else:
        if func is None:
            def _noop(_p: Dict[str, Any]) -> str:
                return f"[MCP] {name} 未配置 func 或 endpoint"

            func = _noop

        compatible_func = func

        def _custom_structured(ctx: ToolCallContext, p: Dict[str, Any]) -> ToolResult:
            started = time.perf_counter()
            metadata = {"backend": "mcp"}
            if endpoint:
                metadata["endpoint"] = endpoint
            early_failure = ctx.failure()
            if early_failure is not None:
                return ToolResult(
                    success=False, error=early_failure,
                    duration=time.perf_counter() - started, metadata=metadata,
                )
            try:
                payload = str(compatible_func(p))
            except Exception as exc:
                error = classify_tool_exception(exc, default_retryable=False)
                return ToolResult(
                    success=False, error=error,
                    duration=time.perf_counter() - started, metadata=metadata,
                )
            context_failure = ctx.failure()
            if context_failure is not None:
                return ToolResult(
                    success=False, payload=payload, error=context_failure,
                    duration=time.perf_counter() - started, metadata=metadata,
                )
            payload_json = None
            try:
                candidate = json.loads(payload)
                if isinstance(candidate, dict):
                    payload_json = candidate
            except (TypeError, json.JSONDecodeError):
                pass
            return ToolResult(
                success=True, payload=payload, payload_json=payload_json,
                duration=time.perf_counter() - started, metadata=metadata,
            )

        structured = _custom_structured

    def _execute_ctx(ctx: ToolCallContext, p: Dict[str, Any]) -> str:
        result = structured(ctx, p)
        if not result.success:
            raise result.error or ToolError("internal", "MCP 请求失败")
        return result.payload

    return Tool(
        name=name,
        description=description,
        params=params,
        func=func,
        is_mcp=True,
        side_effecting=True,
        requires_approval=True,
        execute_ctx=_execute_ctx,
        execute_structured=structured,
    )
