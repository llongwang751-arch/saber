from concurrent.futures import ThreadPoolExecutor

import pytest

from internal.graph.task_graph import Node, TaskGraph
from internal.harness import DAGLoopPlugin, HarnessRuntime, SqliteEventStream
from internal.harness.plugins import HarnessPlugin


def test_reopen_resumes_pending_nodes_without_replaying_completed_nodes(tmp_path):
    path = str(tmp_path / "events.db")
    calls = []
    class Pause(HarnessPlugin):
        def on_tool_execute(self, ctx, name, params):
            if name == "second":
                ctx.interrupted = True
                ctx.interrupted_reason = "paused before dispatch"
            return False, None
    first = HarnessRuntime(event_stream=SqliteEventStream(path))
    first.set_loop(DAGLoopPlugin())
    first.register_plugin(Pause())
    first.register_tool("first", lambda _: calls.append("a") or "a-result")
    first.register_tool("second", lambda _: calls.append("b") or "b-result")
    graph = TaskGraph([Node("a", tool_name="first"), Node("b", tool_name="second", depends_on=["a"])])
    assert first.run("task", session_id="dag", user_id="alice", initial_state={"task_graph": graph}).interrupted
    assert calls == ["a"]
    # A new runtime uses the stored graph, not the caller's old Python objects.
    second = HarnessRuntime(event_stream=SqliteEventStream(path))
    second.register_tool("first", lambda _: pytest.fail("completed action replayed"))
    second.register_tool("second", lambda _: calls.append("b") or "b-result")
    result = second.resume("dag")
    assert result.status == "resumed_success"
    assert calls == ["a", "b"]
    assert result.output["results"] == {"a": "a-result", "b": "b-result"}
    assert second.resume("dag").output == result.output
    assert calls == ["a", "b"]


def test_process_death_after_side_effect_blocks_automatic_replay(tmp_path):
    path = str(tmp_path / "events.db")
    calls = []
    def crash(_):
        calls.append("external-write")
        raise SystemExit("simulated process death")
    runtime = HarnessRuntime(event_stream=SqliteEventStream(path))
    runtime.set_loop(DAGLoopPlugin())
    runtime.register_tool("write", crash)
    with pytest.raises(SystemExit):
        runtime.run("task", session_id="crash", initial_state={"task_graph": TaskGraph([Node("w", tool_name="write")])})
    reopened = HarnessRuntime(event_stream=SqliteEventStream(path))
    reopened.register_tool("write", lambda _: calls.append("duplicate"))
    result = reopened.resume("crash")
    assert result.interrupted and "uncertain" in result.interrupted_reason
    assert calls == ["external-write"]


def test_sqlite_action_claim_is_atomic_across_store_instances(tmp_path):
    path = str(tmp_path / "events.db")
    stores = [SqliteEventStream(path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = list(pool.map(lambda s: s.claim_action("session", "write"), stores))
    assert sum(winners) == 1
    assert SqliteEventStream(path).claim_action("session", "write") is False
    assert stores[0].claim_action("another-session", "write") is True


@pytest.mark.parametrize("loop", ["dag", "react"])
def test_actual_child_process_exit_does_not_duplicate_external_write(tmp_path, loop):
    import subprocess
    import sys
    from pathlib import Path
    db = tmp_path / "crash.db"
    effect = tmp_path / "external.txt"
    script = tmp_path / "crash_worker.py"
    root = Path(__file__).resolve().parents[1]
    script.write_text(f'''
import os, sys
from pathlib import Path
sys.path.insert(0, {str(root)!r})
from internal.harness import HarnessRuntime, SqliteEventStream, DAGLoopPlugin
from internal.graph.task_graph import Node, TaskGraph
runtime = HarnessRuntime(event_stream=SqliteEventStream({str(db)!r}), llm_fn=lambda _: 'Action: write({{}})')
def write(_):
    Path({str(effect)!r}).write_text('once', encoding='utf8')
    os._exit(23)
runtime.register_tool('write', write)
if {loop!r} == 'dag':
    runtime.set_loop(DAGLoopPlugin())
runtime.run('task', session_id='process-crash', initial_state={{'task_graph': TaskGraph([Node('w', tool_name='write')])}})
''', encoding="utf8")
    process = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=30)
    assert process.returncode == 23, process.stderr.decode(errors="replace")
    reopened = HarnessRuntime(event_stream=SqliteEventStream(str(db)))
    reopened.register_tool("write", lambda _: effect.write_text("duplicate", encoding="utf8"))
    assert reopened.resume("process-crash").interrupted
    assert effect.read_text(encoding="utf8") == "once"
