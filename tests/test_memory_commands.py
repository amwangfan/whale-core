"""core/memory_commands.py（记住/忘掉共享指令）离线测试。"""

from __future__ import annotations

from whale_core.core.memory_commands import handle_memory_command
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore, MemoryKind


def test_remember_stores_stable_curated_fact():
    store = InMemoryStore()
    reply = handle_memory_command(store, "记住：我怕辣", source="qq:owner",
                                  evidence_id="qq:c:m1")
    assert reply == "好，我记住啦：我怕辣"
    prefs = store.list_by_kind(MemoryKind.PREFERENCE)
    assert len(prefs) == 1
    assert prefs[0].content["summary"] == "用户自述：我怕辣"
    assert prefs[0].content["source"] == "qq:owner"
    assert prefs[0].content["evidence_id"] == "qq:c:m1"
    assert prefs[0].content["stable_fact"] is True


def test_forget_removes_matched_fact():
    store = InMemoryStore()
    handle_memory_command(store, "记住：我怕辣", source="s", evidence_id="e1")
    reply = handle_memory_command(store, "忘掉：辣", source="s", evidence_id="e2")
    assert reply and reply.startswith("已忘掉")
    assert store.list_by_kind(MemoryKind.PREFERENCE) == []


def test_forget_miss_and_empty_inputs():
    store = InMemoryStore()
    assert handle_memory_command(store, "忘掉：不存在", source="s", evidence_id="e") \
        == "没有找到相关的记忆诶"
    assert handle_memory_command(store, "记住：", source="s", evidence_id="e") \
        == "想让我记住什么？"
    assert handle_memory_command(store, "忘掉：", source="s", evidence_id="e") \
        == "想让我忘掉什么？"


def test_non_command_returns_none():
    store = InMemoryStore()
    assert handle_memory_command(store, "今天天气怎么样", source="s", evidence_id="e") is None
    assert handle_memory_command(store, "", source="s", evidence_id="e") is None


def test_forget_does_not_touch_plain_episodes():
    """忘掉只删 curated 记录；普通聊天情节不受影响（删除语义边界）。"""
    from whale_core.memory.continuity import remember_episode
    store = InMemoryStore()
    remember_episode(store, summary="聊过辣椒的事", key="ep:chat:x", tags=("chat",))
    handle_memory_command(store, "记住：我怕辣", source="s", evidence_id="e1")
    handle_memory_command(store, "忘掉：辣", source="s", evidence_id="e2")
    episodes = store.list_by_kind(MemoryKind.EPISODE)
    assert any(r.key == "ep:chat:x" for r in episodes)
    assert store.list_by_kind(MemoryKind.PREFERENCE) == []


def test_shared_curator_is_used():
    store = InMemoryStore()
    curator = MemoryCurator(store, half_life_days=7)
    handle_memory_command(store, "记住：我怕辣", source="s", evidence_id="e",
                          curator=curator)
    assert curator.recall("辣")
