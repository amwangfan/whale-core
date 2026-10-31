"""D3：screen.*/phone.* scope 常量、GRANTABLE_SCOPES 纳入与 resolver 矩阵。

矩阵规则（contracts/v1/PROTOCOL.md §5 + D3）：
- screen.* 仅 owner 私聊 / API owner 可授；群聊硬上限拒绝；非主人会话拒绝；
- 设备面 scope 不在任何基础 scope 集（默认最小权限），只能经租约授予；
- derive_kind 认识 channel="mobile"。
"""

from __future__ import annotations

import time

from whale_core.life.conversation_context import (
    ConversationKind,
    DEVICE_SCOPES,
    SCOPE_PHONE_CALL_AUDIO,
    SCOPE_PHONE_CALL_RING,
    SCOPE_PHONE_DEVICE_SAFE_ACTION,
    SCOPE_PHONE_NOTIFICATION_POST,
    SCOPE_PHONE_NOTIFICATION_READ,
    SCOPE_SCREEN_CONTEXT_READ,
    SCOPE_SCREEN_EVIDENCE_READ,
    SCOPE_SCREEN_HISTORY_SEARCH,
    SCOPE_SCREEN_LIVE_SUBSCRIBE,
    TemporaryGrantRef,
)
from whale_core.life.context_resolver import (
    ConversationContextResolver,
    derive_kind,
)
from whale_core.life.temporary_auth import GRANTABLE_SCOPES


def test_constants_match_protocol_names():
    assert SCOPE_SCREEN_CONTEXT_READ == "screen.context.read"
    assert SCOPE_SCREEN_HISTORY_SEARCH == "screen.history.search"
    assert SCOPE_SCREEN_EVIDENCE_READ == "screen.evidence.read"
    assert SCOPE_SCREEN_LIVE_SUBSCRIBE == "screen.live.subscribe"
    assert SCOPE_PHONE_NOTIFICATION_POST == "phone.notification.post"
    assert SCOPE_PHONE_NOTIFICATION_READ == "phone.notification.read"
    assert SCOPE_PHONE_CALL_RING == "phone.call.ring"
    assert SCOPE_PHONE_CALL_AUDIO == "phone.call.audio"
    assert SCOPE_PHONE_DEVICE_SAFE_ACTION == "phone.device.safe_action"
    assert len(DEVICE_SCOPES) == 9


def test_device_scopes_in_grantable():
    from whale_core.life.temporary_auth import GRANTABLE_SCOPES
    for scope in DEVICE_SCOPES:
        assert scope in GRANTABLE_SCOPES


def test_device_scopes_not_in_base_scopes():
    """默认最小权限：owner 私聊/API 基础集不含任何设备面 scope。"""
    resolver = ConversationContextResolver(
        pairing_lookup=lambda channel, sender: "owner")
    for channel, cid in (("qq", "private:u1"), ("api", "api")):
        ctx = resolver.resolve(channel=channel, sender_id="u1",
                               conversation_id=channel)
        for scope in DEVICE_SCOPES:
            assert scope not in ctx.capability_scopes


class _Grants:
    def __init__(self, scopes):
        self._scopes = scopes

    def active_grants(self, principal_id, conversation_key, *, now):
        return (TemporaryGrantRef(
            grant_id="g1", principal_id=principal_id,
            conversation_key=conversation_key, scopes=tuple(self._scopes),
            expires_at=now + 600, max_uses=5, used_count=0,
            proof_method="otp"),)


def _resolver_with_grant(scopes, *, owner: bool):
    return ConversationContextResolver(
        pairing_lookup=(lambda channel, sender: "owner") if owner else None,
        grants=_Grants(scopes))


def test_screen_scope_grant_allowed_for_owner_private():
    resolver = _resolver_with_grant([SCOPE_SCREEN_CONTEXT_READ], owner=True)
    ctx = resolver.resolve(channel="qq", sender_id="u1",
                           conversation_id="private:u1")
    assert SCOPE_SCREEN_CONTEXT_READ in ctx.capability_scopes


def test_screen_scope_grant_allowed_for_api_owner():
    resolver = _resolver_with_grant([SCOPE_SCREEN_HISTORY_SEARCH], owner=True)
    ctx = resolver.resolve(channel="api", sender_id="console",
                           conversation_id="api")
    assert SCOPE_SCREEN_HISTORY_SEARCH in ctx.capability_scopes


def test_screen_scope_grant_hard_denied_in_group():
    """群聊硬上限：即使发送者是主人且持有效租约也不授予。"""
    resolver = _resolver_with_grant([SCOPE_SCREEN_CONTEXT_READ], owner=True)
    ctx = resolver.resolve(channel="qq", sender_id="u1",
                           conversation_id="group:g1")
    assert SCOPE_SCREEN_CONTEXT_READ not in ctx.capability_scopes
    assert ctx.conversation.kind is ConversationKind.QQ_GROUP


def test_screen_scope_grant_denied_for_non_owner():
    resolver = _resolver_with_grant([SCOPE_SCREEN_CONTEXT_READ], owner=False)
    ctx = resolver.resolve(channel="qq", sender_id="anon",
                           conversation_id="private:anon")
    assert SCOPE_SCREEN_CONTEXT_READ not in ctx.capability_scopes


def test_phone_scope_grant_follows_group_ceiling():
    """phone.* 不受 screen 专守限制，但仍受群上限（GROUP_CEILING）约束。"""
    resolver = _resolver_with_grant([SCOPE_PHONE_NOTIFICATION_POST], owner=True)
    private = resolver.resolve(channel="qq", sender_id="u1",
                               conversation_id="private:u1")
    assert SCOPE_PHONE_NOTIFICATION_POST in private.capability_scopes
    group = resolver.resolve(channel="qq", sender_id="u1",
                             conversation_id="group:g1")
    assert SCOPE_PHONE_NOTIFICATION_POST not in group.capability_scopes


def test_derive_kind_mobile():
    assert derive_kind("mobile", "dev-1") is ConversationKind.PHONE
    # mobile 会话基础 scope 最小（设备认证 ≠ 主人认证）
    resolver = ConversationContextResolver(
        pairing_lookup=lambda channel, sender: "owner")
    ctx = resolver.resolve(channel="mobile", sender_id="dev",
                           conversation_id="dev-1")
    for scope in DEVICE_SCOPES:
        assert scope not in ctx.capability_scopes
