"""Server-owned checkpoints and leased recovery of the production task graph."""
import copy
import threading
import time
import uuid
from contextlib import contextmanager

from internal.graph.task_graph import Node, NodeStatus, NodeType, TaskGraph
from internal.harness.journal import fingerprint


class RecoveryConflict(RuntimeError):
    pass


class TaskLease:
    def __init__(self, journal, user_id, task_id, ttl=90):
        self.journal, self.user_id = journal, user_id
        self.key = 'task-lease:' + task_id
        self.owner = uuid.uuid4().hex
        self.ttl = ttl
        self.stop = threading.Event()
        self.lost = threading.Event()

    def payload(self):
        return {'expires_at': time.time() + self.ttl}

    def acquire(self):
        if self.journal.create(self.user_id, self.key, fingerprint(self.key), self.owner, self.payload()):
            return
        prior = self.journal.get(self.user_id, self.key)
        if not prior or prior['payload'].get('expires_at', 0) > time.time():
            raise RecoveryConflict('任务仍在执行；进程异常退出后请等待租约到期')
        if not self.journal.claim_expired(self.user_id, self.key, prior['status'], self.owner, self.payload(), time.time()):
            raise RecoveryConflict('任务已由另一个请求接管')

    def refresh(self):
        if self.lost.is_set() or not self.journal.transition(
                self.user_id, self.key, self.owner, self.owner, self.payload()):
            self.lost.set()
            raise RecoveryConflict('任务执行租约已失效')

    def heartbeat(self):
        while not self.stop.wait(max(1, self.ttl / 3)):
            try:
                self.refresh()
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self.acquire()
        self.worker = threading.Thread(target=self.heartbeat, daemon=True, name='task-lease')
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.worker.join(timeout=2)
        self.journal.transition(self.user_id, self.key, self.owner, 'idle', {'expires_at': 0})


def restore_graph(task, agent):
    task = copy.deepcopy(task)
    saved = task.get('recovery') or {}
    session = getattr(agent, 'conversation_id', '') or 'default'
    if saved.get('schema') != 1:
        raise RecoveryConflict('旧快照缺少完整执行检查点，不能自动恢复')
    if saved.get('user_id') != agent.user_id or saved.get('conversation_id') != session:
        raise RecoveryConflict('检查点不属于当前用户或会话')
    nodes = []
    for data in saved['nodes']:
        data = dict(data)
        node = Node(**{k: v for k, v in data.items() if k in Node.__dataclass_fields__})
        node.type, node.status = NodeType(node.type), NodeStatus(node.status)
        if node.status in {NodeStatus.RUNNING, NodeStatus.FAILED}:
            binding = saved.get('dispatches', {}).get(node.id)
            ledger = None
            if binding:
                key = 'execution:' + fingerprint({'conversation': session, 'invocation': task['task_id'] + ':' + node.id})
                ledger = agent.inf.repo.action_journal.get(agent.user_id, key)
            if ledger and ledger['fingerprint'] == binding['fingerprint'] and ledger['status'] == 'completed':
                node.status = NodeStatus.DONE
                node.result = str(ledger['payload'].get('payload') or '')
                node.error = ''
            elif node.status == NodeStatus.RUNNING or (binding and (binding.get('uncertain') or binding.get('error_code') == 'execution_uncertain')):
                raise RecoveryConflict('动作结果不确定，禁止自动重放：' + node.id)
            elif binding and binding.get('error_code') == 'approval_required':
                node.status, node.error = NodeStatus.PENDING, ''
        nodes.append(node)
    if len({n.id for n in nodes}) != len(nodes) or len(nodes) > 256:
        raise RecoveryConflict('检查点执行图不合法')
    graph = TaskGraph(nodes)
    graph.validate()
    return task, graph


def resume_task(owner, task_id, conversation_id, *, cancel_token=None, on_event=None):
    """Only task ID and conversation ID are client inputs; no client graph/params."""
    from internal.agent.graph_runtime import GraphConfig, GraphRuntime
    from internal.harness.execution import start_session
    from internal.harness.plugins import HarnessContext
    from internal.resilience.budget import request_budget

    @contextmanager
    def scope():
        if conversation_id and conversation_id != 'default':
            with owner._conversations.lease(conversation_id) as agent:
                yield agent
        else:
            if not owner._turn_lock.acquire(blocking=False):
                raise RecoveryConflict('当前会话仍在执行')
            try:
                yield owner
            finally:
                owner._turn_lock.release()

    with scope() as agent:
        if agent.inf.repo.snapshot.get(task_id, user_id=agent.user_id) is None:
            raise KeyError('任务不存在')
        with TaskLease(agent.inf.repo.action_journal, agent.user_id, task_id) as lease:
            # Read after acquiring the lease: another resume may have finished.
            task = agent.inf.repo.snapshot.get(task_id, user_id=agent.user_id)
            if task is None:
                raise KeyError('任务不存在')
            task, graph = restore_graph(task, agent)
            if task.get('status') == 'completed':
                return task
            policy = HarnessContext(session_id=conversation_id or 'default', user_id=agent.user_id,
                                    query=task.get('query', ''), plugins=agent.execution_plugins)
            start_session(policy)
            if policy.interrupted:
                raise RecoveryConflict(policy.interrupted_reason)
            if cancel_token is None:
                token, unregister = agent._cancel_registry.register()
            else:
                token, unregister = cancel_token, lambda: None
            try:
                agent._cancel_registry.set_task(task)
                task.update(status='running', phase='resuming')
                runtime = GraphRuntime(graph, agent, GraphConfig(max_parallel=1, enable_racing=False),
                                       agent.tool_executor.snapshot(), task, on_event=on_event)
                runtime._lease = lease
                runtime.query = task.get('query', '')
                with request_budget(agent.cfg.max_llm_calls_per_turn, agent.cfg.max_tool_calls_per_turn):
                    result = runtime.execute(token)
                failed = any(n.status in {NodeStatus.FAILED, NodeStatus.CANCELLED, NodeStatus.SKIPPED} for n in graph.nodes.values())
                task['status'] = 'interrupted' if result.interrupted else ('failed' if failed else 'completed')
                task['phase'] = task['status']
                task['result'] = result.interrupted_msg if result.interrupted else '\n\n'.join(graph.successful_results())
                finish = getattr(agent.inf.repo.snapshot, 'finish_recovery', None)
                if task['status'] == 'completed' and callable(finish):
                    inserted = finish(task, agent.user_id, getattr(agent, 'conversation_id', '') or '')
                    if inserted and getattr(agent, 'stm', None) is not None:
                        agent.stm.add('assistant', task['result'])
                else:
                    agent.persist_task_checkpoint(task)
                return task
            finally:
                unregister()
