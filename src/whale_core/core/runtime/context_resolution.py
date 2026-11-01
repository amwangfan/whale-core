"""服务端会话上下文解析（issue #30 拆分，S1/S2；INV-04/06/08/09）。

- ``ContextResolutionMixin._ensure_context``：注入 resolver 时，任何调用方
  随信封携带的 ``context`` 都被覆盖——权限上下文只能来自服务端解析；
- ``_ctx_spaces`` / ``_ctx_write_space``：授权空间提取与默认写回空间；
- ``_authz_snapshot``：可持久化授权快照（只存元数据，不存正文或秘密）。
"""
from __future__ import annotations

from dataclasses import replace

from whale_core.core.runtime.envelope import InputEnvelope


def _ctx_spaces(context):
    """从 ResolvedContext 提取 MemorySpaceRef 元组；无上下文返回 None（兼容期）。"""
    if context is None:
        return None
    spaces = getattr(context, "allowed_memory_spaces", None)
    return tuple(spaces) if spaces is not None else None


def _ctx_write_space(context):
    """从 ResolvedContext 计算默认写回空间；无上下文返回 None（兼容期）。"""
    if context is None:
        return None
    spaces = getattr(context, "allowed_memory_spaces", None)
    if spaces is None:
        return None
    from whale_core.memory.spaces import default_write_space
    return default_write_space(tuple(spaces))


def _authz_snapshot(context) -> dict | None:
    """S1：从 ResolvedContext 提取可持久化的授权快照（INV-09）。

    只存主体/会话/scope/策略版本等元数据——不存任何正文或秘密。
    非 ResolvedContext（或缺失字段）返回 None，保持旧行为。
    """
    if context is None:
        return None
    try:
        from whale_core.memory.spaces import default_write_space
        conversation = context.conversation
        principal = context.principal
        return {
            "principal_id": principal.principal_id,
            "paired": bool(principal.paired),
            "conversation_key": conversation.as_key(),
            "scopes": sorted(
                set(context.capability_scopes)
                - set(getattr(context, "grant_scopes", ()))),
            "grant_scopes": sorted(getattr(context, "grant_scopes", ())),
            "memory_spaces": sorted(
                s.space_id for s in context.allowed_memory_spaces),
            "write_space": default_write_space(context.allowed_memory_spaces),
            "grant_ids": sorted(g.grant_id for g in context.grants),
            "persona_id": context.persona_id,
            "policy_version": context.policy_version,
        }
    except AttributeError:
        return None


class ContextResolutionMixin:
    """上下文解析职责（编排器 mixin；状态由 ConversationOrchestrator 提供）。"""

    def _ensure_context(self, env: InputEnvelope) -> InputEnvelope:
        """S1：服务端重解析授权上下文。

        注入 resolver 时，任何调用方随信封携带的 ``context`` 都被覆盖——
        权限上下文只能来自服务端解析（INV-04/06/08）；未注入 resolver 的
        旧宿主保持原行为（兼容期）。
        """
        if self.context_resolver is None:
            return env
        channel = env.channel or ("api" if env.source == "api" else env.source)
        sender_id = env.sender_id
        if not sender_id and env.source in ("runtime", "cli"):
            # 本机宿主入口（无渠道 sender）：映射为受信本地管理主体，
            # 由配对表（cli/local → owner）决定身份（F-01 修复的回归保护）。
            channel = "cli"
            sender_id = "local"
        ctx = self.context_resolver.resolve(
            channel=channel,
            sender_id=sender_id,
            conversation_id=env.conversation_id,
        )
        return replace(env, context=ctx)
