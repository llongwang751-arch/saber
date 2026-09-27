"""Chat routes registered by the application composition root."""

import asyncio
import inspect
import logging
import queue
import threading
import time
import uuid

from fastapi import HTTPException, Request, Response as HTTPResponse
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from internal.application.api import current_agent
from internal.agent.contracts import ChatOptions
from internal.agent.cancel import CancelToken
from internal.agent.run_service import RunConflict, RunCapacityExceeded
from internal.application.run_runtime import get_run_service
from internal.experimentation.public import redact_public_experiment_trace

from .models import ChatRequest
from .http_contracts import _response_to_dict, _rag_result_to_main_contract, _sse, _jsonable, _sanitize_stream_done
from .chat_experiments import _begin_online_rag_exposure, _finish_online_rag_exposure

logger = logging.getLogger(__name__)


def _observe_run(request, agent, req, token, *, streaming=False):
    try:
        return get_run_service(request).begin_inline(
            str(request.state.user["id"]),
            agent,
            req.message,
            conversation_id=req.conversation_id,
            use_rag=req.use_rag,
            token=token,
            streaming=streaming,
        )
    except RunConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except RunCapacityExceeded as exc:
        raise HTTPException(429, str(exc)) from exc


def _cancel_kwargs(method, token):
    parameters = inspect.signature(method).parameters
    return {"cancel_token": token} if "cancel_token" in parameters else {}


def register_chat_routes(app, agent, inf, cfg):
    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request, http_response: HTTPResponse):
        exposure = None
        response = None
        observation = None
        token = CancelToken()
        started = time.perf_counter()
        try:
            active_agent = current_agent(request)
            opts = ChatOptions(use_rag=req.use_rag, conversation_id=req.conversation_id)
            # The validated request id is also the turn/trace id.  A client
            # that falls back from SSE to sync with the same X-Request-ID gets
            # the same idempotent exposure instead of being counted twice.
            trace_id = str(getattr(request.state, "request_id", "") or uuid.uuid4())
            exposure, execution_context = _begin_online_rag_exposure(
                request,
                req,
                active_agent,
                trace_id,
            )
            observation = _observe_run(request, active_agent, req, token)
            http_response.headers["X-Saber-Run-ID"] = observation.run_id
            call_kwargs = _cancel_kwargs(active_agent.process_with_options, token)
            if execution_context is None:
                response = await run_in_threadpool(
                    active_agent.process_with_options,
                    req.message,
                    opts,
                    **call_kwargs,
                )
            else:
                response = await run_in_threadpool(
                    active_agent.process_with_options,
                    req.message,
                    opts,
                    execution_context,
                    **call_kwargs,
                )
            _finish_online_rag_exposure(
                request,
                exposure,
                response=response,
                started=started,
            )
            payload = _response_to_dict(response)
            observation.finish(payload)
            return payload
        except HTTPException:
            if observation is not None:
                observation.fail()
            raise
        except Exception as e:
            if observation is not None:
                observation.fail()
            _finish_online_rag_exposure(
                request,
                exposure,
                response=response,
                started=started,
                error=e,
            )
            logger.error("聊天接口错误: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, request: Request):
        """SSE 流式：handler 只负责输出事件，真实 token 由 agent 内部 LLM 流式回调产生。"""

        opts = ChatOptions(use_rag=req.use_rag, conversation_id=req.conversation_id)

        active_agent = current_agent(request)
        # Resolve the experiment exposure before response headers are sent, so
        # duplicate/conflicting idempotency keys produce a real HTTP 409 rather
        # than failing inside an already-open SSE stream.
        trace_id = str(getattr(request.state, "request_id", "") or uuid.uuid4())
        exposure, execution_context = _begin_online_rag_exposure(
            request,
            req,
            active_agent,
            trace_id,
        )
        registry = getattr(active_agent, "_cancel_registry", None)
        if registry is not None:
            token, unregister = registry.register()
        else:
            token = CancelToken()
            unregister = lambda: None

        try:
            observation = _observe_run(request, active_agent, req, token, streaming=True)
        except Exception:
            unregister()
            raise

        async def _generate():
            stream_closed = threading.Event()
            events = None
            execution_started = False
            try:
                yield _sse("start", {"message": req.message, "run_id": observation.run_id})
                if hasattr(active_agent, "process_stream"):
                    events = queue.Queue(maxsize=512)
                    sentinel = object()
                    deferred_done: list[str] = []

                    def enqueue(item):
                        while not stream_closed.is_set():
                            try:
                                events.put(item, timeout=0.1)
                                return
                            except queue.Full:
                                continue

                    def _on_event(evt):
                        if not isinstance(evt, dict):
                            evt = _jsonable(evt)
                        event_type = str((evt or {}).get("type", "") or "")
                        data = (evt or {}).get("data") or {}
                        if event_type == "rag_result" and isinstance(data, dict):
                            data = {
                                **data,
                                "search_results": [
                                    _rag_result_to_main_contract(item)
                                    for item in (data.get("search_results") or [])
                                    if isinstance(item, dict)
                                ],
                            }
                        if event_type == "rag_trace":
                            data = redact_public_experiment_trace(
                                data,
                                experiment_active=bool(exposure),
                            )
                        if event_type == "done":
                            exposure_id_hint = ""
                            if isinstance(exposure, dict):
                                exposure_id_hint = str(
                                    exposure.get("opaque_exposure_id")
                                    or exposure.get("exposure_id")
                                    or exposure.get("id")
                                    or ""
                                )
                            data = _sanitize_stream_done(
                                data,
                                exposure_id_hint=exposure_id_hint,
                            )
                        if event_type:
                            observation.event(event_type, data)
                            rendered = _sse(event_type, data)
                            if event_type == "done":
                                # The opaque feedback handle is useful only
                                # after its exposure has a terminal ledger
                                # state.  Buffer the final event so a fast UI
                                # click cannot race ``finish_exposure``.
                                deferred_done.append(rendered)
                            else:
                                enqueue(rendered)

                    def _run_process_stream():
                        response = None
                        started = time.perf_counter()
                        # 真实 UnifiedAgent 支持复用 HTTP 层取消令牌（断连即取消）；
                        # 测试替身可能是窄签名，不支持时不强传。
                        stream_kwargs = (
                            {"cancel_token": token}
                            if "cancel_token"
                            in getattr(inspect.signature(active_agent.process_stream), "parameters", {})
                            else {}
                        )
                        try:
                            if execution_context is None:
                                response = active_agent.process_stream(
                                    req.message,
                                    opts,
                                    _on_event,
                                    **stream_kwargs,
                                )
                            else:
                                response = active_agent.process_stream(
                                    req.message,
                                    opts,
                                    _on_event,
                                    execution_context,
                                    **stream_kwargs,
                                )
                            _finish_online_rag_exposure(
                                request,
                                exposure,
                                response=response,
                                started=started,
                            )
                            observation.finish(_response_to_dict(response) if response is not None else {})
                            if deferred_done:
                                for rendered in deferred_done:
                                    enqueue(rendered)
                            elif response is not None:
                                enqueue(_sse("done", _response_to_dict(response)))
                        except Exception as e:
                            deferred_done.clear()
                            observation.fail()
                            _finish_online_rag_exposure(
                                request,
                                exposure,
                                response=response,
                                started=started,
                                error=e,
                            )
                            logger.error("流式聊天 process_stream 失败: %s", e)
                            enqueue(_sse("done", {"answer": f"请求失败: {e}", "interrupted": False, "success": False}))
                        finally:
                            enqueue(sentinel)

                    worker = threading.Thread(target=_run_process_stream, name="chat-stream", daemon=True)
                    execution_started = True
                    worker.start()
                    while True:
                        item = await asyncio.to_thread(events.get)
                        if item is sentinel:
                            break
                        yield item
                    return

                try:
                    execution_started = True
                    started = time.perf_counter()
                    if hasattr(active_agent, "_dispatch") and registry is not None:
                        if execution_context is None:
                            resp = active_agent._dispatch(req.message, opts, token)
                        else:
                            resp = active_agent._dispatch(
                                req.message,
                                opts,
                                token,
                                execution_context=execution_context,
                            )
                    else:
                        if execution_context is None:
                            resp = active_agent.process_with_options(req.message, opts)
                        else:
                            resp = active_agent.process_with_options(
                                req.message,
                                opts,
                                execution_context,
                            )
                    _finish_online_rag_exposure(
                        request,
                        exposure,
                        response=resp,
                        started=started,
                    )
                except Exception as e:
                    observation.fail()
                    _finish_online_rag_exposure(
                        request,
                        exposure,
                        response=None,
                        started=started,
                        error=e,
                    )
                    logger.error("流式聊天 _dispatch 失败: %s", e)
                    yield _sse("done", {"answer": f"请求失败: {e}", "interrupted": False, "success": False})
                    return

                data = _response_to_dict(resp)
                observation.finish(data)
                yield _sse("route", {"mode": resp.mode})
                if resp.extracted_info:
                    yield _sse("memory", {"extracted_info": resp.extracted_info})
                for step in resp.steps:
                    yield _sse(
                        "step",
                        {
                            "type": step.type,
                            "content": step.content,
                            "tool": step.tool,
                            "params": step.params,
                        },
                    )
                if resp.tool_call:
                    yield _sse("tool_call", resp.tool_call)
                if resp.search_results:
                    yield _sse("rag_result", {"search_results": data["search_results"]})

                answer_text = resp.answer or ""
                interrupted = bool(resp.interrupted)

                if answer_text and not interrupted:
                    # 逐 token（按字符）yield，体感为真流式。
                    # 注：当前 _dispatch 已生成完整 answer，本路由不再二次调
                    # llm.chat_stream_context 以避免与 stm/记忆写入重复。真流式
                    # LLM 接口 chat_stream_context 有独立单测覆盖，并按 queue+
                    # thread 范式接入，待 Task 25 多任务取消落地后切到本路由。
                    for ch in answer_text:
                        if token.is_cancelled():
                            break
                        yield _sse("token", {"content": ch})

                if token.is_cancelled():
                    data["interrupted"] = True

                yield _sse("done", data)
            finally:
                # 客户端断连时生成器在这里退出：先取消该请求的执行链
                # （LLM 流/图执行/工具循环都会检查该令牌），再关闭事件队列。
                try:
                    observation.cancel(before_execution=not execution_started)
                except Exception:
                    pass
                stream_closed.set()
                if events is not None:
                    # Wake any cancelled asyncio.to_thread(events.get) waiter.
                    try:
                        while True:
                            events.get_nowait()
                    except queue.Empty:
                        pass
                    events.put_nowait(sentinel)
                try:
                    unregister()
                except Exception:
                    pass

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Saber-Run-ID": observation.run_id},
        )
