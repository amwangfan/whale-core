"""S7 跨渠道接线集成测试（Whale Studio，离线）。

覆盖 goal-chain S7 验收的离线部分：QQ 群/私聊/陌生私聊端到端
ResolvedContext → 人格/记忆/能力按会话生效；真实 QQ 渠道联调单独标注。
"""

from __future__ import annotations

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, InputEnvelope
from whale_core.life.context_resolver import (
    ConversationContextResolver, pairing_lookup_from_mapping)
from whale_core.life.conversation_context import MemorySpaceType
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.life.temporary_auth import TemporaryAuthService
from whale_core.memory.bindings import BindingStore
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.spaces import SpaceStore, ensure_owner_working_copy
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord


class Brain:
    """记录收到的 persona/memories，回固定文本。"""

    def __init__(self):
        self.calls = []

    def reply(self, text, **kwargs):
        self.calls.append(kwargs)
        return "收到：" + text[:20]


def _world():
    store = InMemoryStore()
    spaces = SpaceStore(store)
    bindings = BindingStore(store, spaces)
    auth = TemporaryAuthService(store)
    owner_space = ensure_owner_working_copy(store)
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("qq", "10001"): "owner"}),
        bindings=bindings, grants=auth, owner_id="owner",
        owner_default_space=owner_space)
    brain = Brain()
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    orch = ConversationOrchestrator(store, companion, context_resolver=resolver)
    return store, spaces, bindings, auth, resolver, brain, orch


# ---------------------------------------------------------------------------
# 主人私聊：记忆读写走工作副本
# ---------------------------------------------------------------------------


def test_owner_private_memory_roundtrip_via_working_copy():
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    orch.handle_envelope(
        InputEnvelope(conversation_id="private:10001", text="你好呀",
                      channel="qq", sender_id="10001", source="qq:owner"))
    # 情节写入 owner-working 空间
    eps = [r for r in store.list_by_kind(MemoryKind.EPISODE, space_ids=("owner-working",))]
    assert eps, "主人私聊应写入工作副本"
    # 下一轮对话能召回（经授权空间）
    orch.handle_envelope(InputEnvelope(conversation_id="private:10001",
                                       text="还记得吗", channel="qq",
                                       sender_id="10001", source="qq:owner"))
    last = brain.calls[-1]
    assert any("你好呀" in m for m in last["memories"])


def test_owner_in_group_no_private_memory():
    """INV-03：主人在群里发言，不读到私人工作副本，也不写入。"""
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    orch.handle_envelope(InputEnvelope(conversation_id="private:10001",
                                       text="我的私人记忆", channel="qq",
                                       sender_id="10001", source="qq:owner"))
    orch.handle_envelope(InputEnvelope(conversation_id="group:7", text="继续",
                                       channel="qq", sender_id="10001",
                                       source="qq:owner"))
    last = brain.calls[-1]
    assert not any("私人记忆" in m for m in last["memories"])
    # 群里不产生情节写回（无群空间绑定 → fail closed）
    assert store.list_by_kind(MemoryKind.EPISODE, space_ids=("group:7",)) == []


# ---------------------------------------------------------------------------
# 群空间：绑定后群内可读，跨群不可读（N02/N03）
# ---------------------------------------------------------------------------


def test_group_space_scoped_recall():
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    g = spaces.create(MemorySpaceType.GROUP_SPACE, name="七班", space_id="g-7")
    store.put_record(MemoryRecord(
        kind=MemoryKind.PREFERENCE, key="g7-fact",
        content={"summary": "七班周五聚餐", "stable_fact": True,
                 "importance": 0.8, "confidence": 1.0},
        tags=("curated", "stable"), space_id="g-7"))
    bindings.bind_memory("qq:qq_group:group:7", "g-7", granted_by="owner")
    orch.handle_envelope(InputEnvelope(conversation_id="group:7", text="周五安排",
                                       channel="qq", sender_id="member"))
    assert any("七班" in m for m in brain.calls[-1]["memories"])
    # 别群成员查不到（resolver 不给 space）
    orch.handle_envelope(InputEnvelope(conversation_id="group:8", text="周五安排",
                                       channel="qq", sender_id="member"))
    assert not any("七班" in m for m in brain.calls[-1]["memories"])


# ---------------------------------------------------------------------------
# 陌生人私聊：sudo → 租约 → 受限对话；无授权时无任何记忆
# ---------------------------------------------------------------------------


def test_stranger_sudo_then_scoped_chat():
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth)
    code = auth.create_code(allowed_scopes=("task.read:self",))

    class In:
        channel = "qq"
        account = "u_stranger"
        conversation = "private:u_stranger"
        text = f"sudo {code}"

    out = wf.handle_stranger_event(In())
    assert "验证通过" in out["reply"]
    assert out["auth_grant"]
    # 口令明文不落记忆/历史
    assert all(code not in str(r.content)
               for kind in MemoryKind for r in store.list_by_kind(kind))
    # 租约生效后的普通对话：匿名 + 租约 scope；仍无任何记忆空间
    In.text = "你好"
    out2 = wf.handle_stranger_event(In())
    assert out2["reply"].startswith("收到")
    ctx = resolver.resolve(channel="qq", sender_id="u_stranger",
                           conversation_id="private:u_stranger")
    assert ctx.allowed_memory_spaces == ()
    assert "task.read:self" in ctx.capability_scopes
    # 陌生人对话不写情节
    assert store.list_by_kind(MemoryKind.EPISODE, space_ids=("",)) == []


def test_stranger_cannot_read_owner_memory():
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    orch.handle_envelope(InputEnvelope(conversation_id="private:10001",
                                       text="我怕辣", channel="qq",
                                       sender_id="10001", source="qq:owner"))
    wf = QqInboundWorkflow(store, brain, orchestrator=orch, auth_service=auth)

    class In:
        channel = "qq"
        account = "u_eve"
        conversation = "private:u_eve"
        text = "我怕什么？"

    wf.handle_stranger_event(In())
    assert not any("辣" in m for m in brain.calls[-1]["memories"])


def test_stranger_group_sudo_gets_guidance_only():
    """群里 sudo 尝试：不验证、不回显、不落记忆，只提示私聊（N05/N17）。"""
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth)

    class In:
        channel = "qq"
        account = "u_x"
        conversation = "group:7"
        text = "sudo 某个口令"

    out = wf.handle_stranger_event(In())
    assert "私聊" in out["reply"]
    assert out.get("auth_grant") is None
    # 口令不进入任何记忆
    assert all("某个口令" not in str(r.content)
               for kind in MemoryKind for r in store.list_by_kind(kind))


def test_group_member_chat_gets_group_ceiling_reply():
    """S7 灰度：群成员消息走匿名群上下文（chat.basic，无 sudo、无记忆）。"""
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth)

    class In:
        channel = "qq"
        account = "u_member"
        conversation = "group:7"
        text = "大家好"

    out = wf.handle_stranger_event(In())
    assert out["reply"].startswith("收到")
    ctx = resolver.resolve(channel="qq", sender_id="u_member",
                           conversation_id="group:7")
    assert "outbox.submit" not in ctx.capability_scopes


# ---------------------------------------------------------------------------
# 能力收窄（S5 bind:caps → resolver 消费）
# ---------------------------------------------------------------------------


def test_capabilities_binding_narrows_only():
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    store.put("bind:caps:qq:qq_private:private:10001",
              {"scopes": ["chat.basic", "tool.web.search"], "version": 1})
    ctx = resolver.resolve(channel="qq", sender_id="10001",
                           conversation_id="private:10001")
    assert ctx.capability_scopes == ("chat.basic", "tool.web.search")
    # 未配置的会话不受影响
    other = resolver.resolve(channel="qq", sender_id="10001",
                             conversation_id="private:other")
    assert "outbox.submit" in other.capability_scopes


# ---------------------------------------------------------------------------
# 人格按会话生效（S3 × S7 接线路径）
# ---------------------------------------------------------------------------


def test_persona_differs_by_conversation():
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    store.put("persona:card:whale-playful", {
        "id": "whale-playful", "name": "鲸鱼",
        "summary": "有点傲娇", "temperament": "随性"})
    bindings.bind_persona("qq:qq_group:group:7", "whale-playful",
                          updated_by="owner")
    ctx7 = resolver.resolve(channel="qq", sender_id="m", conversation_id="group:7")
    ctx8 = resolver.resolve(channel="qq", sender_id="m", conversation_id="group:8")
    assert ctx7.persona_id == "whale-playful"
    assert ctx8.persona_id is None
