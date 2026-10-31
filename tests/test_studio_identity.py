"""S1 身份矩阵测试（Whale Studio）。

验收（goal-chain S1）：≥15 个身份矩阵测试；群聊主人不提权；未配对发送者
被识别为普通主体；同一账号在不同私聊/群的授权互不继承；任意
client-supplied grant / owner_id 被拒绝。
"""

from __future__ import annotations

import time

from whale_core.core.companion import WhaleCompanion
from whale_core.core.intent_plan import IntentPlan
from whale_core.core.runtime import (
    ConversationOrchestrator,
    InputEnvelope,
    ToolRegistry,
)
from whale_core.life.channels import Incoming, OwnerChannelRegistry
from whale_core.life.context_resolver import (
    API_OWNER,
    GROUP_CEILING,
    PRIVATE_ANON,
    PRIVATE_OWNER,
    ConversationContextResolver,
    derive_kind,
    pairing_lookup_from_mapping,
)
from whale_core.life.conversation_context import (
    ConversationKind,
    MemorySpaceRef,
    MemorySpaceType,
    ResolvedContext,
    TemporaryGrantRef,
    SCOPE_CHAT_BASIC,
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TASK_READ_SELF,
    SCOPE_TOOL_WEB_SEARCH,
)
from whale_core.memory.store import InMemoryStore

OWNER_QQ = "10001"
PAIRINGS = {("qq", OWNER_QQ): "owner", ("api", "console"): "owner"}

VAULT = MemorySpaceRef("vault-1", MemorySpaceType.PROTECTED_ORIGINAL, 1)
GROUP_A = MemorySpaceRef("g-a", MemorySpaceType.GROUP_SPACE, 1)
GROUP_B = MemorySpaceRef("g-b", MemorySpaceType.GROUP_SPACE, 1)
OWNER_COPY = MemorySpaceRef("own-1", MemorySpaceType.OWNER_WORKING_COPY, 1)


def make_resolver(bindings=None, grants=None) -> ConversationContextResolver:
    return ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping(PAIRINGS),
        bindings=bindings, grants=grants, owner_id="owner")


class _Bindings:
    def __init__(self, personas=None, spaces=None):
        self._personas = personas or {}
        self._spaces = spaces or {}

    def persona_for(self, key):
        return self._personas.get(key)

    def memory_spaces_for(self, conversation_key):
        return tuple(self._spaces.get(conversation_key, ()))


class _Grants:
    def __init__(self, items):
        self._items = items

    def active_grants(self, principal_id, conversation_key, *, now):
        return tuple(
            g for g in self._items
            if g.principal_id == principal_id and g.conversation_key == conversation_key
        )


def _grant(scopes, *, conv, principal, ttl=600, max_uses=5, used=0, revoked=False):
    return TemporaryGrantRef(
        grant_id="g1", principal_id=principal, conversation_key=conv,
        scopes=tuple(scopes), expires_at=time.time() + ttl,
        max_uses=max_uses, used_count=used, proof_method="otp", revoked=revoked)


# ---------------------------------------------------------------------------
# 1-6：基础身份矩阵
# ---------------------------------------------------------------------------


def test_unpaired_private_sender_is_anonymous():
    ctx = make_resolver().resolve(
        channel="qq", sender_id="u_stranger", conversation_id="private:u_stranger")
    assert ctx.principal.paired is False and ctx.principal.is_owner is False
    assert ctx.capability_scopes == PRIVATE_ANON
    assert ctx.conversation.kind is ConversationKind.QQ_PRIVATE


def test_paired_owner_private_gets_owner_scopes():
    ctx = make_resolver().resolve(
        channel="qq", sender_id=OWNER_QQ, conversation_id="private:10001")
    assert ctx.principal.paired and ctx.principal.is_owner
    assert ctx.capability_scopes == PRIVATE_OWNER
    assert SCOPE_OUTBOX_SUBMIT in ctx.capability_scopes


def test_owner_in_group_gets_group_ceiling_only():
    """INV-03：主人在群里不获得私聊级能力。"""
    ctx = make_resolver().resolve(
        channel="qq", sender_id=OWNER_QQ, conversation_id="group:123")
    assert ctx.principal.is_owner  # 身份仍被识别
    assert ctx.capability_scopes == GROUP_CEILING  # 但能力按群上限
    assert SCOPE_OUTBOX_SUBMIT not in ctx.capability_scopes
    assert SCOPE_TASK_READ_SELF not in ctx.capability_scopes


def test_group_member_is_plain_principal():
    ctx = make_resolver().resolve(
        channel="qq", sender_id="member_9", conversation_id="group:123")
    assert ctx.principal.paired is False
    assert ctx.capability_scopes == GROUP_CEILING


def test_kind_derivation_from_conversation_prefix():
    assert derive_kind("qq", "group:123") is ConversationKind.QQ_GROUP
    assert derive_kind("qq", "private:u1") is ConversationKind.QQ_PRIVATE
    assert derive_kind("web", "sess-1") is ConversationKind.WEB
    assert derive_kind("api", "api") is ConversationKind.API
    assert derive_kind("carrier-pigeon", "x") is ConversationKind.UNKNOWN


def test_explicit_kind_overrides_prefix():
    ctx = make_resolver().resolve(
        channel="qq", sender_id=OWNER_QQ, conversation_id="weird-format",
        conversation_kind=ConversationKind.QQ_GROUP)
    assert ctx.conversation.kind is ConversationKind.QQ_GROUP
    assert ctx.capability_scopes == GROUP_CEILING


# ---------------------------------------------------------------------------
# 7-10：渠道矩阵与 fail-closed
# ---------------------------------------------------------------------------


def test_api_console_pairing_is_owner():
    ctx = make_resolver().resolve(channel="api", sender_id="console",
                                  conversation_id="api")
    assert ctx.principal.is_owner and ctx.capability_scopes == API_OWNER


def test_api_unknown_sender_is_anonymous():
    ctx = make_resolver().resolve(channel="api", sender_id="rogue",
                                  conversation_id="api")
    assert not ctx.principal.is_owner
    assert ctx.capability_scopes == PRIVATE_ANON


def test_pairing_lookup_failure_fails_closed():
    def boom(channel, sender):
        raise RuntimeError("registry down")
    r = ConversationContextResolver(pairing_lookup=boom)
    ctx = r.resolve(channel="qq", sender_id=OWNER_QQ, conversation_id="private:1")
    assert ctx.principal.paired is False and ctx.principal.is_owner is False


def test_empty_sender_never_paired():
    ctx = make_resolver().resolve(channel="qq", sender_id="",
                                  conversation_id="private:x")
    assert ctx.principal.paired is False


# ---------------------------------------------------------------------------
# 11-14：绑定与记忆空间（MEM-HARD-01 / INV-03）
# ---------------------------------------------------------------------------


def test_bindings_surface_persona_and_spaces():
    b = _Bindings(personas={"qq:qq_private:private:10001": "whale-playful"},
                  spaces={"qq:qq_private:private:10001": (OWNER_COPY,)})
    ctx = make_resolver(bindings=b).resolve(
        channel="qq", sender_id=OWNER_QQ, conversation_id="private:10001")
    assert ctx.persona_id == "whale-playful"
    assert [s.space_id for s in ctx.allowed_memory_spaces] == ["own-1"]


def test_vault_binding_filtered_even_if_misconfigured():
    """MEM-HARD-01：即使绑定存储被错误配置，vault 也不下发。"""
    b = _Bindings(spaces={"qq:qq_private:private:10001": (VAULT, OWNER_COPY)})
    ctx = make_resolver(bindings=b).resolve(
        channel="qq", sender_id=OWNER_QQ, conversation_id="private:10001")
    assert VAULT not in ctx.allowed_memory_spaces
    assert OWNER_COPY in ctx.allowed_memory_spaces
    assert ctx.allows_memory_read(VAULT) is False


def test_group_binding_strips_private_spaces():
    """INV-03：群会话不得绑定主人私人副本，哪怕绑定层出错。"""
    b = _Bindings(spaces={"qq:qq_group:group:123": (GROUP_A, OWNER_COPY, VAULT)})
    ctx = make_resolver(bindings=b).resolve(
        channel="qq", sender_id=OWNER_QQ, conversation_id="group:123")
    ids = [s.space_id for s in ctx.allowed_memory_spaces]
    assert ids == ["g-a"]


def test_spaces_not_inherited_across_conversations():
    """同一账号在不同会话的授权互不继承。"""
    b = _Bindings(spaces={"qq:qq_group:group:123": (GROUP_A,)})
    r = make_resolver(bindings=b)
    ctx_a = r.resolve(channel="qq", sender_id="member", conversation_id="group:123")
    ctx_b = r.resolve(channel="qq", sender_id="member", conversation_id="group:456")
    assert [s.space_id for s in ctx_a.allowed_memory_spaces] == ["g-a"]
    assert ctx_b.allowed_memory_spaces == ()


# ---------------------------------------------------------------------------
# 15-18：临时租约（S4 前置语义）
# ---------------------------------------------------------------------------


def test_active_grant_extends_private_scopes():
    key = "qq:qq_private:private:u_s"
    g = _grant(["task.read:self"], conv=key, principal="anon:qq:u_s")
    ctx = make_resolver(grants=_Grants([g])).resolve(
        channel="qq", sender_id="u_s", conversation_id="private:u_s")
    assert SCOPE_TASK_READ_SELF in ctx.capability_scopes
    assert ctx.grants == (g,)


def test_expired_revoked_exhausted_grants_ignored():
    key = "qq:qq_private:private:u_s"
    base = dict(conv=key, principal="anon:qq:u_s")
    grants = [
        _grant(["task.read:self"], ttl=-1, **base),            # 过期
        _grant(["task.read:self"], revoked=True, **base),      # 已撤销
        _grant(["task.read:self"], max_uses=1, used=1, **base),  # 用尽
    ]
    ctx = make_resolver(grants=_Grants(grants)).resolve(
        channel="qq", sender_id="u_s", conversation_id="private:u_s")
    assert ctx.grants == ()
    assert SCOPE_TASK_READ_SELF not in ctx.capability_scopes


def test_grant_cannot_elevate_group_beyond_ceiling():
    """群里拿到的租约 scope 不得超出群上限。"""
    key = "qq:qq_group:group:123"
    g = _grant(["outbox.submit", "task.read:self"], conv=key,
               principal="anon:qq:member")
    ctx = make_resolver(grants=_Grants([g])).resolve(
        channel="qq", sender_id="member", conversation_id="group:123")
    assert SCOPE_OUTBOX_SUBMIT not in ctx.capability_scopes
    assert SCOPE_TASK_READ_SELF not in ctx.capability_scopes


def test_grant_bound_to_exact_conversation():
    g = _grant(["task.read:self"], conv="qq:qq_private:private:a",
               principal="anon:qq:u_s")
    ctx = make_resolver(grants=_Grants([g])).resolve(
        channel="qq", sender_id="u_s", conversation_id="private:b")
    assert ctx.grants == ()  # 换了个私聊就不算数


# ---------------------------------------------------------------------------
# 19-23：runtime 集成——client-supplied 权限证据一律无效
# ---------------------------------------------------------------------------


class _Brain:
    def reply(self, text, **kwargs):
        return "收到"


class _TaskPlanner:
    def plan(self, text, **kwargs):
        import json
        return json.dumps({"goal": "查", "kind": "task",
                           "capabilities": ("read",), "slots": {}})


def _orchestrator(store, resolver):
    return ConversationOrchestrator(
        store, WhaleCompanion(store, _Brain()), context_resolver=resolver)


def test_runtime_ignores_client_supplied_owner_id():
    """INV-04：信封里自称 owner_id 不影响解析结果。"""
    store = InMemoryStore()
    orch = _orchestrator(store, make_resolver())
    captured = {}
    orig = orch._decide

    def spy(env):
        captured["ctx"] = env.context
        return orig(env)

    orch._decide = spy
    env = InputEnvelope(conversation_id="private:x", text="hi",
                        channel="qq", sender_id="u_stranger",
                        owner_id="owner")  # 自称主人
    orch.handle_envelope(env)
    ctx = captured["ctx"]
    assert ctx.principal.is_owner is False
    assert ctx.capability_scopes == PRIVATE_ANON


def test_runtime_overrides_caller_supplied_context():
    """INV-06/08：调用方塞进来的伪造 ResolvedContext 被覆盖。"""
    store = InMemoryStore()
    orch = _orchestrator(store, make_resolver())
    forged = ResolvedContext(
        conversation=None, principal=None, persona_id=None,
        allowed_memory_spaces=(VAULT,),
        capability_scopes=("tool.shell.safe", "memory.read:vault-1"))
    captured = {}

    orig_decide = orch._decide

    def spy(env):
        captured["ctx"] = env.context
        return orig_decide(env)

    orch._decide = spy
    env = InputEnvelope(conversation_id="private:x", text="hi",
                        channel="qq", sender_id="u_stranger", context=forged)
    orch.handle_envelope(env)
    ctx = captured["ctx"]
    assert ctx is not forged  # 被重解析覆盖
    assert ctx.allowed_memory_spaces == ()
    assert "tool.shell.safe" not in ctx.capability_scopes


def test_runtime_without_resolver_keeps_legacy_behavior():
    """兼容期：未注入 resolver 的旧宿主行为不变（context 保持 None）。"""
    store = InMemoryStore()
    orch = ConversationOrchestrator(store, WhaleCompanion(store, _Brain()))
    captured = {}
    orig = orch._decide

    def spy(env):
        captured["ctx"] = env.context
        return orig(env)

    orch._decide = spy
    orch.handle_envelope(InputEnvelope(conversation_id="c", text="hi"))
    assert captured["ctx"] is None


def test_authz_snapshot_persisted_for_tasks():
    """INV-09 前置：任务台账携带授权快照（主体/会话/scope）。"""
    store = InMemoryStore()
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, _Brain()), intent_model=_TaskPlanner(),
        tools=ToolRegistry(), context_resolver=make_resolver())
    orch.handle_envelope(InputEnvelope(
        conversation_id="private:10001", text="查一下", channel="qq",
        sender_id=OWNER_QQ))
    journals = [store.get(k) for k in store.keys()
                if k.startswith("runtime:task:")]
    assert journals, "任务台账应存在"
    authz = journals[0].get("authz")
    assert authz is not None
    assert authz["paired"] is True
    assert authz["conversation_key"] == "qq:qq_private:private:10001"
    assert "chat.basic" in authz["scopes"]
    assert "policy_version" in authz


def test_channels_owner_event_carries_sender():
    """S1：OwnerEvent 保留真实发送者（群里知道是谁说的）。"""
    reg = OwnerChannelRegistry(owner_id="owner", bindings=PAIRINGS)
    ev = reg.accept(Incoming(
        channel="qq", account=OWNER_QQ, conversation="group:123",
        message_id="m1", text="hi", transport_authenticated=True))
    assert ev is not None and ev.sender_id == OWNER_QQ


def test_channels_unpaired_sender_rejected_at_gate():
    reg = OwnerChannelRegistry(owner_id="owner", bindings=PAIRINGS)
    ev = reg.accept(Incoming(
        channel="qq", account="u_stranger", conversation="private:u",
        message_id="m1", text="hi", transport_authenticated=True))
    assert ev is None  # 入站门不变：未配对不进入 owner 流程
