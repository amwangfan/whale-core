"""Whale Studio 安全不变量断言集（S0 空壳）。

PRD §9 的 INV-01..INV-12 与验收 N01..N17 的测试骨架。
- 契约级断言（纯值对象）现在就必须通过；
- 依赖 S1..S5 实现的集成断言以 skip 占位，随阶段逐个点亮，
  不允许删除或改写为永远绿。

对应：docs/studio/whale-studio-prd-2026-10-31.md §9、§10
"""

from __future__ import annotations

import pytest

from whale_core.life.conversation_context import (
    ConversationKind,
    ConversationRef,
    MemorySpaceRef,
    MemorySpaceType,
    Principal,
    ResolvedContext,
    TemporaryGrantRef,
    SCOPE_CHAT_BASIC,
    scope_memory_read,
)

VAULT = MemorySpaceRef("vault-1", MemorySpaceType.PROTECTED_ORIGINAL, 1)
GROUP_SPACE = MemorySpaceRef("g-space-a", MemorySpaceType.GROUP_SPACE, 1)
OWNER_COPY = MemorySpaceRef("own-copy-1", MemorySpaceType.OWNER_WORKING_COPY, 1)

GROUP = ConversationRef("qq", ConversationKind.QQ_GROUP, "group:123")
PRIVATE = ConversationRef("qq", ConversationKind.QQ_PRIVATE, "u_stranger")

STRANGER = Principal("anon:qq:u_stranger", paired=False, is_owner=False, evidence="none")
OWNER = Principal("owner", paired=True, is_owner=True, evidence="owner_registry:qq:3899713055")


def _ctx(principal: Principal, conv: ConversationRef, spaces=()) -> ResolvedContext:
    return ResolvedContext(
        conversation=conv,
        principal=principal,
        persona_id=None,
        allowed_memory_spaces=tuple(spaces),
        capability_scopes=(SCOPE_CHAT_BASIC,),
    )


# ---------------------------------------------------------------------------
# INV-01 / MEM-HARD-01：protected_original 永不进入会话（契约级，现役）
# ---------------------------------------------------------------------------


def test_inv01_vault_never_readable_even_if_listed():
    """即使 allowed_memory_spaces 被错误地包含 vault，契约层仍拒绝。"""
    ctx = _ctx(OWNER, PRIVATE, spaces=[VAULT])  # 假设上游出错
    assert ctx.allows_memory_read(VAULT) is False


def test_inv01_vault_denied_for_owner_private_and_group():
    for principal in (OWNER, STRANGER):
        for conv in (PRIVATE, GROUP):
            assert _ctx(principal, conv).allows_memory_read(VAULT) is False


@pytest.mark.skip(reason="S2：需要 MemorySpace 存储与 API")
def test_inv01_api_bind_vault_returns_403():  # N01
    raise AssertionError


# ---------------------------------------------------------------------------
# INV-03：群里主人不提权（契约级 + S1 集成）
# ---------------------------------------------------------------------------


def test_inv03_owner_in_group_gets_no_extra_spaces():
    """同一主人，在群里与私聊的上下文由解析器分别构造；契约保证
    群上下文不自动包含主人私人副本（解析器 S1 负责，不在此断言）。"""
    ctx_group = _ctx(OWNER, GROUP, spaces=[GROUP_SPACE])
    assert ctx_group.allows_memory_read(OWNER_COPY) is False
    assert ctx_group.allows_memory_read(GROUP_SPACE) is True


@pytest.mark.skip(reason="S1：ConversationContextResolver 集成")
def test_inv03_resolver_group_owner_matrix():
    raise AssertionError


# ---------------------------------------------------------------------------
# INV-05：一次性口令语义（S4）
# ---------------------------------------------------------------------------


def test_inv05_grant_ref_separates_counts():
    g = TemporaryGrantRef(
        grant_id="g1", principal_id=STRANGER.principal_id,
        conversation_key=PRIVATE.as_key(), scopes=("task.read:self",),
        expires_at=0.0, max_uses=1, used_count=0, proof_method="otp",
    )
    assert g.max_uses != 0 and g.used_count == 0  # 租约次数独立字段存在


@pytest.mark.skip(reason="S4：OneTimeAuthCode 原子核销")
def test_inv05_code_single_success_under_concurrency():  # N06
    raise AssertionError


@pytest.mark.skip(reason="S4：租约绑定 sender+会话+指纹")
def test_inv05_grant_not_portable_across_conversations():  # N06/N16
    raise AssertionError


# ---------------------------------------------------------------------------
# 其余不变量（S2-S7 点亮）
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="S2：空间限域检索")
def test_inv08_no_cross_space_leak_via_cache_or_embedding():  # N02/N09
    raise AssertionError


@pytest.mark.skip(reason="S4+S7：异步任务恢复前重校验")
def test_inv09_expired_grant_blocks_resumed_task():  # N07
    raise AssertionError


@pytest.mark.skip(reason="S5：绑定 API 审计")
def test_inv10_audit_trail_for_grants_and_denials():
    raise AssertionError


@pytest.mark.skip(reason="S3：角色卡与权限解耦")
def test_inv07_persona_never_grants_privilege():  # N04/N11
    raise AssertionError
