"""Shared policy and durable write dispatch for the production Agent."""
from internal.harness.execution import execute_tool, ExecutionInterrupted
from internal.harness.plugins import HarnessContext
from internal.harness.journal import fingerprint
from internal.tools.tools import ToolError, ToolResult


def guarded_tool_attempt(agent, tool, tool_name, params, token, timeout, invocation_id):
    from internal.agent.graph_runtime import _call_tool_attempt
    ctx = HarnessContext(session_id=getattr(agent, "conversation_id", "") or "default",
        user_id=getattr(agent, "user_id", "default_user"),
        state={"invocation_id": invocation_id}, plugins=list(getattr(agent, "execution_plugins", [])))
    approval = getattr(agent, "human_approval", None)
    if approval is not None and getattr(tool, "requires_approval", False):
        with approval._lock:
            approval.dangerous_tools.add(tool_name)
    writes = getattr(tool, "side_effecting", False) or tool_name in {"exec_command", "write_document", "ingest_document"}
    journal = getattr(getattr(getattr(agent, "inf", None), "repo", None), "action_journal", None)
    key = "execution:" + fingerprint({"conversation": ctx.session_id, "invocation": invocation_id})
    digest = fingerprint({"tool": tool_name, "params": params})

    def invoke(approved_params):
        from internal.resilience.budget import charge
        charge("tool")
        if tool_name == "exec_command":
            approved_params["confirm"] = bool(ctx.state.get("human_approved"))
        if writes and journal is not None:
            if not journal.create(ctx.user_id, key, digest, "running", {}):
                prior = journal.get(ctx.user_id, key)
                if prior and prior["fingerprint"] == digest and prior["status"] == "completed":
                    payload = dict(prior["payload"])
                    error = payload.pop("error", None)
                    if error:
                        payload["error"] = ToolError(error["code"], error["message"], retryable=False)
                    return ToolResult(**payload)
                return ToolResult(False, error=ToolError("execution_uncertain", "该动作已经派发，禁止重复执行；请核对原结果", retryable=False))
        result = _call_tool_attempt(tool, approved_params, token, timeout)
        if writes:
            if result.error:
                result.error.retryable = False
            if not result.success:
                result.metadata["dispatch_state"] = "uncertain"
            if journal is not None:
                status = "completed" if result.success else "uncertain"
                journal.transition(ctx.user_id, key, "running", status, result.to_dict())
        return result
    try:
        result = execute_tool(ctx, tool_name, params, invoke)
        return result if isinstance(result, ToolResult) else ToolResult(False, error=ToolError("policy_rejected", str(result)))
    except ExecutionInterrupted as exc:
        return ToolResult(False, error=ToolError("approval_required", str(exc), retryable=False))
