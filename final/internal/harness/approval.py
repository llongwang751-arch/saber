"""Human-in-the-Loop (HITL) and Sensitive Action Approval Plugin.

Inspired by LangGraph's interrupt() and approval workflow, this plugin
intercepts dangerous or high-privilege tool calls, pauses the Harness runtime,
persists a checkpoint, and safely resumes upon human operator authorization.
"""

from __future__ import annotations

import logging
import time
import uuid
import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Set, Tuple

from internal.harness.events import EventType
from internal.harness.plugins import HarnessContext, HarnessPlugin

logger = logging.getLogger(__name__)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"


@dataclass
class ApprovalRequest:
    request_id: str
    session_id: str
    tool_name: str
    params: Dict[str, Any]
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    override_params: Optional[Dict[str, Any]] = None
    rejection_reason: str = ""
    user_id: str = "default_user"
    invocation_id: str = ""
    fingerprint: str = ""
    expires_at: float = 0.0


class HumanInTheLoopPlugin(HarnessPlugin):
    """Intercepts high-risk actions to require explicit human sign-off."""

    name = "human_in_the_loop"
    priority = 5  # High priority to intercept before actual tool execution

    DEFAULT_DANGEROUS_TOOLS: Set[str] = {
        "exec_command",
        "delete_file",
        "drop_table",
        "refund_payment",
        "modify_privilege",
        "send_external_email",
    }

    def __init__(self, dangerous_tools: Optional[Set[str]] = None, ttl_seconds: float = 600, journal=None):
        self.dangerous_tools = set(self.DEFAULT_DANGEROUS_TOOLS if dangerous_tools is None else dangerous_tools)
        self.pending_requests: Dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()
        self.ttl_seconds = ttl_seconds
        self.journal = journal

    def requests_for(self, user_id):
        with self._lock:
            if self.journal is not None:
                for payload in self.journal.list_approvals(user_id):
                    payload["status"] = ApprovalStatus(payload["status"])
                    req = ApprovalRequest(**payload)
                    self.pending_requests[req.request_id] = req
            return [req for req in self.pending_requests.values() if req.user_id == user_id]

    def on_tool_execute(
        self,
        ctx: HarnessContext,
        tool_name: str,
        params: Dict[str, Any],
    ) -> Tuple[bool, Optional[Any]]:
        with self._lock:
            return self._intercept(ctx, tool_name, params)

    def _intercept(self, ctx, tool_name, params):
        if tool_name not in self.dangerous_tools:
            return False, None
        fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        invocation_id = str(ctx.state.get("invocation_id") or ctx.state.get("step_index", ""))
        binding = json.dumps([ctx.user_id, ctx.session_id, invocation_id, tool_name, fingerprint])
        req_id = "appr_" + hashlib.sha256(binding.encode()).hexdigest()
        self.requests_for(ctx.user_id)
        for key, req in list(self.pending_requests.items()):
            if req.expires_at <= time.time():
                del self.pending_requests[key]
        if len(self.pending_requests) >= 256:
            raise RuntimeError("Too many approval requests")

        # Check if this exact tool invocation has already been approved
        existing_approved = None
        for req in self.pending_requests.values():
            if (
                req.session_id == ctx.session_id
                and req.user_id == ctx.user_id
                and req.invocation_id == invocation_id
                and req.fingerprint == fingerprint
                and req.expires_at > time.time()
                and req.tool_name == tool_name
                and req.status == ApprovalStatus.APPROVED
            ):
                existing_approved = req
                break

        if existing_approved:
            if self.journal is not None:
                payload = {**asdict(existing_approved), "status": "consumed"}
                if not self.journal.transition(ctx.user_id, "approval:" + existing_approved.request_id,
                                               "approved", "consumed", payload):
                    raise RuntimeError("Approval has already been consumed")
            existing_approved.status = ApprovalStatus.CONSUMED
            ctx.state["human_approved"] = True
            # Consumed approved grant
            logger.info(f"Executing human-approved sensitive tool '{tool_name}' for session {ctx.session_id}")
            if existing_approved.override_params:
                params.update(existing_approved.override_params)
            ctx.emit(EventType.STATE_CHANGE, {
                "action": "hitl_approved_execution",
                "request_id": existing_approved.request_id,
                "tool": tool_name,
            })
            return False, None

        # Intercept and pause runtime
        prior = self.pending_requests.get(req_id)
        if prior is None and self.journal is not None:
            saved = self.journal.get(ctx.user_id, "approval:" + req_id)
            if saved is not None:
                payload = dict(saved["payload"])
                payload["status"] = ApprovalStatus(payload["status"])
                prior = ApprovalRequest(**payload)
        if prior is not None:
            ctx.interrupted = True
            ctx.interrupted_reason = f"Approval {req_id}: {prior.status.value}; no duplicate dispatch"
            return True, {"status": prior.status.value, "request_id": req_id}
        req = ApprovalRequest(
            request_id=req_id,
            session_id=ctx.session_id,
            tool_name=tool_name,
            params=dict(params),
            user_id=ctx.user_id,
            invocation_id=invocation_id,
            fingerprint=fingerprint,
            expires_at=time.time() + self.ttl_seconds,
        )
        self.pending_requests[req_id] = req
        if self.journal is not None:
            self.journal.create(ctx.user_id, "approval:" + req_id, fingerprint, "pending", asdict(req))

        ctx.interrupted = True
        ctx.interrupted_reason = f"Action '{tool_name}' requires human approval (Request ID: {req_id})"

        ctx.emit(EventType.CHECKPOINT, {
            "action": "pending_human_approval",
            "request_id": req_id,
            "tool": tool_name,
            "params": params,
            "step_index": ctx.state.get("step_index", 0),
        })

        return True, {
            "status": "pending_approval",
            "request_id": req_id,
            "tool": tool_name,
            "message": f"Tool '{tool_name}' is paused awaiting human operator confirmation.",
        }

    def decide(
        self,
        request_id: str,
        approved: bool = True,
        override_params: Optional[Dict[str, Any]] = None,
        operator: str = "human_admin",
        reason: str = "",
    ) -> ApprovalRequest:
        """Process human decision for a pending action."""
        if request_id not in self.pending_requests:
            raise KeyError(f"Approval request '{request_id}' not found.")

        with self._lock:
            req = self.pending_requests[request_id]
            if req.status != ApprovalStatus.PENDING or req.expires_at <= time.time():
                raise ValueError("Approval is expired or already decided")
            new_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            payload = {**asdict(req), "status": new_status.value, "decided_at": time.time(),
                       "decided_by": operator, "override_params": dict(override_params) if override_params else None,
                       "rejection_reason": reason}
            if self.journal is not None and not self.journal.transition(req.user_id, "approval:" + request_id,
                    "pending", new_status.value, payload):
                raise ValueError("Approval has already been decided")
            req.status = new_status
            req.decided_at = time.time()
            req.decided_by = operator
            req.override_params = dict(override_params) if override_params else None
            req.rejection_reason = reason
            return req
