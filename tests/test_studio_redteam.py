"""红队审查（F-01/02/03/05/06）回归测试。

对应 docs/studio 威胁模型与红队报告（2026-10-31）。每项断言一个曾被
实证可利用或存在失守面的修复点。
"""

from __future__ import annotations

import json

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import (
    ConversationOrchestrator, InputEnvelope, ToolRegistry, ToolSpec)
from whale_core.life.channels import OwnerEvent
from whale_core.life.context_resolver import (
    ConversationContextResolver, pairing_lookup_from_mapping)
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.life.temporary_auth import (
    TemporaryAuthError, TemporaryAuthService, maybe_intercept_sudo)
from whale_core.memory.spaces import SpaceStore, ensure_owner_working_copy, \
    migrate_legacy_to_vault
from whale_core.memory.bindings import BindingStore
from whale_core.memory.store import InMemoryStore, MemoryKind, MemoryRecord


class Brain:
    def __init__(self):
        self.calls = []

    def reply(self, text, **kwargs):
        self.calls.append(kwargs)
        return "收到"


class SearchPlanner:
    def plan(self, text, **kwargs):
        return json.dumps({"goal": "搜", "kind": "task",
                           "capabilities": ("web.search",), "slots": {"q": "x"}})


def _orch(store, *, pairings=(), grants=None):
    spaces = SpaceStore(store)
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping(dict(pairings)),
        bindings=__import__("whale_core.memory.bindings", fromlist=["BindingStore"]).BindingStore(store, spaces),
        grants=grants, owner_id="owner",
        owner_default_space=ensure_owner_working_copy(store))
    calls = []
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, Brain()), intent_model=SearchPlanner(),
        tools=ToolRegistry(), context_resolver=resolver,
        grant_gate=(lambda g, fp, scope=None: all(
            grants.consume_grant_use(x, action_fingerprint=fp,
                                     required_scope=scope) for x in g)
            if grants else None))
    orch.tools.register(ToolSpec("search", "web.search", risk_class="read_only",
                                 handler=lambda a: calls.append(a) or "结果"))
    return orch, calls


# ---------------------------------------------------------------------------
# F-01：capability scope 执行端强制
# ---------------------------------------------------------------------------


def test_f01_stranger_cannot_run_web_search():
    """陌生人（scope 仅 chat.basic）触发 web.search → 执行端拒绝。"""
    store = InMemoryStore()
    orch, calls = _orch(store)
    result = orch.handle_envelope(InputEnvelope(
        conversation_id="private:u_eve", text="搜一下", channel="qq",
        sender_id="u_eve"))
    assert calls == []  # 工具从未执行
    assert any("scope-tool.web.search" in r for r in result.receipts)


def test_f01_owner_private_keeps_tool_access():
    """主人私聊 scope 覆盖 tool.web.search → 正常执行（防回归）。"""
    store = InMemoryStore()
    orch, calls = _orch(store, pairings={("qq", "10001"): "owner"})
    result = orch.handle_envelope(InputEnvelope(
        conversation_id="private:10001", text="搜一下", channel="qq",
        sender_id="10001"))
    assert calls, "主人工具执行被误拦（F-01 修复不得回归 owner 能力）"


def test_f01_grant_with_tool_scope_unlocks_once():
    """F-02：租约 scope 覆盖 web.search 时可放行并消耗次数；chat.basic 不行。"""
    store = InMemoryStore()
    auth = TemporaryAuthService(store)
    code = auth.create_code(allowed_scopes=("tool.web.search",), grant_max_uses=1)
    grant = auth.verify(code, channel="qq", sender_id="u_eve",
                        conversation_kind=__import__(
                            "whale_core.life.conversation_context",
                            fromlist=["ConversationKind"]).ConversationKind.QQ_PRIVATE,
                        conversation_id="private:u_eve")
    orch, calls = _orch(store, grants=auth)
    # 陌生人+租约：第一次执行成功（租约补足 scope）
    r1 = orch.handle_envelope(InputEnvelope(
        conversation_id="private:u_eve", text="搜", channel="qq",
        sender_id="u_eve"))
    assert calls, "带 tool.web.search 租约应放行"
    # 第二次：租约次数已用尽 → 拒绝
    calls.clear()
    orch.handle_envelope(InputEnvelope(
        conversation_id="private:u_eve", text="再搜", channel="qq",
        sender_id="u_eve"))
    assert calls == []


def test_f02_chat_basic_grant_cannot_authorize_tool():
    auth = TemporaryAuthService(InMemoryStore())
    code = auth.create_code(allowed_scopes=("task.read:self",))
    grant = auth.verify(code, channel="qq", sender_id="u",
                        conversation_kind=__import__(
                            "whale_core.life.conversation_context",
                            fromlist=["ConversationKind"]).ConversationKind.QQ_PRIVATE,
                        conversation_id="private:u")
    assert auth.consume_grant_use(grant.grant_id,
                                  required_scope="tool.web.search") is False
    # 拒绝不烧次数
    assert auth.consume_grant_use(grant.grant_id,
                                  required_scope="task.read:self") is True


# ---------------------------------------------------------------------------
# F-03：sudo 拦截归一化
# ---------------------------------------------------------------------------


def test_f03_sudo_case_and_fullwidth_variants_intercepted():
    for text in ("sudo abc", "Sudo abc", "SUDO abc", "sudo　abc", "  SuDo abc"):
        assert maybe_intercept_sudo(text) == "abc", text
    assert maybe_intercept_sudo("sudo") == ""
    assert maybe_intercept_sudo("说 sudo 的事") is None


def _prefs():
    from whale_core.life.initiative import ProactivePrefs
    return ProactivePrefs(enabled=True,
                          allowed_channels=frozenset({"direct_message"}))


def test_f03_sudo_in_group_silently_ignored():
    store = InMemoryStore()
    auth = TemporaryAuthService(store)
    wf = QqInboundWorkflow(store, Brain(), auth_service=auth, prefs=_prefs())
    out = wf.handle_owner_event(OwnerEvent(
        owner_id="owner", channel="qq", conversation="group:9",
        message_id="m1", text="sudo 随便什么", sender_id="10001"))
    assert out.get("skipped") == "sudo_in_group_ignored"
    assert "reply" not in out  # 群里无回执草稿


# ---------------------------------------------------------------------------
# F-05：人格连续性快照不进 vault
# ---------------------------------------------------------------------------


def test_f05_migration_excludes_persona_continuity():
    from whale_core.memory.store import PersonaContinuity
    store = InMemoryStore()
    store.put_persona_continuity(PersonaContinuity(persona_name="鲸鱼"))
    store.put_record(MemoryRecord(kind=MemoryKind.EPISODE, key="ep:1",
                                  content={"summary": "私人情节"}))
    migrate_legacy_to_vault(store)
    assert store.get_record("persona:default").space_id == ""  # 人格留全局基线
    assert store.get_record("ep:1").space_id == "vault-default"


# ---------------------------------------------------------------------------
# F-06：回落路径限域
# ---------------------------------------------------------------------------


def test_f06_fallback_chat_uses_context():
    """无编排器时，companion 回落也必须收到 ctx（不跨空间召回）。"""
    store = InMemoryStore()
    spaces = SpaceStore(store)
    own = ensure_owner_working_copy(store)
    resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping({("qq", "10001"): "owner"}),
        bindings=BindingStore(store, spaces), owner_id="owner",
        owner_default_space=own)
    brain = Brain()
    wf = QqInboundWorkflow(store, brain, context_resolver=resolver,
                           prefs=_prefs())
    # 别的空间有一条秘密事实
    store.put_record(MemoryRecord(
        kind=MemoryKind.PREFERENCE, key="other-space-fact",
        content={"summary": "别群秘密", "stable_fact": True, "importance": 0.9,
                 "confidence": 1.0},
        tags=("curated", "stable"), space_id="g-other"))
    out = wf.handle_owner_event(OwnerEvent(
        owner_id="owner", channel="qq", conversation="private:10001",
        message_id="m2", text="聊聊", sender_id="10001"))
    assert out.get("reply")
    assert not any("别群秘密" in str(m) for m in brain.calls[-1]["memories"])


# ---------------------------------------------------------------------------
# F-08：弱口令门槛
# ---------------------------------------------------------------------------


def test_f08_short_or_common_phrase_rejected():
    auth = TemporaryAuthService(InMemoryStore())
    for weak in ("123456", "1234567890", "password1"):
        try:
            auth.create_code(custom_phrase=weak,
                             allowed_scopes=("task.read:self",))
            raise AssertionError(f"弱口令不应被接受: {weak}")
        except TemporaryAuthError:
            pass
