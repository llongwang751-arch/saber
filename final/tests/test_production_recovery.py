import json
import threading
from types import SimpleNamespace

import pytest

from internal.agent.cancel import CancelRegistry, CancelToken
from internal.agent.graph_runtime import GraphRuntime, GraphConfig
from internal.agent.recovery import TaskLease, RecoveryConflict, restore_graph, resume_task
from internal.application.store import ApplicationStore
from internal.application.local_repos import LocalSnapshotRepo
from internal.graph.task_graph import Node, NodeStatus, TaskGraph
from internal.harness.journal import ActionJournal, fingerprint


def make_agent(store, user, tools):
    repo = SimpleNamespace(snapshot=LocalSnapshotRepo(store), action_journal=ActionJournal(store))
    agent = SimpleNamespace(user_id=user, conversation_id='', inf=SimpleNamespace(repo=repo),
        cfg=SimpleNamespace(max_retries=1, retry_delay_ms=0, step_timeout_ms=1000,
                            max_llm_calls_per_turn=4, max_tool_calls_per_turn=4),
        _turn_lock=threading.Lock(), _cancel_registry=CancelRegistry(), execution_plugins=[],
        tool_executor=SimpleNamespace(snapshot=lambda: tools))
    agent.persist_task_checkpoint = lambda task: repo.snapshot.save(task['task_id'], json.dumps(task), user_id=user)
    return agent


def tool(fn, writes=False):
    return SimpleNamespace(func=fn, side_effecting=writes, description='', params=[])


def test_resume_keeps_done_results_isolated_and_leased(tmp_path):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    user = store.create_user('u', 'hash')['id']
    calls = []
    tools = {'first': tool(lambda _: calls.append('first') or 'A'),
             'second': tool(lambda p: calls.append(p['input']) or 'B')}
    agent = make_agent(store, user, tools)
    graph = TaskGraph([Node('one', tool_name='first', status=NodeStatus.DONE, result='A'),
                       Node('two', tool_name='second', params={'input': '{{input}}'}, depends_on=['one'])])
    runtime = GraphRuntime(graph, agent, GraphConfig(), tools, {'task_id': 't', 'query': 'run'})
    runtime._save_snapshot()
    result = resume_task(agent, 't', 'default')
    assert result['status'] == 'completed' and calls == ['## one\n\nA']
    assert resume_task(agent, 't', 'default')['result'] == result['result']
    assert len(calls) == 1
    from internal.application.local_repos import LocalChatHistoryRepo
    assert len(LocalChatHistoryRepo(store).load(10, user_id=user)) == 1
    other = store.create_user('v', 'hash')['id']
    assert agent.inf.repo.snapshot.get('t', user_id=other) is None
    with pytest.raises(RecoveryConflict):
        restore_graph(result, SimpleNamespace(user_id=user, conversation_id='other'))
    with TaskLease(agent.inf.repo.action_journal, user, 'locked'):
        with pytest.raises(RecoveryConflict):
            TaskLease(agent.inf.repo.action_journal, user, 'locked').acquire()
    store.close()


def test_checkpoint_failure_prevents_dispatch(tmp_path):
    calls = []
    agent = SimpleNamespace(user_id='u', persist_task_checkpoint=lambda _: (_ for _ in ()).throw(OSError('disk full')))
    graph = TaskGraph([Node('one', tool_name='write')])
    result = GraphRuntime(graph, agent, GraphConfig(), {'write': tool(lambda _: calls.append(1), True)},
                          {'task_id': 't'}).execute(CancelToken())
    assert result.interrupted and calls == []


def test_uncertain_action_is_blocked_but_committed_result_is_recovered(tmp_path):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    user = store.create_user('u', 'hash')['id']
    agent = make_agent(store, user, {})
    node = Node('one', tool_name='write', status=NodeStatus.RUNNING)
    task = {'task_id': 't'}
    runtime = GraphRuntime(TaskGraph([node]), agent, GraphConfig(), {}, task)
    digest = fingerprint({'tool': 'write', 'params': {}})
    runtime._dispatches['one'] = {'fingerprint': digest}
    runtime._save_snapshot()
    with pytest.raises(RecoveryConflict):
        restore_graph(task, agent)
    key = 'execution:' + fingerprint({'conversation': 'default', 'invocation': 't:one'})
    agent.inf.repo.action_journal.create(user, key, digest, 'completed', {'payload': 'already wrote'})
    _, restored = restore_graph(task, agent)
    assert restored.nodes['one'].status == NodeStatus.DONE
    assert restored.nodes['one'].result == 'already wrote'
    store.close()


def test_heartbeat_renewal_wins_against_stale_takeover(tmp_path):
    import time
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    user = store.create_user('u', 'hash')['id']
    journal = ActionJournal(store)
    lease = TaskLease(journal, user, 't')
    lease.acquire()
    assert not journal.claim_expired(user, lease.key, lease.owner, 'other', {'expires_at': 0}, time.time())
    assert journal.get(user, lease.key)['status'] == lease.owner
    store.close()


@pytest.mark.parametrize('crash_after_commit', [True, False])
def test_real_process_crash_resumes_only_committed_safe_boundary(tmp_path, crash_after_commit):
    import os
    from pathlib import Path
    import subprocess
    import sys
    db = tmp_path / 'app.db'
    marker = tmp_path / 'external-write.txt'
    store = ApplicationStore('sqlite:///' + str(db))
    user = store.create_user('u', 'hash')['id']
    store.close()

    # A real process exit inside the production graph worker, not an exception.
    script = '''
import os, sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_production_recovery import *
store = ApplicationStore('sqlite:///' + sys.argv[1])
def write(_):
    Path(sys.argv[3]).write_text('written', encoding='utf8')
    if sys.argv[4] == 'False': os._exit(23)
    return 'committed'
agent = make_agent(store, sys.argv[2], {'write': tool(write, True), 'next': tool(lambda _: 'next')})
save = agent.persist_task_checkpoint
def checkpoint(task):
    save(task)
    if task['recovery']['nodes'][0]['status'] == 'done': os._exit(23)
agent.persist_task_checkpoint = checkpoint
graph = TaskGraph([Node('one', tool_name='write'), Node('two', tool_name='next', depends_on=['one'])])
GraphRuntime(graph, agent, GraphConfig(max_parallel=1), agent.tool_executor.snapshot(), {'task_id':'crash'}).execute(CancelToken())
'''
    child = subprocess.run([sys.executable, '-c', script, str(db), user, str(marker), str(crash_after_commit)],
                           cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, OPENBLAS_NUM_THREADS='1'),
                           capture_output=True, timeout=30)
    assert child.returncode == 23, child.stderr.decode(errors='replace')
    assert marker.read_text() == 'written'
    store = ApplicationStore('sqlite:///' + str(db))
    calls = []
    agent = make_agent(store, user, {'write': tool(lambda _: calls.append('write'), True),
                                   'next': tool(lambda _: calls.append('next') or 'next')})
    journal = agent.inf.repo.action_journal
    prior = journal.get(user, 'task-lease:crash')
    # Accelerate the dead process's lease expiry; never release a live owner.
    journal.transition(user, 'task-lease:crash', prior['status'], prior['status'], {'expires_at': 0})
    if crash_after_commit:
        assert resume_task(agent, 'crash', 'default')['status'] == 'completed'
        assert calls == ['next']
    else:
        with pytest.raises(RecoveryConflict, match='不确定'):
            resume_task(agent, 'crash', 'default')
        assert calls == []
    store.close()



def test_recovery_http_auth_tenant_boundary_and_client_graph_rejection(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import copy
    from config.config import APIConfig
    from fastapi.testclient import TestClient
    from internal.handler.handler import setup_routes
    monkeypatch.setenv('AGI_EVAL_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'http.db'))
    app = setup_routes(SimpleNamespace(), SimpleNamespace(), APIConfig(), auth_required=True)
    agents = {}
    def get(user):
        if user not in agents:
            agent = make_agent(app.state.application_store, user, {})
            @contextmanager
            def lease(conversation):
                scoped = copy.copy(agent)
                scoped.conversation_id = conversation
                yield scoped
            agent._conversations = SimpleNamespace(lease=lease)
            agents[user] = agent
        return agents[user]
    app.state.agent_registry = SimpleNamespace(get=get)
    with TestClient(app) as client:
        assert client.get('/api/tasks').status_code == 401
        users = [client.post('/api/auth/register', json={'username': name, 'password': 'strong-password'}).json()
                 for name in ('alice-recovery', 'bobby-recovery')]
        headers = [{'Authorization': 'Bearer ' + u['token']} for u in users]
        owner = get(users[0]['user_id'])
        GraphRuntime(TaskGraph([Node('done', status=NodeStatus.DONE, result='ok')]), owner,
                     GraphConfig(), {}, {'task_id': 'owned', 'status': 'interrupted'})._save_snapshot()
        assert client.get('/api/tasks', headers=headers[1]).json()['items'] == []
        assert client.post('/api/tasks/owned/resume', headers=headers[1], json={'conversation_id': 'default'}).status_code == 404
        assert client.post('/api/tasks/owned/resume', headers=headers[0], json={'conversation_id': 'wrong'}).status_code == 409
        assert client.post('/api/tasks/owned/resume', headers=headers[0], json={'conversation_id': 'default', 'nodes': []}).status_code == 422
        assert client.post('/api/tasks/owned/resume', headers=headers[0], json={'conversation_id': 'default'}).json()['status'] == 'completed'
