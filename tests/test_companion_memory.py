"""PR-1 记忆 V1 修复的核心行为测试：

- WhaleCompanion 多轮历史传递（conversation_id 启用，默认单轮兼容）；
- 召回断层修复：chat 情节作为会话记忆进入 prompt（curated 事实优先）；
- QqInboundWorkflow 的「记住/忘掉」显式记忆指令（不调模型、证据链、可删除）。
"""

from __future__ import annotations

from whale_core.core.companion import WhaleCompanion
from whale_core.life.channels import OwnerEvent
from whale_core.life.initiative import ProactivePrefs
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore, MemoryKind


class FakeBrain:
    """捕获 reply() 实际收到的 persona/memories/history 的测试替身。"""

    def __init__(self, reply_text: str = "好的呀"):
        self.calls: list[dict] = []
        self.reply_text = reply_text

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append({
            "text": text,
            "persona": persona,
            "memories": list(memories),
            "history": list(history),
        })
        return self.reply_text


def _workflow(store, brain):
    prefs = ProactivePrefs(
        enabled=True,
        allowed_channels=frozenset({"direct_message"}),
        passive_daily_limit=500,
    )
    return QqInboundWorkflow(store, brain, prefs=prefs)


def _event(text: str, message_id: str = "m1") -> OwnerEvent:
    return OwnerEvent(owner_id="owner", channel="qq", conversation="private:u_test",
                      message_id=message_id, text=text)


# ---------- WhaleCompanion：多轮历史 ----------

def test_chat_without_conversation_id_keeps_single_turn_compat():
    store, brain = InMemoryStore(), FakeBrain()
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    companion.chat("你好")
    companion.chat("第二轮")
    assert brain.calls[1]["history"] == []


def test_chat_with_conversation_id_carries_history():
    store, brain = InMemoryStore(), FakeBrain()
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    companion.chat("我叫小凡", conversation_id="conv-1")
    companion.chat("我叫什么", conversation_id="conv-1")
    history = brain.calls[1]["history"]
    assert history == [
        {"role": "user", "content": "我叫小凡"},
        {"role": "assistant", "content": "好的呀"},
    ]


def test_history_isolated_per_conversation():
    store, brain = InMemoryStore(), FakeBrain()
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    companion.chat("A 会话第一句", conversation_id="A")
    companion.chat("B 会话第一句", conversation_id="B")
    assert brain.calls[1]["history"] == []


# ---------- WhaleCompanion：召回断层修复 ----------

def test_recent_chat_episodes_reach_prompt_as_memories():
    store, brain = InMemoryStore(), FakeBrain()
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    companion.chat("今天去爬山了", conversation_id="c")
    companion.chat("腿好酸", conversation_id="c")
    memories = brain.calls[1]["memories"]
    assert any("爬山" in m for m in memories)


def test_curated_fact_ranks_before_episodes():
    store, brain = InMemoryStore(), FakeBrain()
    curator = MemoryCurator(store)
    curator.remember("用户对香菜过敏", source="qq:owner", evidence_id="qq:c:m0",
                     importance=0.9, stable_fact=True, consent=True)
    companion = WhaleCompanion(store, brain, curator=curator)
    companion.chat("今晚吃香菜面", conversation_id="c")
    companion.chat("香菜的事还记得吗", conversation_id="c")
    memories = brain.calls[1]["memories"]
    assert memories[0] == "用户对香菜过敏"
    assert any("吃香菜面" in m for m in memories)


# ---------- QqInboundWorkflow：会话接入 + 记忆指令 ----------

def test_workflow_passes_conversation_id_to_companion():
    store, brain = InMemoryStore(), FakeBrain()
    wf = _workflow(store, brain)
    wf.handle_owner_event(_event("第一句", "m1"))
    wf.handle_owner_event(_event("第二句", "m2"))
    history = brain.calls[1]["history"]
    assert history[0]["content"] == "第一句"
    assert history[1]["content"] == "好的呀"


def test_remember_command_stores_curated_fact_without_calling_brain():
    store, brain = InMemoryStore(), FakeBrain()
    wf = _workflow(store, brain)
    result = wf.handle_owner_event(_event("记住：我喜欢蓝色", "m1"))
    assert result["reply"].startswith("好，我记住啦")
    assert brain.calls == []  # 指令路径不调模型
    prefs = store.list_by_kind(MemoryKind.PREFERENCE)
    assert len(prefs) == 1
    assert prefs[0].content["summary"] == "用户自述：我喜欢蓝色"
    assert prefs[0].content["evidence_id"] == "qq:private:u_test:m1"
    assert "curated" in prefs[0].tags and "stable" in prefs[0].tags


def test_remembered_fact_recalled_in_later_chat():
    store, brain = InMemoryStore(), FakeBrain()
    wf = _workflow(store, brain)
    wf.handle_owner_event(_event("记住：我喜欢蓝色", "m1"))
    wf.handle_owner_event(_event("我喜欢什么颜色", "m2"))
    assert any("我喜欢蓝色" in m for m in brain.calls[0]["memories"])


def test_forget_command_removes_only_matched_curated_fact():
    store, brain = InMemoryStore(), FakeBrain()
    wf = _workflow(store, brain)
    wf.handle_owner_event(_event("记住：我喜欢蓝色", "m1"))
    result = wf.handle_owner_event(_event("忘掉：蓝色", "m2"))
    assert result["reply"].startswith("已忘掉")
    assert store.list_by_kind(MemoryKind.PREFERENCE) == []


def test_forget_without_match_is_graceful():
    store, brain = InMemoryStore(), FakeBrain()
    wf = _workflow(store, brain)
    result = wf.handle_owner_event(_event("忘掉：不存在的事", "m1"))
    assert result["reply"] == "没有找到相关的记忆诶"
    assert brain.calls == []


def test_empty_command_asks_back():
    store, brain = InMemoryStore(), FakeBrain()
    wf = _workflow(store, brain)
    result = wf.handle_owner_event(_event("记住：", "m1"))
    assert result["reply"] == "想让我记住什么？"
    assert store.list_by_kind(MemoryKind.PREFERENCE) == []


def test_normal_text_still_flows_through_brain_and_draft():
    store, brain = InMemoryStore(), FakeBrain()
    wf = _workflow(store, brain)
    result = wf.handle_owner_event(_event("随便聊聊", "m1"))
    assert result["reply"] == "好的呀"
    assert result["draft_id"] == "qq-reply:m1"
    assert len(brain.calls) == 1
