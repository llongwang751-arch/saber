"""One fail-closed tool interception boundary, shared by Graph/ReAct/DAG."""
from __future__ import annotations

from internal.harness.events import EventType


class ExecutionInterrupted(RuntimeError):
    pass


def execute_tool(ctx, tool_name, params, invoke):
    if ctx.interrupted:
        raise ExecutionInterrupted(ctx.interrupted_reason)
    # Plugins receive a per-call copy; caller-owned model arguments are immutable.
    params = dict(params)
    for plugin in ctx.plugins:
        try:
            intercepted, result = plugin.on_tool_execute(ctx, tool_name, params)
        except Exception as exc:
            ctx.interrupted = True
            ctx.interrupted_reason = f"Execution policy failed: {plugin.name}"
            raise ExecutionInterrupted(ctx.interrupted_reason) from exc
        if ctx.interrupted:
            raise ExecutionInterrupted(ctx.interrupted_reason)
        if intercepted:
            return result
    if ctx.state.get("durable_actions"):
        invocation_id = str(ctx.state.get("invocation_id") or "")
        if not invocation_id or ctx.event_stream is None:
            raise ExecutionInterrupted("Durable dispatch requires an invocation identity and event store")
        if not ctx.event_stream.claim_action(ctx.session_id, invocation_id):
            ctx.interrupted = True
            ctx.interrupted_reason = "Prior dispatch is uncertain; automatic replay refused"
            ctx.state["dispatch_uncertain"] = True
            raise ExecutionInterrupted(ctx.interrupted_reason)
    result = invoke(params)
    for plugin in reversed(ctx.plugins):
        result = plugin.on_tool_result(ctx, tool_name, result)
    return result


def start_session(ctx):
    for plugin in ctx.plugins:
        try:
            plugin.on_session_start(ctx)
        except Exception as exc:
            ctx.interrupted = True
            ctx.interrupted_reason = f"Session policy failed: {plugin.name}"
            ctx.emit(EventType.ERROR, {"error": ctx.interrupted_reason})
        if ctx.interrupted:
            break


def finish_session(ctx, output):
    for plugin in reversed(ctx.plugins):
        plugin.on_session_end(ctx, output)
    if isinstance(output, str):
        output = ctx.state.get("sanitized_output", output)
    return output
