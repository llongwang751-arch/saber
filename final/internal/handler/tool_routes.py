"""Tool routes registered by the application composition root."""

import logging
import time

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from internal.application.api import current_agent
from internal.application.run_runtime import get_run_service

from .models import ApprovalDecision, MCPRegisterRequest, MCPServerDiscoverRequest

logger = logging.getLogger(__name__)


def register_tool_routes(app, agent, inf, cfg):
    @app.get("/api/tool-approvals")
    async def tool_approvals(request: Request):
        from dataclasses import asdict

        agent = current_agent(request)
        return [
            asdict(item)
            for item in agent.human_approval.requests_for(agent.user_id)
            if item.status.value == "pending" and item.expires_at > time.time()
        ]

    @app.post("/api/tool-approvals/{approval_id}/decision")
    async def decide_tool_approval(approval_id: str, decision: ApprovalDecision, request: Request):
        from internal.agent.tool_execution import guarded_tool_attempt
        from internal.agent.conversations import ConversationBusy

        agent = current_agent(request)
        owned = {item.request_id: item for item in agent.human_approval.requests_for(agent.user_id)}
        item = owned.get(approval_id)
        if item is None:
            raise HTTPException(404, "审批不存在")

        def run(scoped):
            tool = scoped.tool_executor.snapshot().get(item.tool_name)
            if tool is None:
                raise HTTPException(409, "工具已不可用，请重新发起请求")
            agent.human_approval.decide(approval_id, approved=decision.approved, operator=agent.user_id)
            if not decision.approved:
                return {"status": "rejected"}
            result = guarded_tool_attempt(
                scoped,
                tool,
                item.tool_name,
                dict(item.params),
                None,
                max(1.0, scoped.cfg.step_timeout_ms / 1000),
                item.invocation_id,
            )
            # Result is added to the same conversation; no model-generated substitute action.
            scoped.stm.add("assistant", result.payload or str(result.error or ""))
            scoped._save_chat_history("assistant", result.payload or str(result.error or ""))
            return {"status": "completed" if result.success else "failed", "result": result.to_dict()}

        def execute():
            if item.session_id != "default":
                with agent._conversations.lease(item.session_id) as scoped:
                    return run(scoped)
            with agent._turn_lock:
                return run(agent)

        try:
            return await run_in_threadpool(execute)
        except (ValueError, ConversationBusy) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/chat/cancel")
    async def chat_cancel(request: Request, conversation_id: str = ""):
        try:
            agent = current_agent(request)
            get_run_service(request).cancel_conversation(agent.user_id, conversation_id)
            if conversation_id:
                agent._conversations.cancel(conversation_id)
            else:
                agent.cancel()
            return {"ok": True, "message": "已发送取消信号"}
        except Exception as e:
            logger.error("取消失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tools/mcp")
    async def register_mcp_tool(req: MCPRegisterRequest, request: Request):
        try:
            active_agent = current_agent(request)
            name = req.name.strip()
            description = req.description.strip()
            endpoint = req.endpoint.strip()
            if not name or not endpoint:
                raise HTTPException(status_code=400, detail="缺少 name 或 endpoint 参数")

            active_agent.register_mcp_tool(name, description, req.params, endpoint=endpoint)
            return {"ok": True, "name": name}
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("注册 MCP 工具失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/tools/mcp/discover")
    async def discover_mcp_server(req: MCPServerDiscoverRequest, request: Request):
        """握手并发现 MCP 服务器的工具清单，批量注册进当前用户的工具箱。

        与 /api/tools/mcp 的单工具裸 HTTP 注册互补：本端点走 MCP 协议
        （initialize → tools/list），远端工具以 mcp_ 前缀注册，执行走 tools/call。
        """
        from internal.tools.mcp_client import McpProtocolError

        try:
            active_agent = current_agent(request)
            endpoint = req.endpoint.strip()
            if not endpoint:
                raise HTTPException(status_code=400, detail="缺少 endpoint 参数")
            return await run_in_threadpool(active_agent.register_mcp_server, endpoint)
        except HTTPException:
            raise
        except ValueError as e:
            # SSRF 校验失败等注册期校验错误。
            raise HTTPException(status_code=400, detail=str(e))
        except McpProtocolError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except Exception as e:
            logger.error("发现 MCP 服务器失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
