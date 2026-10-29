from datetime import datetime, timedelta, timezone

from whale_core.memory.curation import MemoryCurator
from whale_core.memory.continuity import remember_episode
from whale_core.memory.store import InMemoryStore
from whale_core.life.state import InnerStateStore

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def test_curator_requires_consent_and_evidence():
    cur = MemoryCurator(InMemoryStore())
    try:
        cur.remember("喜欢鲸鱼", source="user", evidence_id="chat:1")
    except PermissionError:
        pass
    else:
        assert False
    try:
        cur.remember("喜欢鲸鱼", source="", evidence_id="chat:1", consent=True)
    except ValueError:
        pass
    else:
        assert False


def test_curated_retrieval_is_explainable_and_raw_transcripts_excluded():
    store = InMemoryStore()
    remember_episode(store, summary="原始语音里说了隐私", key="ep:call:1")
    cur = MemoryCurator(store)
    fact = cur.remember("喜欢鲸鱼", source="direct_user", evidence_id="chat:3",
                        stable_fact=True, consent=True, key="pref:fish", now=NOW)
    cur.remember("明天上课", source="calendar", evidence_id="evt:1",
                 importance=0.8, consent=True, now=NOW-timedelta(days=60))
    hits = cur.recall("喜欢鲸鱼", now=NOW)
    assert [hit.record.key for hit in hits] == ["pref:fish"]
    assert "证据:chat:3" in hits[0].reasons
    assert not cur.recall("隐私", now=NOW)
    assert cur.forget(fact.key)
    assert not cur.recall("喜欢鲸鱼", now=NOW)


def test_decay_applies_to_episodes_not_stable_facts():
    cur = MemoryCurator(InMemoryStore())
    old = cur.remember("鲸鱼提醒", source="user", evidence_id="1", importance=0.5,
                       consent=True, key="old", now=NOW-timedelta(days=90))
    new = cur.remember("鲸鱼提醒", source="user", evidence_id="2", importance=0.5,
                       consent=True, key="new", now=NOW)
    hits = cur.recall("鲸鱼提醒", now=NOW)
    assert hits[0].record.key == "new"
    assert hits[1].record.key == "old"


def test_inner_state_persists_and_user_can_reset():
    store = InMemoryStore()
    state = InnerStateStore(store)
    state.update(style="curious", add_interest="芯片", open_topic="模拟电路错题")
    assert "模拟电路错题" in InnerStateStore(store).load().unfinished_topics
    assert state.update(close_topic="模拟电路错题").unfinished_topics == ()
    state.reset()
    assert state.load().style == "neutral"


def test_deepseek_companion_opt_in_curated_context():
    from whale_core.core.companion import WhaleCompanion
    from whale_core.memory.store import PersonaContinuity
    store = InMemoryStore()
    store.put_persona_continuity(PersonaContinuity())
    remember_episode(store, summary="未审查的电话内容", key="ep:call:raw")
    cur = MemoryCurator(store)
    cur.remember("喜欢模拟电路", source="owner", evidence_id="chat:10",
                 consent=True, stable_fact=True)
    class FakeText:
        def reply(self, text, *, persona="", memories=(), history=()):
            assert "模拟电路" in " ".join(memories)
            assert "未审查" not in " ".join(memories)
            return "记得这件事"
    WhaleCompanion(store, FakeText(), curator=cur).chat("模拟电路")
