import pytest
from internal.graph.task_graph import Node, TaskGraph
from internal.harness import (
    DAGLoopPlugin,
    DirectChatLoopPlugin,
    EventType,
    HarnessRuntime,
    MemoryEventStream,
    ReActLoopPlugin,
    ResiliencePlugin,
    SqliteEventStream,
)


def test_harness_direct_chat_loop():
    runtime = HarnessRuntime.create_lightweight(llm_fn=lambda q: f"Answer to {q}")
    runtime.set_loop(DirectChatLoopPlugin())

    res = runtime.run("什么是深度学习？")
    assert res.status == "success"
    assert "Answer to 什么是深度学习？" in res.output
    # 验证事件流中包含完整生命周期事件
    event_types = [e.type for e in res.events]
    assert EventType.SESSION_START in event_types
    assert EventType.USER_INPUT in event_types
    assert EventType.REASONING in event_types
    assert EventType.SESSION_END in event_types


def test_harness_react_loop_with_tool_call():
    # 模拟一个会先调用工具然后给出 Final Answer 的 ReAct 模型
    def fake_llm(prompt: str) -> str:
        if "Past Observations: []" in prompt:
            return "Thought: 需要查询天气\nAction: get_weather(location=Beijing)"
        return "Thought: 已经获取到天气\nFinal Answer: 北京今天晴天，气温20度。"

    runtime = HarnessRuntime.create_lightweight(llm_fn=fake_llm)
    runtime.register_tool("get_weather", lambda params: "Sunny 20C")

    res = runtime.run("北京今天天气怎么样？")
    assert res.status == "success"
    assert res.output["status"] == "success"
    assert "北京今天晴天" in res.output["answer"]

    # 验证工具调用事件与结果事件被捕获
    tool_calls = [e for e in res.events if e.type == EventType.TOOL_CALL]
    tool_results = [e for e in res.events if e.type == EventType.TOOL_RESULT]
    checkpoints = [e for e in res.events if e.type == EventType.CHECKPOINT]

    assert len(tool_calls) == 1
    assert tool_calls[0].payload["tool"] == "get_weather"
    assert len(tool_results) == 1
    assert "Sunny 20C" in tool_results[0].payload["result"]
    assert len(checkpoints) >= 2


def test_harness_event_sourcing_replay():
    runtime = HarnessRuntime.create_lightweight(llm_fn=lambda q: "Final Answer: Done")
    res = runtime.run("测试回放功能")
    sid = res.session_id

    # 1. 全量回放
    replayed = runtime.replay(sid)
    assert len(replayed) == len(res.events)
    assert [e.event_id for e in replayed] == [e.event_id for e in res.events]

    # 2. 定向回放到指定事件点
    target_ev_id = res.events[1].event_id
    partial_replayed = runtime.replay(sid, stop_at_event_id=target_ev_id)
    assert len(partial_replayed) == 2
    assert partial_replayed[-1].event_id == target_ev_id


def test_harness_session_fork():
    runtime = HarnessRuntime.create_lightweight(llm_fn=lambda q: "Final Answer: Done")
    res = runtime.run("主会话任务")
    sid = res.session_id

    # 从第 2 个事件分叉出新会话
    fork_point = res.events[1].event_id
    forked_sid = runtime.fork(sid, fork_at_event_id=fork_point)

    forked_events = runtime.event_stream.get_events(forked_sid)
    assert len(forked_events) == 2
    assert all(e.session_id == forked_sid for e in forked_events)
    assert forked_sid != sid


def test_harness_crash_resume():
    # 模拟在第 1 步成功调用工具并记录 Checkpoint，随后意外中断
    stream = MemoryEventStream()
    runtime = HarnessRuntime(event_stream=stream)

    sid = "interrupted_session"
    # 注入一个历史中断事件序列
    from internal.harness.events import HarnessEvent
    stream.append(HarnessEvent(sid, "e1", EventType.SESSION_START, {"query": "中断任务"}))
    stream.append(HarnessEvent(sid, "e2", EventType.USER_INPUT, {"query": "中断任务"}))
    stream.append(HarnessEvent(sid, "e3", EventType.CHECKPOINT, {
        "step_index": 1,
        "observations": ["Step 1 [db_query]: Data recovered"],
        "status": "running",
    }))

    # 模拟 LLM 看到 Step 1 结果后给出 Final Answer
    def resume_llm(prompt: str) -> str:
        assert "Data recovered" in prompt
        return "Thought: 已有数据\nFinal Answer: 任务已从断点恢复完成。"

    runtime.llm_fn = resume_llm
    resumed_res = runtime.resume(sid)

    assert resumed_res.status == "resumed_success"
    assert "任务已从断点恢复完成" in resumed_res.output["answer"]

    # 验证产生 session_resumed 状态转移事件
    events = stream.get_events(sid)
    resumed_events = [e for e in events if e.type == EventType.STATE_CHANGE and e.payload.get("action") == "session_resumed"]
    assert len(resumed_events) == 1


def test_harness_dag_loop():
    graph = TaskGraph([
        Node(id="step1", tool_name="fetch_data"),
        Node(id="step2", tool_name="process_data", depends_on=["step1"]),
    ])

    runtime = HarnessRuntime.create_lightweight()
    runtime.set_loop(DAGLoopPlugin())
    runtime.register_tool("fetch_data", lambda p: {"raw": [1, 2, 3]})
    runtime.register_tool("process_data", lambda p: {"sum": 6})

    res = runtime.run("执行DAG任务", initial_state={"task_graph": graph})
    assert res.status == "success"
    assert graph.nodes["step1"].status.value == "done"
    assert graph.nodes["step2"].status.value == "done"
    assert res.output["results"]["step2"] == {"sum": 6}


def test_harness_enterprise_sqlite_persistence(tmp_path):
    db_file = str(tmp_path / "enterprise_harness.db")
    runtime = HarnessRuntime.create_enterprise(db_path=db_file, llm_fn=lambda q: "Final Answer: OK")

    res = runtime.run("企业级持久化事件流验证")
    assert res.status == "success"

    # 重新从 SQLite 实例化流并检查持久化数据
    new_stream = SqliteEventStream(db_path=db_file)
    events = new_stream.get_events(res.session_id)
    assert len(events) >= 3
    assert events[0].type == EventType.SESSION_START
