import pytest
from sqlalchemy import select

from internal.application.store import ApplicationStore
from internal.application.local_repos import LocalLongTermRepo, LocalPreferenceRepo
from internal.application.models import MemoryOutboxRecord


def test_fact_preference_events_roll_back_together_and_old_extraction_cannot_overwrite(tmp_path, monkeypatch):
    import internal.application.local_repos as repos
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    user = store.create_user('user', 'hash')['id']
    ltm, prefs = LocalLongTermRepo(store), LocalPreferenceRepo(store)
    old_order = ltm.begin_user_message(user)
    first = ltm.commit_user_fact(user, '城市', '北京', order=old_order)
    new_order = ltm.begin_user_message(user)
    original = repos._projection_events
    def fail(*args):
        original(*args)
        raise RuntimeError('outbox failure')
    monkeypatch.setattr(repos, '_projection_events', fail)
    with pytest.raises(RuntimeError):
        ltm.commit_user_fact(user, '居住地', '上海', order=new_order)
    assert prefs.load(user) == {'居住地': '北京'}
    assert ltm.load(user)[0].content == '用户居住地: 北京'
    monkeypatch.setattr(repos, '_projection_events', original)
    committed = ltm.commit_user_fact(user, '居住地', '上海', order=new_order, priority=1)
    assert committed.memory_id == first.memory_id
    assert ltm.commit_user_fact(user, '居住地', '北京', order=old_order) is None
    assert ltm.commit_user_fact(user, '居住地', '错误抽取', order=new_order) is None
    assert prefs.load(user) == {'居住地': '上海'}
    assert len(ltm.load(user)) == 1
    with store.transaction() as session:
        assert len(list(session.scalars(select(MemoryOutboxRecord)))) == 6
    store.close()


def test_negation_removes_contradictory_preference_and_preserves_other_fact(tmp_path):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    user = store.create_user('user', 'hash')['id']
    repo = LocalLongTermRepo(store)
    repo.commit_user_fact(user, '喜好', '咖啡', order=repo.begin_user_message(user))
    repo.commit_user_fact(user, '忌口与偏好限制', '避免/不喜欢: 咖啡', order=repo.begin_user_message(user))
    prefs = LocalPreferenceRepo(store).load(user)
    assert '喜好' not in prefs
    assert len(repo.load(user)) == 1
    store.close()


def test_vector_enrichment_cannot_weaken_deterministic_fact(tmp_path):
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'app.db'))
    user = store.create_user('user', 'hash')['id']
    repo = LocalLongTermRepo(store)
    order = repo.begin_user_message(user)
    first = repo.commit_user_fact(user, '城市', '上海', order=order, priority=1)
    enriched = repo.commit_user_fact(user, '城市', '上海', order=order, embedding=[1., 0.])
    assert enriched.memory_id == first.memory_id and enriched.embedding == [1., 0.]
    assert repo.commit_user_fact(user, '城市', '北京', order=order, embedding=[0., 1.]) is None
    assert LocalPreferenceRepo(store).load(user) == {'居住地': '上海'}
    store.close()


def test_legacy_alias_is_superseded_in_same_correction_transaction(tmp_path):
    import hashlib
    store = ApplicationStore('sqlite:///' + str(tmp_path / 'alias.db'))
    user = store.create_user('user', 'hash')['id']
    repo = LocalLongTermRepo(store)
    old = repo.create_committed('用户称呼: 旧称呼', .7, [], user_id=user,
                               tags=['factkey:' + hashlib.sha256('称呼'.encode()).hexdigest()])
    LocalPreferenceRepo(store).save(user, '称呼', '旧称呼')
    new = repo.commit_user_fact(user, '姓名', '新称呼', order=repo.begin_user_message(user), priority=1)
    assert LocalPreferenceRepo(store).load(user) == {'姓名': '新称呼'}
    from internal.application.models import AgentLongTermMemoryRecord
    with store.transaction() as session:
        assert session.get(AgentLongTermMemoryRecord, old.memory_id).status == 'superseded'
        assert session.get(AgentLongTermMemoryRecord, new.memory_id).status == 'active'
    assert repo.commit_user_fact(user, '称呼', '旧称呼', order=1) is None
    store.close()
