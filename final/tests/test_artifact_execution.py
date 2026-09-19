from types import SimpleNamespace
import pytest

from internal.agent.artifact import produce_artifact, decide_artifact, ArtifactExecutionBlocked
from internal.application.store import ApplicationStore
from internal.harness.journal import ActionJournal


def test_artifact_uses_policy_and_durable_dispatch(tmp_path, monkeypatch):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    user = store.create_user('u', 'hash')['id']
    agent = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False), user_id=user,
                            inf=SimpleNamespace(repo=SimpleNamespace(action_journal=ActionJournal(store))))
    calls = []
    monkeypatch.setattr('internal.agent.artifact._materialize', lambda *a: (calls.append(1) or True, 'test'))
    class Stop:
        name = 'stop'
        def on_tool_execute(self, ctx, name, params):
            ctx.interrupted, ctx.interrupted_reason = True, 'blocked'
            return False, None
    agent.execution_plugins = [Stop()]
    with pytest.raises(ArtifactExecutionBlocked):
        produce_artifact(agent, '生成一份报告', 'content', str(tmp_path))
    assert calls == []
    agent.execution_plugins = []
    first = produce_artifact(agent, '生成一份报告', 'content', str(tmp_path))
    assert produce_artifact(agent, '生成一份报告', 'content', str(tmp_path)) == first
    assert calls == [1]
    with pytest.raises(ArtifactExecutionBlocked):
        produce_artifact(agent, '生成一份报告', 'changed arguments', str(tmp_path))
    assert calls == [1]
    store.close()


def test_artifact_negative_request_and_false_string_cannot_trigger_write():
    agent = SimpleNamespace(cfg=SimpleNamespace(is_real_llm=lambda: False))
    assert decide_artifact(agent, '只报告计算值，不要保存或写入文档') == (False, '')
    agent.cfg.is_real_llm = lambda: True
    agent.llm = SimpleNamespace(chat=lambda *a, **k: '{"needs_file":"false"}')
    assert decide_artifact(agent, '普通问答') == (False, '')
