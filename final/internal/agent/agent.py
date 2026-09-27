"""Agent composition and backwards-compatible facade.

Turn orchestration, planning, context, memory, retrieval and documents are
implemented in stateless services receiving the current conversation runtime.
"""

import logging
import threading
from typing import Any, Dict, List, Optional

from config.config import APIConfig
from internal.document.library import WriteRequest
from internal.infra.infra import Infrastructure
from internal.llm.llm import Client as LLMClient, Message
from internal.memory.memory import LongTerm, Preference, ShortTerm
from internal.memory.mem_stack import ConsolidationConfig, MemoryStack
from internal.promptctx import (
    StepObservation,
    ToolCallTrace,
)
from internal.promptctx.compactor import ContextCompactor
from internal.rag.rag import Engine as RAGEngine
from internal.rag.reranker import LLMReranker
from internal.rag.local_reranker import LocalCrossEncoderReranker, LocalOverlapReranker
from internal.rag.rewriter import HistoryMessage, LLMRewriter
from internal.tools.tools import Tool, ToolExecutor, default_tools

from .cancel import CancelRegistry
from .artifact import produce_artifact
from .init_sandbox import init_sandbox
from .memory_writer import (
    AsyncMemoryWriter,
    async_update_memory,
)
from .restore import init_knowledge_graph, restore_from_db, restore_rag_from_db
from .planner import (
    llm_plan_graph,
)
from .status import infra_status, status as build_status
from .subagents import register_builtin_subagents


from . import context_service, memory_service, retrieval_service, document_service, planning_service
from .contracts import (
    StepType as StepType,
    ReActStep as ReActStep,
    ChatOptions as ChatOptions,
    RequestExecutionContext as RequestExecutionContext,
    Response as Response,
)
from .serialization import (
    _param_string as _param_string,
    _param_string_default as _param_string_default,
    _param_bool as _param_bool,
    _json_string as _json_string,
    _latest_structured_tool_result as _latest_structured_tool_result,
    _graph_to_contract as _graph_to_contract,
    _graph_to_react_steps as _graph_to_react_steps,
    _emit as _emit,
    _to_jsonable as _to_jsonable,
)

logger = logging.getLogger(__name__)


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
                    self.rag.set_reranker(
                        LLMReranker(
                            self._llm_generate_fast,
                            cfg.rag_rerank_preview_len,
                            failure_threshold=cfg.rag_rerank_failure_threshold,
                            cooldown_seconds=cfg.rag_rerank_cooldown_seconds,
                            half_open_max_calls=cfg.rag_rerank_half_open_max_calls,
                            fallback_reranker=fallback_reranker,
                        )
                    )
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
        from internal.tools.manifest import apply_tool_manifest

        apply_tool_manifest(self)

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
                    query,
                    ChatOptions(use_rag=opts.use_rag),
                    execution_context,
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
                    query,
                    ChatOptions(use_rag=opts.use_rag),
                    on_event,
                    execution_context,
                    cancel_token=cancel_token,
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
        return document_service.register_document_tools(self)

    def _write_document_tool(self) -> Tool:
        return document_service.write_document_tool(self)

    def _list_documents_tool(self) -> Tool:
        return document_service.list_documents_tool(self)

    def _read_document_tool(self) -> Tool:
        return document_service.read_document_tool(self)

    def _ingest_document_tool(self) -> Tool:
        return document_service.ingest_document_tool(self)

    def _document_store(self):
        return document_service.document_store(self)

    def _document_store_call(self, method_name: str, *args):
        return document_service.document_store_call(self, method_name, *args)

    def write_document(self, req: WriteRequest, ingest_to_rag: bool = False) -> Dict[str, Any]:
        return document_service.write_document(self, req, ingest_to_rag)

    def list_documents(self) -> List[Any]:
        return document_service.list_documents(self)

    def get_document(self, document_id: str) -> Dict[str, Any]:
        return document_service.get_document(self, document_id)

    def delete_document(self, document_id: str) -> None:
        return document_service.delete_document(self, document_id)

    def ingest_document(self, document_id: str, version_id: str = "") -> Dict[str, Any]:
        return document_service.ingest_document(self, document_id, version_id)

    def _ingest_content(self, content: str, document_id: str, version_id: str, section: str) -> Dict[str, Any]:
        return document_service.ingest_content(self, content, document_id, version_id, section)

    def register_mcp_tool(
        self, name: str, description: str, params: List[Dict[str, str]], func=None, endpoint: str = ""
    ):
        return document_service.register_mcp_tool(self, name, description, params, func, endpoint)

    def register_mcp_server(self, endpoint: str) -> Dict[str, Any]:
        return document_service.register_mcp_server(self, endpoint)

    def rag_ingest(self, document: str) -> int:
        return retrieval_service.rag_ingest(self, document)

    def rag_query(self, question: str) -> tuple:
        return retrieval_service.rag_query(self, question)

    def status(self) -> Dict[str, Any]:
        return build_status(self)

    def infra_status(self) -> Dict[str, str]:
        return infra_status(self)

    # ── 调度主循环 ─────────────────────────────────────────────────────────
    #
    # 聊天回合编排自 L3c 起由 internal/chat_graph 承担（LangGraph StateGraph，
    # 默认 engine；chat.engine: native 保留原生回退路径）。以下 facade 方法
    # 保持原有签名，供图节点与测试复用同一批 agent 方法。

    def _dispatch(self, query, opts, token, on_event=None, execution_context=None):
        from internal.chat_graph import dispatch

        return dispatch(self, query, opts, token, on_event, execution_context)

    def _dispatch_once(
        self,
        query: str,
        opts: ChatOptions,
        token,
        on_event=None,
        execution_context: Optional[RequestExecutionContext] = None,
    ) -> Response:
        from internal.chat_graph import dispatch_once

        return dispatch_once(self, query, opts, token, on_event, execution_context)

    # ── prepare ──────────────────────────────────────────────────────────────

    def _prepare(
        self, query: str, opts: ChatOptions, execution_context: Optional[RequestExecutionContext] = None
    ) -> Dict[str, Any]:
        from internal.chat_graph import prepare

        return prepare(self, query, opts, execution_context, memory_update=async_update_memory)

    def _route_decide(self, query: str, opts: ChatOptions):
        from internal.chat_graph import route_decide

        return route_decide(self, query, opts)

    @staticmethod
    def _report_intent(query: str) -> bool:
        from internal.chat_graph import report_intent

        return report_intent(query)

    def _report_intent_refined(self, query: str) -> bool:
        from internal.chat_graph import report_intent_refined

        return report_intent_refined(self, query)

    # ── dispatch ─────────────────────────────────────────────────────────────

    def _dispatch_mode(self, pr: Dict[str, Any], resp: Response, token, on_event=None) -> None:
        from internal.chat_graph import dispatch_mode

        return dispatch_mode(self, pr, resp, token, on_event)

    # ── finalize ─────────────────────────────────────────────────────────────

    def _finalize(self, query: str, resp: Response) -> None:
        return memory_service.finalize(self, query, resp)

    def _persist_trace(self, query: str, resp: Response) -> None:
        return memory_service.persist_trace(self, query, resp)

    # ── Memory / Prompt 拼装 ───────────────────────────────────────────────

    def _llm_generate(self, system_prompt: str, user_msg: str) -> str:
        return self.llm.chat([Message(role="user", content=user_msg)], system_prompt=system_prompt)

    def _llm_generate_fast(self, system_prompt: str, user_msg: str) -> str:
        chat = getattr(self.llm, "chat_fast", None) or self.llm.chat
        return chat([Message(role="user", content=user_msg)], system_prompt=system_prompt)

    def _build_prompt_context(self) -> None:
        return context_service.build_prompt_context(self)

    def _planner_snapshot(self):
        return context_service.planner_snapshot(self)

    def _build_context_prefix(self, query: str, mode: str = "chat") -> str:
        return context_service.build_context_prefix(self, query, mode)

    def push_task_mem(self, obs: StepObservation) -> None:
        return memory_service.push_task_mem(self, obs)

    def record_tool_call(self, trace: ToolCallTrace) -> None:
        return memory_service.record_tool_call(self, trace)

    def save_snapshot(self, task: dict) -> None:
        return memory_service.save_snapshot(self, task)

    def persist_task_checkpoint(self, task: dict) -> None:
        return memory_service.persist_task_checkpoint(self, task)

    def snapshot_list(self) -> List[dict]:
        return memory_service.snapshot_list(self)

    def _save_chat_history(self, role: str, content: str) -> None:
        return memory_service.save_chat_history(self, role, content)

    def _build_memory_system_prefix(self, query: str = "") -> str:
        return context_service.build_memory_system_prefix(self, query)

    def _recent_history_for_rag(self) -> List[HistoryMessage]:
        return retrieval_service.recent_history_for_rag(self)

    def _run_rag_query(self, query: str):
        return retrieval_service.run_rag_query(self, query)

    def _run_rag_query_with_trace(self, query: str, *, runtime_overrides: Optional[Dict[str, Any]] = None):
        return retrieval_service.run_rag_query_with_trace(self, query, runtime_overrides=runtime_overrides)

    def _compactor_summary_fn(self, turns_to_compress: List[Dict[str, str]]) -> str:
        return context_service.compactor_summary_fn(self, turns_to_compress)

    def _build_history_messages(self, query: str) -> List[Message]:
        return context_service.build_history_messages(self, query)

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
        self,
        query: str,
        tools_map: Dict[str, Tool],
        mem_prefix: str,
        hist_msgs: List[Message],
        token,
        on_event=None,
        allow_subagents: bool = False,
        force_subagent_plan: bool = False,
    ):
        return planning_service.run_react_with_tools(
            self,
            query,
            tools_map,
            mem_prefix,
            hist_msgs,
            token,
            on_event,
            allow_subagents,
            force_subagent_plan,
            dependencies=planning_service.PlanningDependencies(
                plan_graph=llm_plan_graph, artifact_writer=produce_artifact
            ),
        )

    def _generate_final_answer(
        self, query: str, steps: List[ReActStep], mem_prefix: str, token=None, on_event=None
    ) -> str:
        return planning_service.generate_final_answer(self, query, steps, mem_prefix, token, on_event)

    @staticmethod
    def _apply_graph_tool_calls(resp: Response) -> None:
        return planning_service.apply_graph_tool_calls(resp)

    @staticmethod
    def _apply_structured_tool_output(resp: Response) -> None:
        return planning_service.apply_structured_tool_output(resp)

    def _save_agent_snapshot(self, query: str, resp: Response):
        return memory_service.save_agent_snapshot(self, query, resp)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def close(self):
        try:
            self.memory_writer.stop()
        except Exception:
            pass
