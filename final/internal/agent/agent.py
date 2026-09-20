# agent — Python 版统一智能体（重写后版本）
#
# 主分支 Go 版 internal/agent/agent.go 拆出的诸多职责被分散到同目录下的：
#   - router.py          — chat / tool / react / rag 模式路由
#   - planner.py         — ReAct 模式下的 Planner LLM
#   - restore.py         — 启动期从 PG 恢复偏好/长期记忆/聊天记录 + KG 初始化
#   - cancel.py          — 取消令牌注册表 + go_safe
#   - init_sandbox.py    — 沙箱 + exec_command 工具初始化
#   - memory_writer.py   — 异步记忆写入 + 回复事实抽取
#   - status.py          — 系统状态视图聚合
#
# 本文件只负责：构造 + 路由分派 + 图调度入口（react 模式走 GraphRuntime）。
import json
import inspect
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from config.config import APIConfig
from internal.document.library import DOCUMENT_SOURCE_AGENT, WriteRequest
from internal.infra.infra import Infrastructure
from internal.llm.llm import Client as LLMClient, Message
from internal.memory.memory import LongTerm, Preference, ShortTerm
from internal.memory.mem_stack import ConsolidationConfig, MemoryStack
from internal.promptctx import (
    ConstraintsSource,
    ContextAssembler,
    PlannerSnapshot,
    PlannerSource,
    Policy,
    ProfileSource,
    Query,
    RecallSource,
    SourceRegistry,
    StepObservation,
    TaskMemBuffer,
    TaskMemSource,
    ToolCallTrace,
    ToolStateSource,
    ToolStateTracker,
    default_schemas,
)
from internal.promptctx.compactor import ContextCompactor
from internal.observability import redact_text, sanitize_trace
from internal.rag.rag import Engine as RAGEngine
from internal.rag.reranker import LLMReranker
from internal.rag.local_reranker import LocalCrossEncoderReranker, LocalOverlapReranker
from internal.rag.rewriter import HistoryMessage, LLMRewriter
from internal.tools.tools import Tool, ToolExecutor, default_tools, new_mcp_tool

from .cancel import CancelRegistry, go_safe
from .artifact import prepare_artifact_workspace, produce_artifact
from .graph_runtime import GraphConfig, GraphRuntime
from .init_sandbox import init_sandbox
from .memory_writer import (
    AsyncMemoryWriter,
    async_update_memory,
    extract_memory_from_reply,
    maybe_consolidate_memory,
)
from .restore import init_knowledge_graph, restore_from_db, restore_rag_from_db
from .planner import (
    llm_plan_graph,
    needs_subagent_plan,
    refine_intent_with_llm,
    subagent_pipeline_nodes,
)
from .status import infra_status, status as build_status
from .subagents import register_builtin_subagents

logger = logging.getLogger(__name__)


class StepType:
    THOUGHT = "Thought"
    ACTION = "Action"
    OBSERVATION = "Observation"
    FINAL_ANSWER = "Final Answer"


@dataclass
class ReActStep:
    type: str
    content: str
    tool: str = ""
    params: Optional[Dict[str, str]] = None


@dataclass
class ChatOptions:
    use_rag: bool = False
    conversation_id: str = ""


@dataclass(frozen=True)
class RequestExecutionContext:
    """Server-created per-request context; it is not part of the chat wire API.

    Online experiments may only inject a pre-compiled allowlist here.  Never
    mutate ``UnifiedAgent.cfg``: one Agent instance can serve concurrent
    requests and a shared mutation would mix variants.
    """

    runtime_overrides: Dict[str, Any] = field(default_factory=dict)
    experiment_exposure_id: str = ""
    runtime_strategy_checksum: str = ""
    trace_id: str = ""


@dataclass
class Response:
    query: str
    answer: str = ""
    mode: str = "chat"
    # ``mode`` describes the execution path (react/rag); these fields carry
    # the business semantics extracted by deterministic domain tools.
    intent: Optional[str] = None
    slots: Dict[str, Any] = field(default_factory=dict)
    fallback: bool = False
    error: Optional[str] = None
    steps: List[ReActStep] = field(default_factory=list)
    tool_call: Optional[Dict[str, Any]] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    search_results: List[dict] = field(default_factory=list)
    rag_trace: Dict[str, Any] = field(default_factory=dict)
    task: Optional[dict] = None
    extracted_info: str = ""
    short_term_count: int = 0
    long_term_count: int = 0
    preferences: Dict[str, str] = field(default_factory=dict)
    interrupted: bool = False
    trace_id: str = ""
    # The public HTTP response only exposes the opaque exposure id.  The
    # strategy checksum is retained in the tenant-scoped trace for audit.
    experiment_exposure_id: str = ""
    runtime_strategy_checksum: str = ""


class UnifiedAgent:
    """统一智能体入口。负责装配各子模块、路由分派与 ReAct 推理循环。"""

    def __init__(self, cfg: APIConfig, inf: Infrastructure, user_id: str = "default_user"):
        self.cfg = cfg
        self.inf = inf
        self.user_id = str(user_id or "default_user")
        self.conversation_id = ""
        self._turn_lock = threading.Lock()
        from .conversations import ConversationPool
        self._conversations = ConversationPool(self)
        self.llm = LLMClient(cfg)
        self.stm = ShortTerm(cfg.short_term_max_turns)
        self.ltm = LongTerm(cfg, inf, self.user_id)
        self.preference = Preference(self.user_id, inf)
        # 三层记忆 + 偏好聚合容器（与 main memoryStack 对齐）。
        # graph_memory 由 init_knowledge_graph 在末尾通过 attach_graph 注入。
        self.mem = MemoryStack(stm=self.stm, ltm=self.ltm, preference=self.preference)
        # 用 ConsolidationConfig（dataclass + memory_consolidation_* 别名）替换裸 cfg
        # 喂给 LongTerm；保留对 APIConfig 的引用便于后续访问其它字段。
        try:
            self.ltm.set_consolidation_config(ConsolidationConfig.from_api_config(cfg))
        except Exception as e:
            logger.warning("⚠️  ConsolidationConfig 装配失败: %s", e)
        # 暴露 chat_history 仓储，供 restore.py 与 _save_chat_history 复用。
        self.chat_repo = getattr(getattr(inf, "repo", None), "chat_history", None)

        # RAG 引擎构造失败不致命：降级为禁用知识库
        try:
            self.rag = RAGEngine(cfg, inf, self.llm, user_id=self.user_id)
        except Exception as e:
            logger.warning("⚠️  RAG 引擎初始化失败: %s（已禁用知识库）", e)
            self.rag = None

        # 默认工具集；planner / sandbox 可后续追加
        self.tool_executor = ToolExecutor(default_tools(cfg=cfg, llm=self.llm))
        self.subagents = register_builtin_subagents(self)

        # 注册依赖 agent 上下文的内置工具（rag_search 闭包，与 Go 版
        # registerBuiltinTools 对齐）。search_web 已在 default_tools 中
        # 通过 search_web_factory 处理 Tavily / LLM 降级，无需重复注册。
        self._register_builtin_tools()

        # 取消令牌注册表 + 兼容旧接口的 process-level cancel event
        self._cancel_registry = CancelRegistry()
        self._memory_lock = threading.Lock()
        from internal.harness.approval import HumanInTheLoopPlugin
        from internal.harness.guardrails import SecurityGuardrailPlugin
        self.human_approval = HumanInTheLoopPlugin(journal=getattr(inf.repo, "action_journal", None))
        self.execution_plugins = [SecurityGuardrailPlugin(), self.human_approval]

        # 异步记忆写入器（单 worker 线程串行化）
        self.memory_writer = AsyncMemoryWriter()

        # 接通 LLM embed / RAG generate
        try:
            self.ltm.set_embed_fn(self.llm.embed)
        except Exception as e:
            logger.warning("⚠️  LTM embed 函数注入失败: %s", e)
        if self.rag is not None:
            try:
                self.rag.set_generate_fn(self._llm_generate)
                if getattr(cfg, "rag_rewrite_enabled", False):
                    self.rag.set_rewriter(LLMRewriter(self._llm_generate_fast, cfg.rag_rewrite_num_queries))
                if getattr(cfg, "rag_rerank_enabled", False):
                    fallback_mode = getattr(cfg, "rag_rerank_fallback_mode", "rrf")
                    fallback_reranker = None
                    if fallback_mode == "local_overlap":
                        fallback_reranker = LocalOverlapReranker()
                    elif fallback_mode == "cross_encoder":
                        fallback_reranker = LocalCrossEncoderReranker(
                            getattr(cfg, "rag_rerank_cross_encoder_model", ""),
                            fallback_reranker=LocalOverlapReranker(),
                        )
                    self.rag.set_reranker(LLMReranker(
                        self._llm_generate_fast,
                        cfg.rag_rerank_preview_len,
                        failure_threshold=cfg.rag_rerank_failure_threshold,
                        cooldown_seconds=cfg.rag_rerank_cooldown_seconds,
                        half_open_max_calls=cfg.rag_rerank_half_open_max_calls,
                        fallback_reranker=fallback_reranker,
                    ))
            except Exception as e:
                logger.warning("⚠️  RAG generate 函数注入失败: %s", e)

        # 加载持久化的长期记忆 + chat_history（best-effort）
        # 注：实际还原由 restore_from_db 完成，此处的 ltm.load_from_storage
        # 仅作为冷启动 RAG 索引装载前的快速预热——已合并到 _bootstrap_concurrent。

        # bootstrap 4 路并发（与 main bootstrapConcurrent 对齐）：
        #   - ragchunk.init(dim)         建 Milvus collection + ES 索引
        #   - restore_from_db            从 PG 恢复偏好 / LTM / 聊天记录
        #   - restore_rag_from_db        从 PG 恢复 RAG chunks
        #   - init_sandbox               Docker 探测 + exec_command 注册
        # 主线程同时同步注册 builtin 工具（rag_search 已在 _register_builtin_tools
        # 中提前完成，无须再放进并发组）。
        self.sandbox = None
        self._bootstrap_concurrent()

        # 知识图谱：必须在 restore_from_db 完成后串行执行（依赖 ltm 已就绪）
        self.kg = None
        try:
            init_knowledge_graph(self)
        except Exception as e:
            logger.warning("⚠️  init_knowledge_graph 失败: %s", e)
            self.kg = None
        # KG 就绪后把 graph_memory 挂回 mem stack（对应 main attachGraph）
        self.mem.attach_graph(getattr(self, "graph_memory", None))

        # 快照计数器（每 N 轮序列化 agent_state 到 PG）
        self._turn_count = 0
        self._snapshot_every = max(1, getattr(cfg, "snapshot_every_turns", 5) or 5)
        self._build_prompt_context()

        # 自适应上下文压缩器（ContextCompactor，对齐 MemGPT/AutoGen）
        self._rolling_summary = ""
        self._uncompacted_turns: List[Dict[str, str]] = []
        self.compactor = None
        if getattr(cfg, "enable_context_compactor", True):
            self.compactor = ContextCompactor(
                max_recent_turns=getattr(cfg, "context_compactor_max_recent_turns", 3),
                char_watermark=getattr(cfg, "context_compactor_char_watermark", 1500),
                summarizer_fn=self._compactor_summary_fn,
            )

        logger.info("✅ UnifiedAgent 初始化完成")

    def _bootstrap_concurrent(self) -> None:
        """与 main bootstrapConcurrent 对齐的 4 路并发启动。

        每个子任务自行做异常吞没，整体串行总耗时被压缩到最慢一项。
        """
        def _ragchunk_init():
            try:
                repo = getattr(getattr(self.inf, "repo", None), "ragchunk", None)
                if repo is not None and hasattr(repo, "init"):
                    repo.init(int(self.cfg.rag_milvus_dim or 1024))
            except Exception as e:
                logger.warning("⚠️  ragchunk.init 失败: %s", e)

        def _restore_db():
            try:
                restore_from_db(self)
            except Exception as e:
                logger.warning("⚠️  restore_from_db 失败: %s", e)

        def _restore_rag():
            try:
                restore_rag_from_db(self)
            except Exception as e:
                logger.warning("⚠️  restore_rag_from_db 失败: %s", e)

        def _init_sandbox():
            try:
                init_sandbox(self)
            except Exception as e:
                logger.warning("⚠️  init_sandbox 失败: %s", e)
                self.sandbox = None

        threads = [
            threading.Thread(target=_ragchunk_init, name="bootstrap:ragchunk", daemon=True),
            threading.Thread(target=_restore_db, name="bootstrap:restore-db", daemon=True),
            threading.Thread(target=_restore_rag, name="bootstrap:restore-rag", daemon=True),
            threading.Thread(target=_init_sandbox, name="bootstrap:sandbox", daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # ── 对外 API ────────────────────────────────────────────────────────────

    def cancel(self):
        """触发所有 in-flight 请求的取消。"""
        self._cancel_registry.cancel_all()

    def process(self, query: str) -> Response:
        return self.process_with_options(query, ChatOptions())

    def process_with_options(
        self,
        query: str,
        opts: ChatOptions,
        execution_context: Optional[RequestExecutionContext] = None,
        cancel_token=None,
    ) -> Response:
        if opts.conversation_id and self._conversations is not None:
            with self._conversations.lease(opts.conversation_id) as scoped:
                return scoped.process_with_options(
                    query, ChatOptions(use_rag=opts.use_rag), execution_context,
                    cancel_token=cancel_token,
                )
        if cancel_token is not None:
            # 复用调用方（HTTP 层）的取消令牌：客户端断连时取消能穿透到执行链。
            token, unregister = cancel_token, lambda: None
        else:
            token, unregister = self._cancel_registry.register()
        try:
            return self._dispatch(
                query,
                opts,
                token,
                execution_context=execution_context,
            )
        finally:
            unregister()

    def process_stream(
        self,
        query: str,
        opts: ChatOptions,
        on_event,
        execution_context: Optional[RequestExecutionContext] = None,
        cancel_token=None,
    ) -> Response:
        if opts.conversation_id and self._conversations is not None:
            with self._conversations.lease(opts.conversation_id) as scoped:
                return scoped.process_stream(
                    query, ChatOptions(use_rag=opts.use_rag), on_event,
                    execution_context, cancel_token=cancel_token,
                )
        if cancel_token is not None:
            token, unregister = cancel_token, lambda: None
        else:
            token, unregister = self._cancel_registry.register()
        try:
            return self._dispatch(
                query,
                opts,
                token,
                on_event,
                execution_context=execution_context,
            )
        finally:
            unregister()

    def route(self, user_input: str, use_rag: bool = False) -> str:
        return self.process_with_options(user_input, ChatOptions(use_rag=use_rag)).answer

    def get_tools(self) -> List[Dict[str, Any]]:
        return self.tool_executor.get_tool_descriptions()

    def add_tool(self, tool: Tool):
        self.tool_executor.add_tool(tool)

    def _register_builtin_tools(self) -> None:
        """兼容入口；Go 当前版不再把 RAG/文档领域操作注册成普通工具。"""
        return None

    def _register_document_tools(self) -> None:
        for tool in [
            self._write_document_tool(),
            self._list_documents_tool(),
            self._read_document_tool(),
            self._ingest_document_tool(),
        ]:
            self.tool_executor.add_tool(tool)

    def _write_document_tool(self) -> Tool:
        return Tool(
            name="write_document",
            description="将 Markdown 文档写入本地文档库，可选择同步入库 RAG。适合保存报告、总结、研究结果。",
            params=[
                {"name": "title", "type": "string", "description": "文档标题"},
                {"name": "content_md", "type": "string", "description": "Markdown 正文"},
                {"name": "doc_type", "type": "string", "description": "文档类型，如 report/note/summary"},
                {"name": "source", "type": "string", "description": "来源，如 agent_generated"},
                {"name": "summary", "type": "string", "description": "简短摘要"},
                {"name": "ingest_to_rag", "type": "boolean", "description": "是否写入后立即进入 RAG 索引"},
            ],
            func=lambda params: _json_string(self.write_document(
                WriteRequest(
                    title=_param_string(params, "title"),
                    doc_type=_param_string_default(params, "doc_type", "report"),
                    source=_param_string_default(params, "source", DOCUMENT_SOURCE_AGENT),
                    created_by="agent",
                    content_md=_param_string(params, "content_md") or _param_string(params, "content"),
                    summary=_param_string(params, "summary"),
                    metadata={"tool": "write_document"},
                ),
                _param_bool(params, "ingest_to_rag"),
            )),
        )

    def _list_documents_tool(self) -> Tool:
        return Tool(
            name="list_documents",
            description="列出本地文档库中的文档。",
            params=[],
            func=lambda params: _json_string({"documents": self.list_documents()}),
        )

    def _read_document_tool(self) -> Tool:
        return Tool(
            name="read_document",
            description="读取本地文档库中的指定文档最新版本。",
            params=[{"name": "document_id", "type": "string", "description": "文档 ID"}],
            func=lambda params: _json_string(self.get_document(_param_string(params, "document_id"))),
        )

    def _ingest_document_tool(self) -> Tool:
        return Tool(
            name="ingest_document",
            description="将本地文档库中的文档版本切分并写入 RAG 索引。",
            params=[
                {"name": "document_id", "type": "string", "description": "文档 ID"},
                {"name": "version_id", "type": "string", "description": "版本 ID，不填则使用最新版本"},
            ],
            func=lambda params: _json_string(self.ingest_document(
                _param_string(params, "document_id"),
                _param_string(params, "version_id"),
            )),
        )

    def _document_store(self):
        store = getattr(getattr(getattr(self, "inf", None), "repo", None), "documents", None)
        if store is None:
            raise RuntimeError("document library not configured")
        return store

    def _document_store_call(self, method_name: str, *args):
        """Apply tenant scoping when the repository supports ``user_id``."""
        method = getattr(self._document_store(), method_name)
        try:
            supports_user = "user_id" in inspect.signature(method).parameters
        except (TypeError, ValueError):
            supports_user = False
        if supports_user:
            return method(*args, user_id=getattr(self, "user_id", "default_user"))
        return method(*args)

    def write_document(self, req: WriteRequest, ingest_to_rag: bool = False) -> Dict[str, Any]:
        # Never trust a caller-provided owner in multi-user mode.
        req.created_by = getattr(self, "user_id", "default_user")
        wr = self._document_store_call("write", req)
        out = _to_jsonable(wr)
        if ingest_to_rag:
            out["ingest"] = self._ingest_content(
                wr.version.content_md,
                document_id=wr.document.id,
                version_id=wr.version.id,
                section=wr.document.doc_type,
            )
        return out

    def list_documents(self) -> List[Any]:
        return self._document_store_call("list")

    def get_document(self, document_id: str) -> Dict[str, Any]:
        doc, ver = self._document_store_call("get", document_id)
        return {"document": doc, "version": ver}

    def delete_document(self, document_id: str) -> None:
        if self.rag is not None and hasattr(self.rag, "delete_document"):
            self.rag.delete_document(document_id)
        self._document_store_call("delete", document_id)

    def ingest_document(self, document_id: str, version_id: str = "") -> Dict[str, Any]:
        if version_id:
            ver = self._document_store_call("get_version", version_id)
        else:
            _, ver = self._document_store_call("get", document_id)
        doc_id = document_id or ver.document_id
        return self._ingest_content(
            ver.content_md,
            document_id=doc_id,
            version_id=ver.id,
            section="document",
        )

    def _ingest_content(self, content: str, document_id: str, version_id: str, section: str) -> Dict[str, Any]:
        if self.rag is None:
            raise RuntimeError("RAG 引擎未初始化")
        try:
            chunk_count = self.rag.ingest(
                content,
                document_id=document_id,
                version_id=version_id,
                section=section,
            )
        except TypeError:
            chunk_count = self.rag.ingest(content)
        return {
            "chunk_count": int(chunk_count or 0),
            "document_id": document_id,
            "version_id": version_id,
            "section": section,
        }

    def register_mcp_tool(
        self, name: str, description: str, params: List[Dict[str, str]],
        func=None, endpoint: str = "",
    ):
        self.add_tool(new_mcp_tool(name, description, params, func=func, endpoint=endpoint))

    def register_mcp_server(self, endpoint: str) -> Dict[str, Any]:
        """按 MCP 协议握手并批量注册远端服务器提供的工具。

        与 register_mcp_tool 的单工具裸 HTTP 模式互补：这里完成 initialize
        握手与 tools/list 自动发现，远端每个工具以 mcp_ 前缀注册为本地 Tool，
        执行走 tools/call。与既有工具重名的一律跳过（不静默覆盖内置能力）。
        SSRF 校验沿用 validate_mcp_endpoint：默认拒绝内网/回环端点。
        """
        from internal.tools.mcp_client import initialize_session, list_remote_tools
        from internal.tools.tools import build_mcp_remote_tool, validate_mcp_endpoint

        validate_mcp_endpoint(endpoint)
        server_info = initialize_session(endpoint)
        specs = list_remote_tools(endpoint)
        existing = set(self.tool_executor.snapshot().keys())
        registered: List[str] = []
        skipped: List[str] = []
        for spec in specs:
            remote_name = str(spec.get("name") or "").strip()
            if not remote_name:
                continue
            local_name = f"mcp_{remote_name}"
            if local_name in existing:
                skipped.append(local_name)
                continue
            self.add_tool(build_mcp_remote_tool(endpoint, spec))
            existing.add(local_name)
            registered.append(local_name)
        return {"server": server_info, "registered": registered, "skipped": skipped}

    def rag_ingest(self, document: str) -> int:
        if self.rag is None:
            return 0
        return self.rag.ingest(document)

    def rag_query(self, question: str) -> tuple:
        if self.rag is None:
            return ("RAG 不可用", [])
        return self.rag.query(question)

    def status(self) -> Dict[str, Any]:
        return build_status(self)

    def infra_status(self) -> Dict[str, str]:
        return infra_status(self)

    # ── 调度主循环 ─────────────────────────────────────────────────────────

    def _dispatch(self, query, opts, token, on_event=None, execution_context=None):
        from .conversations import ConversationBusy
        # The legacy/default conversation also has a single in-flight turn.
        lock = getattr(self, "_turn_lock", None)
        if lock is not None and not lock.acquire(blocking=False):
            raise ConversationBusy("当前会话仍在执行")
        try:
            from internal.resilience.budget import request_budget
            with request_budget(getattr(self.cfg, "max_llm_calls_per_turn", 24),
                                getattr(self.cfg, "max_tool_calls_per_turn", 32)):
                return self._dispatch_once(query, opts, token, on_event, execution_context)
        finally:
            if lock is not None:
                lock.release()

    def _dispatch_once(
        self,
        query: str,
        opts: ChatOptions,
        token,
        on_event=None,
        execution_context: Optional[RequestExecutionContext] = None,
    ) -> Response:
        """三段式编排：prepare → dispatch → finalize（与 main runOnce 对齐）。"""
        from internal.harness.plugins import HarnessContext
        from internal.harness.execution import start_session
        policy = HarnessContext(session_id=getattr(self, "conversation_id", "") or "default",
                                user_id=getattr(self, "user_id", "default_user"), query=query,
                                plugins=getattr(self, "execution_plugins", []))
        start_session(policy)
        if policy.interrupted:
            denied = Response(query=query, answer=policy.state.get("final_answer", "请求被执行策略拒绝"),
                              interrupted=True, error=policy.interrupted_reason)
            _emit(on_event, "done", _to_jsonable(denied))
            return denied
        context = execution_context or RequestExecutionContext()
        pr = self._prepare(query, opts, context)
        resp = Response(
            query=query,
            mode=pr["mode"],
            trace_id=str(context.trace_id or uuid.uuid4()),
            experiment_exposure_id=str(context.experiment_exposure_id or ""),
            runtime_strategy_checksum=str(context.runtime_strategy_checksum or ""),
        )
        resp.extracted_info = pr["extracted"]
        if resp.extracted_info:
            _emit(on_event, "memory", {"extracted_info": resp.extracted_info})
        _emit(on_event, "route", {"mode": resp.mode})

        if token.is_cancelled():
            resp.interrupted = True
            resp.answer = "[已中断] 请求在开始前被取消"
            _emit(on_event, "done", _to_jsonable(resp))
            return resp

        self._dispatch_mode(pr, resp, token, on_event)

        if isinstance(resp.task, dict) and resp.task.get("status") == "interrupted":
            resp.interrupted = True

        if token.is_cancelled():
            resp.interrupted = True

        self._finalize(query, resp)
        _emit(on_event, "done", _to_jsonable(resp))
        return resp

    # ── prepare ──────────────────────────────────────────────────────────────

    def _prepare(
        self,
        query: str,
        opts: ChatOptions,
        execution_context: Optional[RequestExecutionContext] = None,
    ) -> Dict[str, Any]:
        """STM 写入 + 偏好提取 + 路由决策 + 上下文装配 + 历史构建。"""
        self.stm.add("user", query)
        self._save_chat_history("user", query)

        # 偏好/记忆抽取（同步规则即时回显 + 异步 LLM 扩展）
        # 注：async_update_memory 的写入路径直接修改 resp.extracted_info，
        # 这里复用一个 Response 占位以承接同步部分的输出。
        ph = Response(query=query)
        async_update_memory(self, query, ph)

        # 知识库请求走只读 RAG；其余请求进入统一工具执行链。
        mode, route_tools = self._route_decide(query, opts)

        mem_prefix = self._build_context_prefix(query, mode)
        hist_msgs = self._build_history_messages(query)

        return {
            "query": query,
            "mode": mode,
            "route_tools": route_tools,
            "mem_prefix": mem_prefix,
            "hist_msgs": hist_msgs,
            "extracted": ph.extracted_info,
            "runtime_overrides": dict(
                (execution_context or RequestExecutionContext()).runtime_overrides
                or {}
            ),
        }

    def _route_decide(self, query: str, opts: ChatOptions):
        """Choose Agentic RAG, lightweight RAG, or ordinary ReAct."""
        rag_loaded = bool(self.rag and getattr(self.rag, "loaded", False))
        if opts.use_rag and rag_loaded:
            if self._report_intent(query):
                return "rag_agent", None
            if self._report_intent_refined(query):
                return "rag_agent", None
            return "rag", None
        executor = getattr(self, "tool_executor", None)
        return "react", executor.snapshot() if executor is not None else {}

    @staticmethod
    def _report_intent(query: str) -> bool:
        return needs_subagent_plan(query)

    def _report_intent_refined(self, query: str) -> bool:
        """可选的 LLM 意图复核（默认关闭）：兜住关键词漏网的报告类任务。

        两级漏斗的第二级——关键词命中时 `_route_decide` 已短路返回，只有
        未命中的灰区消息才会走到这里。开关关闭、复核超时或输出非法时一律
        维持关键词结果（rag），路由行为与未启用时完全一致。
        """
        raw = os.getenv("AGI_INTENT_LLM_ENABLED", "").strip().lower()
        if raw not in {"1", "true", "yes", "on"}:
            return False
        try:
            return refine_intent_with_llm(self, query)
        except Exception:
            return False

    # ── dispatch ─────────────────────────────────────────────────────────────

    def _dispatch_mode(self, pr: Dict[str, Any], resp: Response, token, on_event=None) -> None:
        """按 mode 分发到对应 handler，把结果填回 resp。"""
        mode = pr["mode"]
        query = pr["query"]
        mem_prefix = pr["mem_prefix"]
        hist_msgs = pr["hist_msgs"]
        route_tools = pr["route_tools"]
        runtime_overrides = pr.get("runtime_overrides") or {}
        resp.extracted_info = pr["extracted"]

        if mode == "react":
            resp.answer, resp.steps, resp.task = self._run_react_with_tools(
                query, route_tools, mem_prefix, hist_msgs, token, on_event,
                allow_subagents=False,
            )
            self._apply_graph_tool_calls(resp)
            self._apply_structured_tool_output(resp)
            if resp.intent is None and not any(step.type == StepType.ACTION for step in resp.steps):
                resp.intent = "direct_chat"
        elif mode == "rag_agent":
            resp.answer, resp.steps, resp.task = self._run_react_with_tools(
                query, route_tools or {}, mem_prefix, hist_msgs, token, on_event,
                allow_subagents=True,
                force_subagent_plan=True,
            )
            self._apply_graph_tool_calls(resp)
        elif mode == "rag":
            resp.answer, resp.search_results, resp.rag_trace = self._run_rag_query_with_trace(
                query,
                runtime_overrides=runtime_overrides,
            )
            resp.rag_trace.setdefault("trace_id", resp.trace_id)
            if resp.runtime_strategy_checksum:
                resp.rag_trace.setdefault(
                    "runtime_strategy_checksum",
                    resp.runtime_strategy_checksum,
                )
            _emit(on_event, "rag_trace", resp.rag_trace)
            _emit(on_event, "rag_result", {"search_results": resp.search_results})
            _emit(on_event, "token", {"content": resp.answer})
        else:
            resp.answer = self._chat_response(mem_prefix, hist_msgs, token, on_event)

    # ── finalize ─────────────────────────────────────────────────────────────

    def _finalize(self, query: str, resp: Response) -> None:
        """assistant 写回 + 异步记忆抽取 + 异步图感知合并 + 事件发布 + 计数。"""
        self.stm.add("assistant", resp.answer)
        self._save_chat_history("assistant", resp.answer)
        if hasattr(self, "_uncompacted_turns") and isinstance(self._uncompacted_turns, list):
            self._uncompacted_turns.append({"role": "assistant", "content": resp.answer})

        # 异步：从回复中提取事实 → 长期记忆
        if getattr(self.cfg, "memory_store_exchange_facts", False):
            self.memory_writer.submit(
                lambda: extract_memory_from_reply(self, resp.answer, user_query=query)
            )
        # 异步：长期记忆合并/淘汰（有图层时走图感知合并）
        self.memory_writer.submit(lambda: maybe_consolidate_memory(self))

        # 每 N 轮快照一次 agent 状态到 PG
        self._turn_count += 1
        if self._turn_count % self._snapshot_every == 0:
            go_safe("snapshot", lambda: self._save_agent_snapshot(query, resp))

        try:
            self.inf.repo.events.publish(
                "agent.chat",
                json.dumps({"query": query, "mode": resp.mode}, ensure_ascii=False),
            )
        except Exception:
            pass

        resp.short_term_count = self.stm.count()
        resp.long_term_count = len(self.ltm.items)
        resp.preferences = self.preference.get_all()
        self._persist_trace(query, resp)

    def _persist_trace(self, query: str, resp: Response) -> None:
        repo = getattr(getattr(self.inf, "repo", None), "ragtrace", None)
        if repo is None or not hasattr(repo, "save"):
            return
        payload = sanitize_trace({
            "trace_id": resp.trace_id,
            "mode": resp.mode,
            "intent": resp.intent,
            "slots": resp.slots,
            "rag": resp.rag_trace,
            "steps": [asdict(step) for step in resp.steps],
            "tool_call": resp.tool_call,
            "tool_calls": resp.tool_calls,
            "task": resp.task,
            "interrupted": resp.interrupted,
            "fallback": resp.fallback,
            "error": resp.error,
            "experiment_exposure_id": resp.experiment_exposure_id,
            "runtime_strategy_checksum": resp.runtime_strategy_checksum,
        })
        try:
            repo.save(
                resp.trace_id,
                self.user_id,
                resp.mode,
                redact_text(query, max_length=1000),
                "interrupted" if resp.interrupted else "failed" if resp.error else "completed",
                payload,
            )
        except Exception as exc:
            logger.warning("⚠️  Trace 持久化失败 trace_id=%s: %s", resp.trace_id, exc)

    # ── Memory / Prompt 拼装 ───────────────────────────────────────────────

    def _llm_generate(self, system_prompt: str, user_msg: str) -> str:
        return self.llm.chat([Message(role="user", content=user_msg)], system_prompt=system_prompt)

    def _llm_generate_fast(self, system_prompt: str, user_msg: str) -> str:
        chat = getattr(self.llm, "chat_fast", None) or self.llm.chat
        return chat([Message(role="user", content=user_msg)], system_prompt=system_prompt)

    def _build_prompt_context(self) -> None:
        self.task_mem = TaskMemBuffer(20)
        self.tool_tracker = ToolStateTracker(10)
        registry = SourceRegistry()
        registry.register(ProfileSource(self.preference, self.ltm))
        registry.register(PlannerSource(self._planner_snapshot))
        registry.register(TaskMemSource(self.task_mem))
        registry.register(ToolStateSource(lambda: self.tool_executor.snapshot(), self.tool_tracker))
        registry.register(ConstraintsSource([
            Policy(pattern="rm -rf", reason="禁止破坏性删除命令", level="block"),
            Policy(pattern="sudo", reason="禁止提权命令", level="block"),
        ]))
        registry.register(RecallSource(self.ltm))
        self.prompt_assembler = ContextAssembler(default_schemas(), registry)

    def _planner_snapshot(self):
        task = self._cancel_registry.current_task() if hasattr(self, "_cancel_registry") else None
        if not task:
            return None
        steps = task.get("steps") or []
        return PlannerSnapshot(
            task_id=task.get("task_id", ""),
            query=task.get("query", ""),
            status=task.get("status", ""),
            phase=task.get("phase", ""),
            total_steps=len(steps),
            current_step=task.get("current_step", 0),
            interrupted_at=task.get("interrupted_at", ""),
        )

    def _build_context_prefix(self, query: str, mode: str = "chat") -> str:
        if not hasattr(self, "prompt_assembler"):
            self._build_prompt_context()
        try:
            try:
                embedding = self.llm.embed(query)
            except Exception:
                embedding = []
            current = self._cancel_registry.current_task() if hasattr(self, "_cancel_registry") else None
            task_id = str((current or {}).get("task_id", "")) if isinstance(current, dict) else ""
            return self.prompt_assembler.assemble(Query(
                text=query,
                embedding=embedding,
                task_id=task_id,
                mode=mode,
                user_id=getattr(self, "user_id", ""),
            )).render()
        except Exception as e:
            logger.warning("⚠️  promptctx 装配失败，降级到旧记忆前缀: %s", e)
            return self._build_memory_system_prefix(query)

    def push_task_mem(self, obs: StepObservation) -> None:
        if hasattr(self, "task_mem"):
            self.task_mem.push(obs)

    def record_tool_call(self, trace: ToolCallTrace) -> None:
        if hasattr(self, "tool_tracker"):
            self.tool_tracker.record(trace)

    def save_snapshot(self, task: dict) -> None:
        task_id = task.get("task_id", f"task_{int(time.time())}") if isinstance(task, dict) else f"task_{int(time.time())}"
        # 持锁追加到 cancel registry（与 main taskRuntime.appendSnapshot 对齐）
        if isinstance(task, dict):
            self._cancel_registry.append_snapshot({
                "state": dict(task),
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            })
        try:
            try:
                self.inf.repo.snapshot.save(task_id, json.dumps(task, ensure_ascii=False), user_id=self.user_id)
            except TypeError:
                self.inf.repo.snapshot.save(task_id, json.dumps(task, ensure_ascii=False))
        except Exception as e:
            logger.warning("⚠️  快照写入失败: %s", e)

    def persist_task_checkpoint(self, task: dict) -> None:
        self.inf.repo.snapshot.save(task['task_id'], json.dumps(task, ensure_ascii=False), user_id=self.user_id)

    def snapshot_list(self) -> List[dict]:
        """返回当前任务的内存快照列表（对应 main snapshotList）。"""
        return self._cancel_registry.snapshot_list()

    def _save_chat_history(self, role: str, content: str) -> None:
        """best-effort 写聊天记录。优先 chat_repo，其次 inf.repo.chat_history。"""
        chat_repo = getattr(self, "chat_repo", None)
        if chat_repo is None:
            chat_repo = getattr(getattr(self.inf, "repo", None), "chat_history", None)
        if chat_repo is not None and hasattr(chat_repo, "save"):
            try:
                try:
                    if getattr(self, "conversation_id", ""):
                        chat_repo.save(role, content, user_id=self.user_id, conversation_id=self.conversation_id)
                    else:
                        chat_repo.save(role, content, user_id=self.user_id)
                except TypeError:
                    if getattr(self, "conversation_id", ""):
                        raise  # Never spill a conversation into legacy user-wide history.
                    chat_repo.save(role, content)
            except Exception:
                pass

    def _build_memory_system_prefix(self, query: str = "") -> str:
        parts: List[str] = []
        prefs = self.preference.get_all()
        if prefs:
            parts.append(f"用户偏好: {json.dumps(prefs, ensure_ascii=False)}")
        memories = self.ltm.recall(query, self.cfg.long_term_top_k) if query else []
        if memories:
            parts.append("相关记忆:\n" + "\n".join(f"- {m.content}" for m in memories))
        return "\n".join(parts)

    def _recent_history_for_rag(self) -> List[HistoryMessage]:
        msgs = []
        rolling_summary = getattr(self, "_rolling_summary", "")
        if rolling_summary:
            msgs.append(HistoryMessage(role="system", content=f"【前情提要与历史背景摘要】\n{rolling_summary}"))
        stm_msgs = [HistoryMessage(role=m["role"], content=m["content"]) for m in self.stm.get()]
        msgs.extend(stm_msgs[-5:] if rolling_summary else stm_msgs[-6:])
        return msgs

    def _run_rag_query(self, query: str):
        answer, results, _trace = self._run_rag_query_with_trace(query)
        return answer, results

    def _run_rag_query_with_trace(
        self,
        query: str,
        *,
        runtime_overrides: Optional[Dict[str, Any]] = None,
    ):
        if self.rag is None:
            return "RAG 不可用", [], {
                "original_query": query,
                "decision": "unavailable",
                "reason": "rag_engine_unavailable",
            }
        if hasattr(self.rag, "query_with_history_trace"):
            method = self.rag.query_with_history_trace
            overrides = dict(runtime_overrides or {})
            if overrides:
                try:
                    parameters = inspect.signature(method).parameters
                except (TypeError, ValueError):
                    parameters = {}
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if "runtime_overrides" in parameters or accepts_kwargs:
                    return method(
                        query,
                        self._recent_history_for_rag(),
                        runtime_overrides=overrides,
                    )
                raise RuntimeError(
                    "RAG engine cannot apply the compiled request-scoped strategy"
                )
            return method(query, self._recent_history_for_rag())
        if hasattr(self.rag, "query_with_history"):
            answer, results = self.rag.query_with_history(query, self._recent_history_for_rag())
        else:
            answer, results = self.rag.query(query)
        return answer, results, {
            "original_query": query,
            "decision": "answer" if results else "no_answer",
            "reason": "legacy_rag_engine",
        }

    def _compactor_summary_fn(self, turns_to_compress: List[Dict[str, str]]) -> str:
        """为被滑动窗口淘汰的历史对话生成滚动摘要（优先快模型 LLM，离线/测试降级为启发式规则）。"""
        if not turns_to_compress:
            return ""
        if (
            hasattr(self, "llm")
            and self.llm is not None
            and hasattr(self, "cfg")
            and getattr(self.cfg, "is_real_llm", lambda: False)()
        ):
            try:
                transcript = "\n".join(
                    f"{t.get('role', 'user')}: {t.get('content', '')}"
                    for t in turns_to_compress
                )
                prompt = [
                    Message(
                        role="user",
                        content=(
                            "请用简明扼要的一两句话提炼以下多轮对话的关键背景、用户关键偏好与重要决策，"
                            "作为后续对话的前情提要，不要废话：\n\n" + transcript
                        ),
                    )
                ]
                summary = self.llm.chat_fast(prompt)
                if summary and summary.strip():
                    return summary.strip()
            except Exception as e:
                logger.debug("LLM 摘要生成失败，降级为规则提炼: %s", e)
        return ContextCompactor._default_heuristic_summary(turns_to_compress)

    def _build_history_messages(self, query: str) -> List[Message]:
        compactor = getattr(self, "compactor", None)
        if compactor is None or not getattr(self.cfg, "enable_context_compactor", True):
            msgs = [Message(role=m["role"], content=m["content"]) for m in self.stm.get()]
            if not msgs or msgs[-1].content != query:
                msgs.append(Message(role="user", content=query))
            return msgs

        # 启用了 ContextCompactor
        uncompacted = getattr(self, "_uncompacted_turns", None)
        if uncompacted is None:
            uncompacted = []
            self._uncompacted_turns = uncompacted

        # 若本地缓存为空但 stm 中已有对话历史（例如 DB 还原或多会话克隆），从 stm 初始化
        if not uncompacted and hasattr(self, "stm"):
            stm_items = self.stm.get()
            if stm_items:
                uncompacted.extend(stm_items)

        # 确保当前 query 在消息末尾
        if not uncompacted or uncompacted[-1].get("content") != query:
            uncompacted.append({"role": "user", "content": query})

        existing_summary = getattr(self, "_rolling_summary", "")
        res = compactor.compact(uncompacted, existing_summary=existing_summary)

        if res.compressed_turns_count > 0:
            self._rolling_summary = res.rolling_summary
            self._uncompacted_turns = list(res.recent_history)
        else:
            self._rolling_summary = res.rolling_summary

        msgs: List[Message] = []
        if self._rolling_summary:
            msgs.append(Message(role="system", content=f"【前情提要与历史背景摘要】\n{self._rolling_summary}"))

        for m in res.recent_history:
            msgs.append(Message(role=m.get("role", "user"), content=m.get("content", "")))

        return msgs

    def _chat_response(self, mem_prefix: str, hist_msgs: List[Message], token=None, on_event=None) -> str:
        system_prompt = "你是一个简洁的AI助手。结合你掌握的用户信息，使回答更个性化。"
        if mem_prefix:
            system_prompt = mem_prefix + "\n\n" + system_prompt
        return self._chat_llm(system_prompt, hist_msgs, token, on_event)

    def _chat_llm(self, system_prompt: str, messages: List[Message], token=None, on_event=None) -> str:
        if on_event is None:
            return self.llm.chat(messages, system_prompt=system_prompt)
        return self.llm.chat_stream_context(
            token,
            system_prompt,
            messages,
            on_token=lambda content: _emit(on_event, "token", {"content": content}),
        )

    # ── 工具参数与偏好 ────────────────────────────────────────────────────

    # 偏好键 → 候选工具参数名（与 main 分支 fillParamsFromPreference 完全一致）。
    # 当前 planner 直接产出工具参数，此映射未接入执行链；作为独立纯逻辑保留
    # （有单测覆盖），供未来工具执行路径复用。
    _PREFERENCE_PARAM_MAP = {
        "城市": ("city", "location", "location_name"),
        "时区": ("timezone", "tz", "time_zone"),
        "姓名": ("name", "username", "user_name"),
        "语言": ("language", "lang"),
        "国家": ("country", "nation"),
    }

    def _fill_params_from_preference(self, params: Dict[str, Any]) -> None:
        """在工具 Execute 之前用偏好补齐空槽位（不覆盖既有非空值）。

        与 main 分支 UnifiedAgent.fillParamsFromPreference 对齐：取偏好快照后
        按 5 键映射表逐个尝试填入候选参数名，仅当对应槽位缺失或为空字符串时才赋值。
        """
        if not isinstance(params, dict):
            return
        try:
            snapshot = self.preference.get_all() or {}
        except Exception:
            return
        if not snapshot:
            return
        for pref_key, candidates in self._PREFERENCE_PARAM_MAP.items():
            value = snapshot.get(pref_key)
            if value is None or str(value) == "":
                continue
            for name in candidates:
                existing = params.get(name)
                if existing is None or str(existing) == "":
                    params[name] = value

    # ── 图调度（统一 react 入口） ──────────────────────────────────────────

    def _run_react_with_tools(
        self, query: str, tools_map: Dict[str, Tool], mem_prefix: str,
        hist_msgs: List[Message], token, on_event=None,
        allow_subagents: bool = False,
        force_subagent_plan: bool = False,
    ):
        """ReAct 模式入口：与 main 分支 runReAct 行为一致。

        - llm_plan_graph 拿到节点列表；
        - 节点为空 → 直接调 chat LLM 给一句话回答（对应 Go chatLLM 兜底），不再做工具迭代；
        - 节点非空 → 走 GraphRuntime 拓扑分层 + race + 重试；执行结束后用
          _generate_final_answer（对应 Go llmGenerate）合成自然语言回复。
        """
        task = {
            "task_id": f"task-{time.time_ns()}", "query": query,
            "status": "running", "phase": "planning", "steps": [],
        }
        task_lease = None
        self._cancel_registry.set_task(task)
        try:
            tools_map = tools_map or {}
            if force_subagent_plan:
                plan_nodes = subagent_pipeline_nodes(query)
            else:
                plan_nodes = llm_plan_graph(
                    self, query, tools_map, mem_prefix, allow_subagents
                )
            if not plan_nodes:
                # 与 Go runReAct: planNodes 空 → chatLLM 一句话答复
                answer = self._chat_response(mem_prefix, hist_msgs, token, on_event)
                task.update(status="completed", phase="done", result=answer)
                self.save_snapshot(task)
                return answer, [], task

            if any(node.tool_name == "farm_copilot" for node in plan_nodes):
                try:
                    from internal.application.farm_agent import detect_farm_guardrails

                    for guardrail in detect_farm_guardrails(query):
                        _emit(on_event, "guardrail", guardrail)
                except Exception:
                    pass
            _emit(on_event, "plan_created", {
                "task_id": task["task_id"],
                "nodes": [
                    {
                        "id": node.id,
                        "type": getattr(node.type, "value", str(node.type)),
                        "tool": node.tool_name,
                        "reason": node.name,
                        "arguments": dict(node.params or {}),
                        "depends_on": list(node.depends_on or []),
                    }
                    for node in plan_nodes
                ],
            })

            from internal.graph.task_graph import TaskGraph

            graph = TaskGraph(plan_nodes)
            try:
                graph.validate()
            except Exception:
                # Never remove dependencies to make an invalid plan runnable.
                task.update(status='interrupted', phase='invalid_plan', result='执行计划依赖不合法，请重新规划')
                self.save_snapshot(task)
                return task['result'], [], task
            task["phase"] = "executing"
            task["steps"] = [
                {
                    "id": index, "name": node.name,
                    "node_id": node.id,
                    "tool_name": node.tool_name,
                    "params": dict(node.params or {}), "status": "pending",
                    "retry_count": 0,
                }
                for index, node in enumerate(graph.nodes.values(), start=1)
            ]
            task["current_step"] = 0
            task["graph"] = _graph_to_contract(graph)
            if hasattr(self, "task_mem"):
                self.task_mem.reset()
            self.save_snapshot(task)
            host_workspace = prepare_artifact_workspace(self, task["task_id"])
            if host_workspace:
                _emit(on_event, "sandbox_ready", {"workspace": host_workspace})
            cfg = GraphConfig(
                max_parallel=getattr(self.cfg, "graph_max_parallel", 2),
                race_timeout_ms=getattr(self.cfg, "graph_race_timeout_ms", 30000),
                enable_racing=getattr(self.cfg, "graph_enable_racing", True),
                replan_enabled=getattr(self.cfg, "graph_replan_enabled", False),
                max_replan=getattr(self.cfg, "graph_max_replan", 2),
                replan_on_failed=getattr(self.cfg, "graph_replan_on_failed", False),
            )
            runtime = GraphRuntime(graph, self, cfg, tools_map, task, on_event=on_event)
            journal = getattr(self.inf.repo, 'action_journal', None)
            if journal is not None:
                from .recovery import TaskLease
                task_lease = TaskLease(journal, self.user_id, task['task_id'])
                task_lease.__enter__()
                runtime._lease = task_lease
            runtime.set_replan_context(query, mem_prefix, allow_subagents)
            result = runtime.execute(token)
            steps = _graph_to_react_steps(graph)
            cancelled = bool(token is not None and callable(getattr(token, "is_cancelled", None)) and token.is_cancelled())
            if result.interrupted or cancelled:
                task["phase"] = "interrupted"
                task["status"] = "interrupted"
                summary = "; ".join(
                    f"{node.tool_name}:{getattr(node.status, 'value', str(node.status))}" for node in graph.nodes.values()
                )
                return f"[已中断] {result.interrupted_msg}\n当前进度：{summary}", steps, task

            task["phase"] = "generating"
            final_answer = self._generate_final_answer(query, steps, mem_prefix, token, on_event)
            graph_failed = any(
                getattr(node.status, "value", str(node.status)) == "failed"
                for node in graph.nodes.values()
            )
            if task_lease is not None:
                task_lease.refresh()
            from .artifact import ArtifactExecutionBlocked
            try:
                artifact_step = None if graph_failed else produce_artifact(
                    self, query, final_answer, host_workspace, on_event=on_event, token=token,
                )
            except ArtifactExecutionBlocked as exc:
                task.update(status='interrupted', phase='artifact_blocked',
                            result=final_answer + '\n\n[产物生成中断] ' + str(exc))
                return task['result'], steps, task
            if artifact_step is not None:
                step = ReActStep(**artifact_step)
                steps.append(step)
                final_answer += "\n\n---\n" + step.content
            steps.append(ReActStep(type=StepType.FINAL_ANSWER, content=final_answer))
            if result.interrupted:
                task["status"] = "interrupted"
                task["phase"] = "interrupted"
            elif graph_failed:
                task["status"] = "failed"
                task["phase"] = "failed"
            else:
                task["status"] = "completed"
                task["phase"] = "done"
            task["result"] = final_answer
            task["graph"] = _graph_to_contract(graph)
            return final_answer, steps, task
        finally:
            # Go 版保留最近任务及快照供 /api/snapshots 和恢复接口读取。
            try:
                if task.get('recovery'):
                    if task_lease is not None:
                        task_lease.refresh()
                    self.persist_task_checkpoint(task)
            finally:
                if task_lease is not None and hasattr(task_lease, 'worker'):
                    task_lease.__exit__(None, None, None)

    def _generate_final_answer(self, query: str, steps: List[ReActStep], mem_prefix: str, token=None, on_event=None) -> str:
        # Domain tools return an auditable deterministic answer.  In Mock mode
        # present that verified answer instead of wrapping the JSON in a fake
        # LLM/typewriter response.
        if not self.cfg.is_real_llm():
            structured = _latest_structured_tool_result(steps)
            if structured is not None:
                answer = str(structured.get("answer") or "").strip()
                if answer:
                    _emit(on_event, "token", {"content": answer})
                    return answer
        steps_str = "\n".join(f"{s.type}: {s.content}" for s in steps)
        prompt = f"""基于以下推理过程，给出最终答案。

任务: {query}

记忆上下文:
{mem_prefix or '（无）'}

推理过程:
{steps_str}

请用自然语言总结最终答案，不要包含 Action/Final 等关键字。
"""
        messages = [Message(role="user", content=prompt)]
        return self._chat_llm("你是一个总结助手，能够基于推理过程给出简洁的最终答案。", messages, token, on_event)

    @staticmethod
    def _apply_graph_tool_calls(resp: Response) -> None:
        task = resp.task if isinstance(resp.task, dict) else {}
        graph = task.get("graph") if isinstance(task, dict) else None
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if not isinstance(nodes, dict):
            return
        calls: List[Dict[str, Any]] = []
        for node in nodes.values():
            if not isinstance(node, dict) or not node.get("tool_name"):
                continue
            status = str(node.get("status") or "").lower()
            result = node.get("result") or ""
            success = status == "done"
            try:
                result_payload = json.loads(result) if isinstance(result, str) else result
                if isinstance(result_payload, dict) and result_payload.get("ok") is False:
                    success = False
            except json.JSONDecodeError:
                pass
            calls.append({
                "tool_name": str(node.get("tool_name")),
                "params": dict(node.get("params") or {}),
                "tool_result": result,
                "success": success,
                "error": str(node.get("error") or "") or None,
            })
        resp.tool_calls = calls
        if len(calls) == 1:
            resp.tool_call = dict(calls[0])

    @staticmethod
    def _apply_structured_tool_output(resp: Response) -> None:
        structured = _latest_structured_tool_result(resp.steps)
        if structured is None:
            return
        intent = str(structured.get("intent") or "").strip()
        evidence = structured.get("evidence")
        slots = evidence.get("slots") if isinstance(evidence, dict) else None
        if intent:
            resp.intent = intent
        if isinstance(slots, dict):
            resp.slots = dict(slots)
        if structured.get("ok") is False:
            resp.fallback = True
            error = structured.get("error")
            if isinstance(error, dict):
                resp.error = str(error.get("message") or error.get("code") or "工具执行失败")
            else:
                resp.error = str(error or "工具执行失败")

    def _save_agent_snapshot(self, query: str, resp: Response):
        """每 N 轮把 agent 整体状态序列化到 PG（含路由 mode/计数/偏好）。"""
        snapshot = {
            "task_id": f"agent_{int(time.time())}",
            "query": query,
            "mode": resp.mode,
            "short_term_count": resp.short_term_count,
            "long_term_count": resp.long_term_count,
            "preferences": resp.preferences,
            "timestamp": time.time(),
        }
        try:
            try:
                self.inf.repo.snapshot.save(snapshot["task_id"], json.dumps(snapshot, ensure_ascii=False), user_id=self.user_id)
            except TypeError:
                self.inf.repo.snapshot.save(snapshot["task_id"], json.dumps(snapshot, ensure_ascii=False))
        except Exception as e:
            logger.warning("⚠️  agent 快照写入失败: %s", e)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def close(self):
        try:
            self.memory_writer.stop()
        except Exception:
            pass


def _param_string(params: Dict[str, Any], key: str) -> str:
    if not isinstance(params, dict):
        return ""
    value = params.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _param_string_default(params: Dict[str, Any], key: str, fallback: str) -> str:
    value = _param_string(params, key)
    return value if value else fallback


def _param_bool(params: Dict[str, Any], key: str) -> bool:
    if not isinstance(params, dict):
        return False
    value = params.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "on"}
    return bool(value)


def _json_string(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, indent=2)


def _latest_structured_tool_result(steps: List[ReActStep]) -> Optional[Dict[str, Any]]:
    for step in reversed(steps or []):
        if step.type != StepType.OBSERVATION:
            continue
        try:
            payload = json.loads(step.content or "")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        evidence = payload.get("evidence")
        if (
            isinstance(evidence, dict)
            and evidence.get("tool") == "farm_copilot"
            and payload.get("intent")
            and "answer" in payload
        ):
            return payload
    return None


def _graph_to_contract(graph) -> Dict[str, Any]:
    nodes: Dict[str, Any] = {}
    for node_id, node in graph.nodes.items():
        is_subagent = getattr(node.type, "value", node.type) == "sub_agent"
        item = {
            "id": node.id,
            "type": getattr(node.type, "value", str(node.type)),
            "name": node.name,
            "params": dict(node.params or {}),
            "depends_on": list(node.depends_on or []),
            "optional_depends_on": list(node.optional_depends_on or []),
            "status": getattr(node.status, "value", str(node.status)),
            "retry_count": node.retry_count,
        }
        if is_subagent:
            item["agent_name"] = node.tool_name
            item["goal"] = str((node.params or {}).get("goal") or "")
        else:
            item["tool_name"] = node.tool_name
        if node.race_group:
            item["race_group"] = node.race_group
        if node.result:
            item["result"] = node.result
        if node.error:
            item["error"] = node.error
        nodes[node_id] = item
    return {
        "nodes": nodes,
        "adj_list": {key: list(value) for key, value in graph.adj.items()},
        "in_degree": dict(graph.indegree),
    }


def _graph_to_react_steps(graph) -> List[ReActStep]:
    steps: List[ReActStep] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        steps.append(ReActStep(type=StepType.THOUGHT, content=node.name))
        steps.append(ReActStep(
            type=StepType.ACTION,
            content=f"调用 {node.tool_name}",
            tool=node.tool_name,
            params=dict(node.params or {}),
        ))
        status = getattr(node.status, "value", str(node.status))
        if status == "done":
            content = node.result
        elif status == "failed":
            # A semantic-failure envelope contains the safe user-facing answer
            # and evidence.  Keep it available to the response/adapter layer.
            content = node.result or f"执行失败: {node.error}"
        elif status == "skipped":
            content = "[竞速跳过] 其他节点已胜出"
        elif status == "cancelled":
            content = "[已中断]"
        else:
            continue
        steps.append(ReActStep(type=StepType.OBSERVATION, content=content))
    return steps


def _emit(on_event, event_type: str, data: Any) -> None:
    if on_event is None:
        return
    on_event({"type": event_type, "data": _to_jsonable(data)})


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return _to_jsonable(asdict(value))
    return value
