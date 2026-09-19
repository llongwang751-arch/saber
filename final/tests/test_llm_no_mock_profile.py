from types import SimpleNamespace
import pytest
from internal.llm.llm import Client, Message


@pytest.mark.parametrize('real', [False, True])
def test_production_profile_never_returns_mock_after_missing_or_failed_provider(monkeypatch, real):
    monkeypatch.setenv('AGI_LLM_ALLOW_MOCK', '0')
    client = Client(SimpleNamespace(is_real_llm=lambda: real))
    monkeypatch.setattr(client, '_call_chat', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('provider unavailable')))
    with pytest.raises(RuntimeError, match='禁止用模拟回复替代'):
        client.chat([Message('user', 'hello')])


def test_development_mock_remains_explicitly_available(monkeypatch):
    monkeypatch.setenv('AGI_LLM_ALLOW_MOCK', '1')
    client = Client(SimpleNamespace(is_real_llm=lambda: False))
    assert '模拟 LLM 回复' in client.chat([Message('user', 'hello')])
