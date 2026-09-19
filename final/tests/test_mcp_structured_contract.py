import threading
import time
from types import SimpleNamespace

import requests
import pytest

from internal.agent.cancel import CancelToken
from internal.agent.graph_runtime import GraphConfig, GraphRuntime
from internal.graph.task_graph import Node, NodeStatus, TaskGraph
from internal.tools.tools import Tool, ToolCallContext, ToolResult, new_mcp_tool


class _Response:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _Agent:
    def __init__(self, *, max_retries: int = 3, step_timeout_ms: int = 0):
        self.cfg = SimpleNamespace(
            max_retries=max_retries,
            retry_delay_ms=0,
            step_timeout_ms=step_timeout_ms,
        )


def _run(tool, *, token=None, max_retries=3, step_timeout_ms=0, events=None):
    # This fixture models an explicitly read-only remote lookup.
    tool.side_effecting = False
    graph = TaskGraph([Node(id="n1", tool_name=tool.name, params={"q": "hi"})])
    result = GraphRuntime(
        graph,
        _Agent(max_retries=max_retries, step_timeout_ms=step_timeout_ms),
        GraphConfig(max_parallel=1),
        {tool.name: tool},
        on_event=(events.append if events is not None else None),
    ).execute(token or CancelToken())
    return graph, result


def test_mcp_success_preserves_payload_json_status_metadata_and_duration(monkeypatch):
    monkeypatch.setattr(
        "internal.tools.tools.requests.post",
        lambda _url, json, timeout: _Response(200, '{"label":"ok","echo":"hi"}'),
    )
    tool = new_mcp_tool("echo", "", [], endpoint="https://mcp.example/echo")

    result = tool.execute_structured(ToolCallContext(), {"q": "hi"})

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.payload == '{"label":"ok","echo":"hi"}'
    assert result.payload_json == {"label": "ok", "echo": "hi"}
    assert result.metadata == {
        "backend": "mcp",
        "endpoint": "https://mcp.example/echo",
        "status_code": "200",
    }
    assert result.duration > 0
    assert result.error is None


def test_mcp_4xx_is_not_retried_and_graph_keeps_body_and_evidence(monkeypatch):
    calls = []

    def post(_url, json, timeout):
        calls.append((json, timeout))
        return _Response(422, '{"error":"bad param"}')

    monkeypatch.setattr("internal.tools.tools.requests.post", post)
    events = []
    graph, result = _run(
        new_mcp_tool("bad", "", [], endpoint="https://mcp.example/bad"),
        max_retries=4,
        events=events,
    )

    node = result.node_results["n1"]
    assert len(calls) == 1
    assert graph.nodes["n1"].status == NodeStatus.FAILED
    assert graph.nodes["n1"].result == '{"error":"bad param"}'
    assert node.error_code == "http_4xx"
    assert node.retryable is False
    assert node.metadata["status_code"] == "422"
    assert node.duration > 0
    event = next(e["data"] for e in events if e["type"] == "tool_result")
    assert event["error_detail"]["code"] == "http_4xx"
    assert event["metadata"]["status_code"] == "422"


def test_mcp_5xx_retries_with_fresh_attempt_and_then_succeeds(monkeypatch):
    responses = [
        _Response(503, '{"error":"busy"}'),
        _Response(200, '{"ok":true,"answer":"ready"}'),
    ]

    def post(_url, json, timeout):
        return responses.pop(0)

    monkeypatch.setattr("internal.tools.tools.requests.post", post)
    events = []
    graph, result = _run(
        new_mcp_tool("flaky", "", [], endpoint="https://mcp.example/flaky"),
        max_retries=3,
        events=events,
    )

    node = result.node_results["n1"]
    assert responses == []
    assert graph.nodes["n1"].status == NodeStatus.DONE
    assert graph.nodes["n1"].retry_count == 1
    assert node.result == '{"ok":true,"answer":"ready"}'
    assert node.payload_json == {"ok": True, "answer": "ready"}
    assert node.error_code == ""
    assert node.metadata["status_code"] == "200"
    assert [attempt["metadata"]["status_code"] for attempt in node.attempts] == [
        "503", "200",
    ]
    assert node.attempts[0]["payload"] == '{"error":"busy"}'
    retry = next(e["data"] for e in events if e["type"] == "tool_retry")
    assert retry["metadata"]["status_code"] == "503"
    assert retry["error_detail"] == {
        "code": "http_5xx", "message": "MCP 返回 503", "retryable": True,
    }


def test_mcp_network_and_timeout_errors_are_retryable(monkeypatch):
    tool = new_mcp_tool("remote", "", [], endpoint="https://mcp.example/tool")

    monkeypatch.setattr(
        "internal.tools.tools.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )
    network = tool.execute_structured(ToolCallContext(), {})
    assert network.error.code == "network"
    assert network.error.retryable is True

    monkeypatch.setattr(
        "internal.tools.tools.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout("slow")),
    )
    timeout = tool.execute_structured(ToolCallContext(), {})
    assert timeout.error.code == "timeout"
    assert timeout.error.retryable is True


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (requests.ConnectionError("down"), "network"),
        (requests.Timeout("slow"), "timeout"),
    ],
)
def test_graph_runtime_retries_mcp_network_and_timeout_then_recovers(
    monkeypatch, failure, expected_code
):
    calls = []

    def post(_url, json, timeout):
        calls.append(True)
        if len(calls) == 1:
            raise failure
        return _Response(200, '{"ok":true,"answer":"recovered"}')

    monkeypatch.setattr("internal.tools.tools.requests.post", post)
    graph, result = _run(
        new_mcp_tool("recover", "", [], endpoint="https://mcp.example/recover"),
        max_retries=3,
    )

    node = result.node_results["n1"]
    assert len(calls) == 2
    assert graph.nodes["n1"].status == NodeStatus.DONE
    assert graph.nodes["n1"].retry_count == 1
    assert node.attempts[0]["error"]["code"] == expected_code
    assert node.attempts[0]["error"]["retryable"] is True


def test_mcp_non_json_serializable_parameter_is_non_retryable(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "internal.tools.tools.requests.post",
        lambda *_args, **_kwargs: calls.append(True),
    )
    tool = new_mcp_tool("bad-param", "", [], endpoint="https://mcp.example/tool")

    result = tool.execute_structured(ToolCallContext(), {"bad": object()})

    assert result.success is False
    assert result.error.code == "param"
    assert result.error.retryable is False
    assert calls == []


def test_graph_step_deadline_is_passed_to_mcp_http_client(monkeypatch):
    observed_timeouts = []

    def post(_url, json, timeout):
        observed_timeouts.append(timeout)
        return _Response(200, '{"ok":true}')

    monkeypatch.setattr("internal.tools.tools.requests.post", post)
    graph, _result = _run(
        new_mcp_tool("bounded", "", [], endpoint="https://mcp.example/bounded"),
        max_retries=1,
        step_timeout_ms=80,
    )

    assert graph.nodes["n1"].status == NodeStatus.DONE
    assert len(observed_timeouts) == 1
    assert 0 < observed_timeouts[0] <= 0.08


def test_graph_runtime_value_error_is_not_blindly_retried():
    calls = []

    def invalid(_params):
        calls.append(True)
        raise ValueError("invalid argument")

    tool = Tool("invalid", "", [], invalid)
    graph, result = _run(tool, max_retries=5)

    assert len(calls) == 1
    assert graph.nodes["n1"].status == NodeStatus.FAILED
    assert result.node_results["n1"].error_code == "param"
    assert result.node_results["n1"].retryable is False


def test_graph_runtime_interrupted_sentinel_is_cancelled_and_not_retried():
    calls = []
    tool = Tool(
        "interrupting", "", [],
        lambda _params: calls.append(True) or "[已中断]",
    )

    graph, result = _run(tool, max_retries=5)

    assert len(calls) == 1
    assert graph.nodes["n1"].status == NodeStatus.CANCELLED
    assert result.node_results["n1"].error_code == "cancelled"
    assert result.node_results["n1"].retryable is False


def test_graph_runtime_cooperatively_stops_waiting_on_cancel_without_claiming_thread_kill():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocking(_params):
        calls.append(True)
        started.set()
        release.wait(1.0)
        return "late"

    token = CancelToken()
    tool = new_mcp_tool("blocking", "", [], func=blocking)
    holder = {}

    def execute():
        holder["value"] = _run(tool, token=token, max_retries=4)

    worker = threading.Thread(target=execute)
    before = time.perf_counter()
    worker.start()
    assert started.wait(0.5)
    token.cancel()
    worker.join(0.5)
    elapsed = time.perf_counter() - before
    release.set()

    assert worker.is_alive() is False
    graph, result = holder["value"]
    assert elapsed < 0.5
    assert len(calls) == 1
    assert graph.nodes["n1"].status == NodeStatus.CANCELLED
    assert result.interrupted is True
    assert result.node_results["n1"].error_code == "cancelled"
    assert result.node_results["n1"].retryable is False
