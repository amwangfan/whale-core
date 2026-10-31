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


def _prefs_no_quiet():
    from datetime import time as _t
    from whale_core.life.initiative import ProactivePrefs
    return ProactivePrefs(quiet_start=_t(0), quiet_end=_t(0))


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
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth,
                         prefs=_prefs_no_quiet())
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
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth,
                         prefs=_prefs_no_quiet())

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
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth,
                         prefs=_prefs_no_quiet())

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


# ---------------------------------------------------------------------------
# G2.4：成员私聊自助指令（授权/撤销/查看/清除）
# ---------------------------------------------------------------------------


class _In:
    def __init__(self, sender, text, conv=None):
        self.channel = "qq"
        self.account = sender
        self.conversation = conv or f"private:u_{sender}"
        self.text = text
        self.sender_id = sender


def test_member_self_service_full_cycle():
    """授权（有共同群证据）→ 群里自述被提炼 → 私聊可回忆 → 撤销 → 清除。"""
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    from whale_core.memory.timeline import GroupTimelineStore
    timeline = GroupTimelineStore(store)
    orch.companion.timeline = timeline
    from whale_core.life.channels import record_uid_mapping
    record_uid_mapping(store, "u_s", "u_s")  # 测试形态：uin==uid 恒等映射
    timeline.append("group:7", message_id="m1", sender_id="u_s",
                    text="我明天要交论文，电脑还卡死了")
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth)
    wf.companion.timeline = timeline

    # 默认在记（owner 决策 2026-11-01）：授权指令=恢复/确认语义
    out1 = wf.handle_stranger_event(_In("u_s", "同意记录我", conv="private:u_s"))
    assert "记" in out1["reply"]
    from whale_core.memory.consent import MemberConsentStore
    from whale_core.memory.bindings import BindingStore
    # 新模型：无 opt-out 标记 + 已建档（默认开启，不再要求 consent 记录）
    assert not MemberConsentStore(store).is_opted_out("anon:qq:u_s")
    assert BindingStore(store, spaces).member_binding_for("anon:qq:u_s")

    # 提炼（时间线里已有自述）→ 本人私聊可读
    from whale_core.memory.extraction import MemberMemoryExtractor
    MemberMemoryExtractor(store, timeline, MemberConsentStore(store),
                          bindings, spaces).extract_for_principal(
                              "anon:qq:u_s", "group:7")
    out2 = wf.handle_stranger_event(_In("u_s", "我的记忆", conv="private:u_s"))
    assert "论文" in out2["reply"]

    # 清除：真删 + 撤销 + 不可再召回
    out3 = wf.handle_stranger_event(_In("u_s", "删掉我的记忆", conv="private:u_s"))
    assert "清掉" in out3["reply"]
    assert MemberConsentStore(store).is_opted_out("anon:qq:u_s")
    out4 = wf.handle_stranger_event(_In("u_s", "我的记忆", conv="private:u_s"))
    assert "空的" in out4["reply"] or "没授权" in out4["reply"]


def test_member_commands_group_chat_not_management():
    """群里的记忆管理短语只能作为普通消息，不能改变成员本人设置。"""
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    from whale_core.memory.consent import MemberConsentStore

    principal_id = "anon:qq:u_s"
    consents = MemberConsentStore(store)
    consents.opt_out(principal_id)  # 建立明确基线：该成员此前已退出记忆
    wf = QqInboundWorkflow(
        store, Brain(), orchestrator=orch, auth_service=auth,
        prefs=_prefs_no_quiet())
    out = wf.handle_stranger_event(
        _In("u_s", "同意记录我", conv="group:7"))

    # Brain 是回显测试替身，回复正文出现原短语不代表执行了管理指令。
    # 应断言群聊不能替成员本人重新开启记忆，而不是限制回复复述输入。
    assert out.get("reply") == "收到：同意记录我"
    assert consents.is_opted_out(principal_id)
    assert consents.list_for(principal_id) == ()
    assert bindings.member_binding_for(principal_id) is None


# ---------------------------------------------------------------------------
# issue #22：静默时段只拦主动打扰；陌生人/群友先发的被动回复深夜不拦截
# ---------------------------------------------------------------------------


def test_stranger_passive_reply_not_blocked_in_quiet_hours():
    """深夜（静默窗口内）陌生人私聊仍得到回复；防滥用日额度照常生效。"""
    from datetime import datetime, time as _t, timezone
    from whale_core.life.initiative import ProactivePrefs

    store, spaces, bindings, auth, resolver, brain, orch = _world()
    # 2026-11-01 03:30 Asia/Shanghai（静默窗 22:00–07:00 内）
    night = datetime(2026, 10, 31, 19, 30, tzinfo=timezone.utc)
    prefs = ProactivePrefs(enabled=True,
                           allowed_channels=frozenset({"direct_message"}),
                           quiet_start=_t(22), quiet_end=_t(7))
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth,
                           prefs=prefs, now_fn=lambda: night)

    class In:
        channel = "qq"
        account = "u_night"
        conversation = "private:u_night"
        text = "睡了吗"

    out = wf.handle_stranger_event(In())
    assert out.get("skipped") != "quiet_hours"      # 被动回复不被深夜拦截
    assert out["reply"].startswith("收到")

    # 主动打扰在同一时刻仍被静默拦下（unsolicited 不豁免）
    from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox
    ob = ProactiveOutbox(store)
    ob.propose(ProactiveDraft("n1", "t", "r", ("e",), channel="direct_message"))
    assert ob.inspect("n1", prefs, now=night).status == "deferred"


def test_stranger_daily_budget_still_enforced_at_night():
    """被动豁免静默 ≠ 豁免防滥用额度（issue #22 边界）。"""
    from datetime import datetime, time as _t, timezone
    from whale_core.life.initiative import ProactivePrefs

    store, spaces, bindings, auth, resolver, brain, orch = _world()
    night = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)
    prefs = ProactivePrefs(quiet_start=_t(22), quiet_end=_t(7))
    wf = QqInboundWorkflow(store, Brain(), orchestrator=orch, auth_service=auth,
                           prefs=prefs, now_fn=lambda: night)

    class In:
        channel = "qq"
        account = "u_spam"
        conversation = "private:u_spam"
        text = "刷屏"

    for _ in range(wf.STRANGER_DAILY_LIMIT):
        out = wf.handle_stranger_event(In())
        assert "skipped" not in out
    out = wf.handle_stranger_event(In())
    assert out.get("skipped") == "stranger_budget"


# ---------------------------------------------------------------------------
# issue #14：主人私聊 vault 端到端召回 + 非主人零泄漏
# ---------------------------------------------------------------------------


def _world_with_vault():
    """_world + 真实 vault 空间与绑定（owner 大号私聊例外链路）。"""
    store, spaces, bindings, auth, resolver, brain, orch = _world()
    store.put("space:vault-1", {
        "space_id": "vault-1", "type": "protected_original",
        "name": "默认私人原始库", "owner": "owner", "version": 1,
        "created_at": "t", "retired_at": None})
    store.put_record(MemoryRecord(
        kind=MemoryKind.EPISODE, key="v-fact",
        content={"summary": "主人对芒果过敏", "importance": 0.9,
                 "confidence": 1.0},
        tags=("curated",), space_id="vault-1"))
    # 重建 resolver：bindings 带 owner 私聊白名单
    owner_key = "qq:qq_private:private:10001"
    bindings2 = BindingStore(store, spaces,
                             owner_conversations=frozenset({owner_key}))
    bindings2.bind_memory(owner_key, "vault-1", granted_by="owner")
    resolver2 = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("qq", "10001"): "owner"}),
        bindings=bindings2, grants=auth, owner_id="owner")
    orch2 = ConversationOrchestrator(
        store, WhaleCompanion(store, brain, curator=MemoryCurator(store)),
        context_resolver=resolver2)
    return store, brain, orch2


def test_owner_private_recalls_vault_end_to_end():
    store, brain, orch = _world_with_vault()
    orch.handle_envelope(InputEnvelope(
        conversation_id="private:10001", text="我有什么过敏？",
        channel="qq", sender_id="10001", source="qq:owner"))
    assert any("芒果" in m for m in brain.calls[-1]["memories"])


def test_stranger_and_group_never_recall_vault():
    store, brain, orch = _world_with_vault()
    # 陌生人私聊：无绑定 → 不召回
    orch.handle_envelope(InputEnvelope(
        conversation_id="private:u_eve", text="芒果？",
        channel="qq", sender_id="u_eve"))
    assert not any("芒果" in m for m in brain.calls[-1]["memories"])
    # owner 本人在群聊：也不召回（kind 不符，allows_memory_read=False）
    orch.handle_envelope(InputEnvelope(
        conversation_id="group:9", text="芒果？",
        channel="qq", sender_id="10001", source="qq:owner"))
    assert not any("芒果" in m for m in brain.calls[-1]["memories"])
