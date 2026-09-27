"""Agent scheduling and observation over an injected durable run repository."""

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass

from .cancel import CancelToken
from .contracts import ChatOptions
from .run_contracts import TERMINAL, RunConflict, RunNotFound, result_status

logger = logging.getLogger(__name__)


class _ResearchCancelToken(CancelToken):
    """Fence provider calls against the durable lease, not only a local heartbeat."""

    def __init__(self, service, owner_id, run_id):
        super().__init__()
        self.service, self.owner_id, self.run_id = service, owner_id, run_id

    def is_cancelled(self):
        if not super().is_cancelled():
            try:
                allowed = self.service._repository.execution_allowed(
                    self.owner_id, self.run_id, self.service._worker_id,
                )
            except Exception:
                # An unreadable lease must never authorize a new external call.
                allowed = False
            if not allowed:
                self.cancel()
        return super().is_cancelled()


class RunObservation:
    """Foreground execution with the same lifecycle as background work."""

    def __init__(self, service, owner_id, run_id, token):
        self.service, self.owner_id, self.run_id, self.token = service, owner_id, run_id, token

    def event(self, event_type, data):
        if event_type and event_type != "done":
            self.service.append_event(self.run_id, self.owner_id, event_type, data)

    def finish(self, response):
        status = result_status(response, self.token.is_cancelled())
        self.service._finish(self.run_id, self.owner_id, status, {"status": status, "response": response})
        with self.service._lock:
            self.service._tokens.pop(self.run_id, None)

    def fail(self):
        self.finish({"error": "agent_execution_failed", "success": False})

    def cancel(self, *, before_execution=False):
        self.service.cancel(self.owner_id, self.run_id)
        if before_execution:
            self.finish({"interrupted": True})


class NativeRunService:
    """Bounded workers sharing a transactional ledger with foreground requests.

    A lost worker lease fences submissions and cancels local execution;
    orphaned side-effecting tools are never automatically replayed.
    """

    def __init__(
        self, db_path, *, max_workers=4, max_active=64, heartbeat_interval=1.0, stale_after=10.0, repository=None,
        research_engine_factory=None,
    ):
        from .run_repository import SQLiteRunRepository

        self._lock = threading.RLock()
        self._research_engine_factory = research_engine_factory
        self._condition = threading.Condition()
        self._worker_id = str(uuid.uuid4())
        self._heartbeat_interval = max(0.05, heartbeat_interval)
        self._stale_after = max(self._heartbeat_interval * 3, stale_after)
        self._repository = (
            repository
            if repository is not None
            else SQLiteRunRepository(db_path, max_active=max(1, max_active), stale_after=self._stale_after)
        )
        self._repository.register_worker(self._worker_id)
        self._executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, 16)), thread_name_prefix="saber-run")
        self._tokens, self._futures = {}, {}
        self._closed = self._lease_lost = False
        self._last_heartbeat = time.monotonic()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="saber-run-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _notify(self):
        with self._condition:
            self._condition.notify_all()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(self._heartbeat_interval):
            try:
                with self._lock:
                    if self._closed:
                        return
                    alive, cancellations = self._repository.heartbeat(self._worker_id)
                    if not alive:
                        self._lease_lost = True
                        for token in self._tokens.values():
                            token.cancel()
                        return
                    self._last_heartbeat = time.monotonic()
                    for row in cancellations:
                        run_id = row["run_id"]
                        token = self._tokens.get(run_id)
                        if token is not None:
                            token.cancel()
                        future = self._futures.get(run_id)
                        if future is not None and future.cancel():
                            self._finish(run_id, row["owner_id"], "cancelled", {"status": "cancelled"})
                            self._tokens.pop(run_id, None)
                            self._futures.pop(run_id, None)
                self._notify()
            except Exception:
                logger.exception("Native run heartbeat failed")
                if time.monotonic() - self._last_heartbeat >= self._stale_after:
                    with self._lock:
                        self._lease_lost = True
                        for token in self._tokens.values():
                            token.cancel()
                    return

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._heartbeat_stop.set()
            for token in self._tokens.values():
                token.cancel()
            self._repository.release_worker(self._worker_id)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._heartbeat_thread.join(timeout=2)
        self._notify()

    def create(self, owner_id, agent, message, *, conversation_id="", use_rag=False, request_key="", mode="chat"):
        if mode not in {"chat", "research"}:
            raise ValueError("Unknown run mode")
        return self._create(
            owner_id,
            agent,
            message,
            conversation_id=conversation_id,
            use_rag=use_rag,
            request_key=request_key,
            kind=mode,
            task_id="",
        )

    def begin_inline(
        self, owner_id, agent, message, *, conversation_id="", use_rag=False, token: CancelToken, streaming=False
    ):
        run = self._create(
            owner_id,
            agent,
            message,
            conversation_id=conversation_id or "default",
            use_rag=use_rag,
            request_key="",
            kind="chat_stream" if streaming else "chat_sync",
            task_id="",
            enqueue=False,
            external_token=token,
        )
        return RunObservation(self, owner_id, run["run_id"], token)

    def create_recovery(self, owner_id, agent, task_id, *, conversation_id, request_key=""):
        self._validate_owner(owner_id, agent)
        if not task_id or len(task_id) > 128 or not conversation_id:
            raise ValueError("Task ID and conversation ID are required")
        if agent.inf.repo.snapshot.get(task_id, user_id=owner_id) is None:
            raise RunNotFound(task_id)
        return self._create(
            owner_id,
            agent,
            f"恢复任务 {task_id}",
            conversation_id=conversation_id,
            use_rag=False,
            request_key=request_key,
            kind="recovery",
            task_id=task_id,
        )

    def resume_research(self, owner_id, agent, run_id, *, request_key=""):
        self._validate_owner(owner_id, agent)
        run = self.get(owner_id, run_id)
        if run["kind"] != "research" or run["status"] != "interrupted" or run["plan_status"] != "approved":
            raise RunConflict("Only an interrupted, approved research run can be resumed")
        return self._create(
            owner_id, agent, run["message"], conversation_id=run["conversation_id"],
            use_rag=run["use_rag"], request_key=request_key, kind="research", task_id="",
            resume_from=run_id,
        )

    @staticmethod
    def _validate_owner(owner_id, agent):
        if not owner_id or owner_id != str(getattr(agent, "user_id", "")):
            raise ValueError("Agent owner does not match authenticated user")

    def _create(
        self,
        owner_id,
        agent,
        message,
        *,
        conversation_id,
        use_rag,
        request_key,
        kind,
        task_id,
        enqueue=True,
        external_token=None,
        resume_from="",
    ):
        self._validate_owner(owner_id, agent)
        if request_key and (len(request_key) > 128 or not all(c.isalnum() or c in "_-" for c in request_key)):
            raise ValueError("Invalid idempotency key")
        with self._lock:
            if self._closed or self._lease_lost:
                raise RunConflict("Run worker is shutting down or its lease expired")
            run, created = self._repository.reserve(
                owner_id, self._worker_id, message, conversation_id, use_rag, request_key, kind, task_id,
                resume_from=resume_from,
            )
            if not created:
                return run
            run_id = run["run_id"]
            token = external_token if external_token is not None else (
                _ResearchCancelToken(self, owner_id, run_id) if kind == "research" else CancelToken()
            )
            self._tokens[run_id] = token
            if not enqueue:
                if not self._repository.start(owner_id, run_id, worker_id=self._worker_id):
                    token.cancel()
                    self._tokens.pop(run_id, None)
                    raise RunConflict("Run was interrupted before execution")
                return self.get(owner_id, run_id)
            try:
                self._futures[run_id] = self._executor.submit(self._execute, owner_id, agent, run, token)
            except RuntimeError as exc:
                self._finish(run_id, owner_id, "failed", {"status": "failed", "reason": "worker_unavailable"})
                self._tokens.pop(run_id, None)
                raise RunConflict("Run worker is unavailable") from exc
        self._notify()
        return self.get(owner_id, run_id)

    def _execute(self, owner_id, agent, run, token):
        run_id = run["run_id"]
        try:
            if token.is_cancelled():
                self._finish(run_id, owner_id, "cancelled", {"status": "cancelled"})
                return
            if not self._repository.start(owner_id, run_id, worker_id=self._worker_id):
                return

            def on_event(event, data=None):
                if isinstance(event, str):
                    event = {"type": event, "data": data or {}}
                if isinstance(event, dict) and event.get("type") and event["type"] != "done":
                    self.append_event(run_id, owner_id, str(event["type"]), event.get("data") or {})

            if run["kind"] == "research":
                engine = self._research_engine(agent, run)
                if run.get("plan_status") != "approved":
                    plan = engine.plan(
                        run["message"], use_rag=run["use_rag"], cancel_token=token, on_event=on_event,
                    )
                    self._repository.pause_for_plan(owner_id, run_id, self._worker_id, plan)
                    self._notify()
                    return

                def checkpoint(state):
                    try:
                        self._repository.checkpoint_research(owner_id, run_id, self._worker_id, state)
                    except RunConflict as exc:
                        token.cancel()
                        raise InterruptedError("Research execution lease is no longer valid") from exc
                    self._notify()

                result = engine.execute(
                    run["plan"], run_id=run_id, conversation_id=run["conversation_id"],
                    use_rag=run["use_rag"], cancel_token=token, on_event=on_event,
                    checkpoint=checkpoint, state=run.get("research_state") or None,
                )
                response = result.to_dict() if hasattr(result, "to_dict") else dict(result)
            elif run["kind"] == "recovery":
                from .recovery import resume_task

                task = resume_task(agent, run["task_id"], run["conversation_id"], cancel_token=token, on_event=on_event)
                response = {
                    "answer": str(task.get("result") or ""),
                    "task": task,
                    "interrupted": task.get("status") == "interrupted",
                    "error": "recovery_failed" if task.get("status") == "failed" else None,
                }
            else:
                result = agent.process_stream(
                    run["message"],
                    ChatOptions(use_rag=run["use_rag"], conversation_id=run["conversation_id"]),
                    on_event,
                    cancel_token=token,
                )
                response = asdict(result) if is_dataclass(result) else dict(result)
            status = result_status(response, token.is_cancelled())
            self._finish(run_id, owner_id, status, {"status": status, "response": response})
        except Exception as exc:
            from .recovery import RecoveryConflict

            if isinstance(exc, RecoveryConflict):
                self._finish(
                    run_id,
                    owner_id,
                    "interrupted",
                    {"status": "interrupted", "reason": str(exc), "error_type": "RecoveryConflict"},
                )
            else:
                reason = "agent_execution_failed"
                if run["kind"] == "research":
                    from internal.observability.redaction import redact_text

                    reason = redact_text(str(exc), max_length=600) or type(exc).__name__
                    logger.error("Native research run %s failed (%s): %s", run_id, type(exc).__name__, reason)
                else:
                    logger.exception("Native run %s failed", run_id)
                status = "cancelled" if token.is_cancelled() else "failed"
                self._finish(
                    run_id,
                    owner_id,
                    status,
                    {"status": status, "reason": reason, "error_type": type(exc).__name__},
                )
        finally:
            with self._lock:
                # Approval can enqueue the next phase before this planning
                # future exits. Never remove the next phase's cancellation token.
                if self._tokens.get(run_id) is token:
                    self._tokens.pop(run_id, None)
                    self._futures.pop(run_id, None)

    def _research_engine(self, agent, run):
        """Dispatch research execution by ``research.engine``; native stays the default."""
        engine_name = str(getattr(getattr(agent, "cfg", None), "research_engine", "") or "native")
        if engine_name == "langgraph" and self._research_engine_factory is None:
            from internal.research_graph import LangGraphResearchEngine

            return LangGraphResearchEngine.from_agent(
                agent, run_id=run["run_id"], conversation_id=run["conversation_id"],
                parent_run_id=run.get("parent_run_id") or "",
            )
        if self._research_engine_factory is not None:
            return self._research_engine_factory(agent)
        from internal.research import ResearchEngine

        return ResearchEngine.from_agent(agent)

    def append_event(self, run_id, owner_id, event_type, data):
        event_id = self._repository.append_event(
            run_id, owner_id, event_type, data, worker_id=self._worker_id,
        )
        self._notify()
        return event_id

    def _finish(self, run_id, owner_id, status, payload):
        self._repository.finish(run_id, owner_id, status, payload, worker_id=self._worker_id)
        self._notify()

    def get_plan(self, owner_id, run_id):
        return self._repository.get_plan(owner_id, run_id)

    def review_plan(self, owner_id, agent, run_id, *, action, version, plan=None, steps=None):
        self._validate_owner(owner_id, agent)
        with self._lock:
            if self._closed or self._lease_lost:
                raise RunConflict("Run worker is shutting down or its lease expired")
            review = self._repository.review_plan(
                owner_id, run_id, action=action, version=version, worker_id=self._worker_id,
                plan=plan, steps=steps,
            )
            if action == "approve":
                run = self.get(owner_id, run_id)
                token = _ResearchCancelToken(self, owner_id, run_id)
                self._tokens[run_id] = token
                try:
                    self._futures[run_id] = self._executor.submit(self._execute, owner_id, agent, run, token)
                except RuntimeError as exc:
                    self._finish(run_id, owner_id, "interrupted", {
                        "status": "interrupted", "reason": "worker_unavailable_after_plan_approval",
                    })
                    self._tokens.pop(run_id, None)
                    raise RunConflict("Run worker is unavailable") from exc
        self._notify()
        return review

    def get(self, owner_id, run_id):
        return self._repository.get(owner_id, run_id)

    def healthy(self):
        with self._lock:
            return not self._closed and not self._lease_lost and self._repository.worker_healthy(self._worker_id)

    def list(self, owner_id, *, limit=50):
        return self._repository.list(owner_id, limit=limit)

    def summary(self, owner_id):
        return self._repository.summary(owner_id)

    def cancel_conversation(self, owner_id, conversation_id=""):
        for run_id in self._repository.active_ids(owner_id, conversation_id):
            self.cancel(owner_id, run_id)

    def events_since(self, owner_id, run_id, after_id=0, *, limit=200):
        return self._repository.events_since(owner_id, run_id, after_id, limit=limit)

    def wait_for_change(self, run_id, after_id, timeout=1.0):
        # Remote processes cannot notify this condition. Bound the polling
        # interval without retaining an unbounded per-run cursor cache.
        with self._condition:
            self._condition.wait(timeout=max(0.05, min(timeout, 1.0)))

    def cancel(self, owner_id, run_id):
        with self._lock:
            current = self.get(owner_id, run_id)
            if current["status"] in TERMINAL:
                return current
            self._repository.request_cancel(owner_id, run_id)
            token = self._tokens.get(run_id)
            if token is not None:
                token.cancel()
            future = self._futures.get(run_id)
            if future is not None and future.cancel():
                self._finish(run_id, owner_id, "cancelled", {"status": "cancelled"})
                self._tokens.pop(run_id, None)
                self._futures.pop(run_id, None)
        self._notify()
        return self.get(owner_id, run_id)
