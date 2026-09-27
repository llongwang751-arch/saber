"""SSE 客户端断连 → 请求级取消的集成测试。

客户端中途放弃流时，HTTP 层的取消令牌必须穿透到 agent 执行链
（handler 在生成器 finally 中 cancel；cancel_token 由 process_stream 透传）。

TestClient 的 transport 会缓冲整个响应，无法模拟真实断连；
这里手工驱动 ASGI 协议，在首个响应块之后注入 ``http.disconnect``，
与 uvicorn 的断连行为对齐。
"""
import asyncio
import json
import threading
import time

from config.config import APIConfig
from internal.agent.agent import Response
from internal.agent.cancel import CancelRegistry
from internal.handler.handler import setup_routes


class _Infra:
    pass


class _CancellableAgent:
    """模拟慢执行：只在观察到取消令牌后才提前返回。

    signature 与真实 UnifiedAgent.process_stream 对齐（含 cancel_token），
    使 handler 会把 HTTP 层令牌透传进来。
    """

    def __init__(self):
        self._cancel_registry = CancelRegistry()
        self.cancel_observed = threading.Event()
        self.started = threading.Event()

    def process_stream(self, message, _opts, on_event, execution_context=None, cancel_token=None):
        self.started.set()
        on_event({"type": "route", "data": {"mode": "chat"}})
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if cancel_token is not None and cancel_token.is_cancelled():
                self.cancel_observed.set()
                break
            time.sleep(0.02)
        cancelled = bool(cancel_token is not None and cancel_token.is_cancelled())
        return Response(query=message, answer="done", mode="chat", interrupted=cancelled)


def _stream_scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/chat/stream",
        "raw_path": b"/api/chat/stream",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", b"0"),
        ],
    }


def _drive_with_disconnect(app, agent):
    """发出请求 → 收到首个响应块后注入 http.disconnect → 等待应用结束。"""

    async def _run():
        messages = []
        first_body_sent = asyncio.Event()
        request_sent = asyncio.Event()
        disconnect_sent = asyncio.Event()

        async def receive():
            if not request_sent.is_set():
                request_sent.set()
                body = json.dumps({"message": "慢慢答", "use_rag": False}).encode("utf-8")
                return {"type": "http.request", "body": body, "more_body": False}
            if not disconnect_sent.is_set():
                await first_body_sent.wait()
                # 留出时间让流式生成器阻塞在事件队列上，再模拟客户端断开。
                await asyncio.sleep(0.2)
                disconnect_sent.set()
                return {"type": "http.disconnect"}
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body":
                first_body_sent.set()

        await app(_stream_scope(), receive, send)
        return messages

    return asyncio.run(_run())


def test_client_disconnect_cancels_in_flight_stream():
    agent = _CancellableAgent()
    app = setup_routes(agent, _Infra(), APIConfig(), auth_required=False)

    messages = _drive_with_disconnect(app, agent)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    assert status == 200
    assert agent.started.wait(timeout=2)
    # 断连后 worker 线程应观察到取消令牌，而不是继续算满 10 秒。
    assert agent.cancel_observed.wait(timeout=5), (
        "客户端断连后 agent 未收到取消令牌，请求仍在后台空转"
    )
    service = app.state.native_run_service
    runs = service.list(agent.user_id)
    assert runs[0]["kind"] == "chat_stream"
    for _ in range(100):
        if service.get(agent.user_id, runs[0]["run_id"])["status"] == "cancelled":
            break
        time.sleep(.01)
    assert service.get(agent.user_id, runs[0]["run_id"])["status"] == "cancelled"
    service.close()
