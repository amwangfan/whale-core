"""记忆分层治理 + 跨设备 OwnerIdentity。

（原 tests/test_g2_memory_identity.py，G2 里程碑命名；issue #29 起按领域与层级组织。）

DoD 对照：
- QQ 提出任务 → 另一渠道（语音/网页）按 owner 查到进度（tasks_for_owner）；
- 错误记忆可更正（superseded 不再召回）/可删除（embedding 传播）；
- 情节记忆质量：同一轮 ack+最终答复合并一条；内部调用不写情节；
- pending_fact 模型推断不自动升级为事实；显式确认才进召回；
- 重启后人格不丢（persona 在 store，与治理操作互不影响）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import (
    ConversationOrchestrator,
    InputEnvelope,
    ToolRegistry,
    ToolSpec,
)
from whale_core.life.identity import OwnerRegistry
from whale_core.memory.continuity import remember_episode
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.governance import (
    confirm_fact,
    dedupe_episodes,
    expire_episodes,
    forget_gone_everywhere,
    layer_of,
    propose_fact,
    supersede,
)
from whale_core.memory.store import InMemoryStore, MemoryKind


class StubBrain:
    def __init__(self, reply: str = "好的"):
        self._reply = reply

    def reply(self, text, *, persona="", memories=(), history=(), **kw):
        return self._reply


# ---------- OwnerIdentity / SessionBinding ----------


def test_owner_registry_bind_resolve_unbind():
    reg = OwnerRegistry(InMemoryStore())
    reg.bind("owner-1", "qq", "3563425362")
    reg.bind("owner-1", "web", "console-local")
    assert reg.resolve("qq", "3563425362") == "owner-1"
    assert reg.resolve("web", "console-local") == "owner-1"
    assert reg.resolve("qq", "someone-else") is None
    assert len(reg.channels_for("owner-1")) == 2
    assert reg.unbind("qq", "3563425362") is True
    assert reg.resolve("qq", "3563425362") is None


def test_owner_registry_no_implicit_rebind():
    reg = OwnerRegistry(InMemoryStore())
    reg.bind("owner-1", "qq", "u1")
    with pytest.raises(ValueError):
        reg.bind("owner-2", "qq", "u1")  # 不得静默改绑


def test_session_binding_scopes_and_revoke():
    reg = OwnerRegistry(InMemoryStore())
    reg.bind_session("call-1", "owner-1", "voice", scopes=("task_progress",))
    binding = reg.resolve_session("call-1")
    assert binding is not None and binding.owner_id == "owner-1"
    assert binding.allows("task_progress") and not binding.allows("reminders")
    assert reg.revoke_session("call-1") is True
    assert reg.resolve_session("call-1") is None


# ---------- 跨设备任务可见性（DoD：QQ 提出 → 语音查进度） ----------


def test_tasks_for_owner_cross_channel():
    store = InMemoryStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="gen", capability="media.image.generate",
                            handler=lambda s: "done"))
    companion = WhaleCompanion(store, StubBrain())

    class _Intent:
        def plan(self, text, *, context=""):
            return json.dumps({"goal": "画图", "kind": "task",
                               "capabilities": ["media.image.generate"]})

    orc = ConversationOrchestrator(store, companion, intent_model=_Intent(),
                                   tools=tools)
    done = threading.Event()
    # QQ 渠道发起（信封携带 owner_id 与 channel）
    orc.handle_envelope_async(
        InputEnvelope(conversation_id="qq:c1", text="画只猫", source="qq:owner",
                      channel="qq", owner_id="owner-1"),
        on_complete=lambda r: done.set())
    assert done.wait(5)
    # 语音渠道查询同一 owner 的任务进度
    tasks = orc.tasks_for_owner("owner-1")
    assert len(tasks) == 1
    assert tasks[0]["channel"] == "qq" and tasks[0]["phase"] in ("succeeded", "delivered")
    assert "reply" not in tasks[0] and "text" not in tasks[0]  # 不跨渠道泄漏正文
    assert orc.tasks_for_owner("owner-2") == []  # 其它 owner 不可见
    assert orc.tasks_for_owner("") == []


# ---------- 情节质量：一轮一条，内部调用不写 ----------


def test_episode_consolidation_per_turn():
    class EchoBrain:
        def reply(self, text, *, persona="", memories=(), history=(), **kw):
            return text  # 回声替身：区分两次调用的答复内容

    store = InMemoryStore()
    companion = WhaleCompanion(store, EchoBrain())
    companion.chat("我在画了", conversation_id="c1", user_text="画只猫",
                   turn_key="task-1")  # ack
    companion.chat("画好了", conversation_id="c1", user_text="画只猫",
                   turn_key="task-1")  # 最终答复（同一轮覆盖同一条情节）
    episodes = [r for r in store.list_by_kind(MemoryKind.EPISODE)
                if "chat" in r.tags]
    assert len(episodes) == 1  # ack + 最终答复合并为一条
    assert "画好了" in episodes[0].content["summary"]


def test_internal_call_not_recorded():
    store = InMemoryStore()
    companion = WhaleCompanion(store, StubBrain())
    companion.chat("描述这张图", conversation_id="c1", episodic=False)
    assert [r for r in store.list_by_kind(MemoryKind.EPISODE)
            if "chat" in r.tags] == []


# ---------- 记忆治理 ----------


def test_dedupe_episodes_keeps_newest():
    store = InMemoryStore()
    remember_episode(store, summary="重复内容", key="ep:1",
                     tags=("chat",), extra={"layer": "episode"})
    remember_episode(store, summary="重复内容", key="ep:2",
                     tags=("chat",), extra={"layer": "episode"})
    remember_episode(store, summary="独特内容", key="ep:3",
                     tags=("chat",), extra={"layer": "episode"})
    assert dedupe_episodes(store) == 1
    assert store.get_record("ep:2") is not None  # 保留最新
    assert store.get_record("ep:1") is None


def test_expire_episodes_respects_curated():
    store = InMemoryStore()
    old = datetime.now(timezone.utc) - timedelta(days=120)
    MemoryCurator(store).remember("长期事实", source="qq", evidence_id="e1",
                                  stable_fact=True, consent=True, now=old)
    remember_episode(store, summary="旧聊天", key="ep:old", tags=("chat",))
    # 手工把旧情节的 created_at 改老
    rec = store.get_record("ep:old")
    store.put_record(type(rec)(kind=rec.kind, key=rec.key, content=dict(rec.content),
                               record_id=rec.record_id, created_at=old,
                               tags=rec.tags))
    remember_episode(store, summary="新聊天", key="ep:new", tags=("chat",))
    assert expire_episodes(store, older_than_days=90) == 1
    assert store.get_record("ep:old") is None
    assert store.get_record("ep:new") is not None
    # curated 稳定事实永不过期
    assert any("长期事实" in str(r.content.get("summary"))
               for r in store.list_by_kind(MemoryKind.PREFERENCE))


def test_pending_fact_not_recalled_until_confirmed():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    key = propose_fact(store, "用户可能喜欢辣", source="inferred",
                       evidence_id="chat:c1:1", confidence=0.6)
    # 未确认：不进召回
    assert curator.recall("辣") == ()
    assert layer_of(store.get_record(key)) == "pending_fact"
    # 显式确认后：升级为 curated 稳定事实并可召回
    assert confirm_fact(store, key, curator=curator) is True
    hits = curator.recall("辣")
    assert len(hits) == 1 and "辣" in hits[0].record.content["summary"]
    assert layer_of(hits[0].record) == "preference"
    assert hits[0].record.content["consent"] is True
    assert hits[0].record.content["last_confirmed"]


def test_supersede_excludes_old_from_recall():
    store = InMemoryStore()
    curator = MemoryCurator(store)
    old = curator.remember("用户住在上海", source="qq", evidence_id="e1",
                           stable_fact=True, consent=True)
    assert curator.recall("上海")
    supersede(store, old.key, "用户住在杭州", source="qq", evidence_id="e2")
    # 旧记忆不再召回；新事实可召回
    assert curator.recall("上海") == ()
    assert curator.recall("杭州")


def test_forget_propagates_to_embedding_cache():
    class FakeEmbedder:
        def __init__(self):
            self.purged = []

        def forget_text(self, text):
            self.purged.append(text)

        purged: list = []

    class FakeRegistry:
        def __init__(self, provider):
            self._p = provider

        def providers_for(self, capability):
            return [("embed", self._p)] if capability == "embedding.embed" else []

    store = InMemoryStore()
    embedder = FakeEmbedder()
    rec = MemoryCurator(store).remember("要删掉的秘密", source="qq",
                                        evidence_id="e1", stable_fact=True,
                                        consent=True)
    assert forget_gone_everywhere(store, rec.key, registry=FakeRegistry(embedder))
    assert store.get_record(rec.key) is None
    assert embedder.purged == ["要删掉的秘密"]


def test_persona_survives_governance():
    """治理操作绝不动人格连续性（DoD：重启后人格不丢）。"""
    from whale_core.memory.store import PersonaContinuity

    store = InMemoryStore()
    store.put_persona_continuity(PersonaContinuity(persona_name="鲸鱼", tone="温柔"))
    remember_episode(store, summary="旧聊天", key="ep:x", tags=("chat",))
    dedupe_episodes(store)
    expire_episodes(store, older_than_days=0.000001)
    persona = store.get_persona_continuity()
    assert persona is not None and persona.persona_name == "鲸鱼"
