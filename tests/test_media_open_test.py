"""WHALE_MEDIA_OPEN_TEST=1 测试期开放（owner 2026-11-01）。

测试期：群会话与陌生人/成员私聊也获得媒体生成 scope（图片/视频/语音）；
默认（未开环境变量）行为完全不变。开放只加媒体 scope——高风险动作仍走
审批链，频控/配额/静默照常。
"""

from __future__ import annotations

from whale_core.life.context_resolver import (
    GROUP_CEILING,
    MEDIA_GENERATION_SCOPES,
    PRIVATE_ANON,
    PRIVATE_OWNER,
    ConversationContextResolver,
    pairing_lookup_from_mapping,
)

OWNER_QQ = "10001"
PAIRINGS = {("qq", OWNER_QQ): "owner"}


def make_resolver() -> ConversationContextResolver:
    return ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping(PAIRINGS), owner_id="owner")


def test_default_closed_stranger_private(monkeypatch):
    monkeypatch.delenv("WHALE_MEDIA_OPEN_TEST", raising=False)
    ctx = make_resolver().resolve(
        channel="qq", sender_id="stranger-1", conversation_id="private:u_x")
    assert ctx.capability_scopes == PRIVATE_ANON
    for s in MEDIA_GENERATION_SCOPES:
        assert s not in ctx.capability_scopes


def test_default_closed_group(monkeypatch):
    monkeypatch.delenv("WHALE_MEDIA_OPEN_TEST", raising=False)
    ctx = make_resolver().resolve(
        channel="qq", sender_id="member-1", conversation_id="group:1124744261")
    assert ctx.capability_scopes == GROUP_CEILING
    for s in MEDIA_GENERATION_SCOPES:
        assert s not in ctx.capability_scopes


def test_open_stranger_private_gets_media(monkeypatch):
    monkeypatch.setenv("WHALE_MEDIA_OPEN_TEST", "1")
    ctx = make_resolver().resolve(
        channel="qq", sender_id="stranger-1", conversation_id="private:u_x")
    for s in MEDIA_GENERATION_SCOPES:
        assert s in ctx.capability_scopes
    # 但不获得主人其它能力
    assert "tool.web.fetch" not in ctx.capability_scopes
    assert "outbox.submit" not in ctx.capability_scopes


def test_open_group_ceiling_gets_media(monkeypatch):
    monkeypatch.setenv("WHALE_MEDIA_OPEN_TEST", "1")
    for sender in ("member-1", OWNER_QQ):  # 群里含主人也按同一上限
        ctx = make_resolver().resolve(
            channel="qq", sender_id=sender, conversation_id="group:1124744261")
        for s in MEDIA_GENERATION_SCOPES:
            assert s in ctx.capability_scopes
        assert "outbox.submit" not in ctx.capability_scopes


def test_open_owner_private_unchanged(monkeypatch):
    """主人私聊本就全量；开关不产生重复也不减损。"""
    monkeypatch.setenv("WHALE_MEDIA_OPEN_TEST", "1")
    ctx = make_resolver().resolve(
        channel="qq", sender_id=OWNER_QQ, conversation_id="private:u_owner")
    assert set(PRIVATE_OWNER) <= set(ctx.capability_scopes)
    assert len(ctx.capability_scopes) == len(set(ctx.capability_scopes))
