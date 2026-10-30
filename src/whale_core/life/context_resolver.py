"""S1：ConversationContextResolver —— 服务端会话身份与权限解析。

规则（PRD §6.2，全部在服务端执行）：

- **配对证据**只来自可信来源（OwnerRegistry / 宿主注入的配对表）；
  昵称、消息正文、客户端字段一律不构成身份证明（INV-04）。
- **群里主人不提权**：QQ 群等群会话中，即使发送者是已配对主人，
  能力也不超过群上限，且不得包含任何私人记忆空间（INV-03）。
- **最小权限默认**：未配对发送者 / 未知渠道 = 普通主体，仅 ``chat.basic``。
- **临时授权**：只从 GrantStore 读取服务端签发且未过期/未撤销的租约；
  本解析器不接受任何调用方提供的 scope / grant 参数。

依赖方向：life → life（conversation_context）。本模块永不 import core。
"""

from __future__ import annotations

import time
from typing import Callable, Mapping, Optional, Protocol, Tuple

from whale_core.life.conversation_context import (
    ConversationKind,
    ConversationRef,
    MemorySpaceRef,
    MemorySpaceType,
    Principal,
    ResolvedContext,
    TemporaryGrantRef,
    SCOPE_CHAT_BASIC,
    SCOPE_DEVICE_NOTIFY,
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TASK_READ_SELF,
    SCOPE_TOOL_WEB_SEARCH,
)

# ---- 能力上限（§6.2 权限矩阵的首版落地） ------------------------------

#: 群会话上限：任何成员（含主人本人）在群里都不超过这个集合。
GROUP_CEILING: Tuple[str, ...] = (SCOPE_CHAT_BASIC, SCOPE_TOOL_WEB_SEARCH)

#: 未配对私聊：仅对话。
PRIVATE_ANON: Tuple[str, ...] = (SCOPE_CHAT_BASIC,)

#: 已配对主人私聊：对话 + 常规工具 + 提交出站 + 查看本人任务 + 设备通知。
#: 高风险动作（shell、记忆写 vault、支付）不在此列，仍需逐项审批（policy.py）。
PRIVATE_OWNER: Tuple[str, ...] = (
    SCOPE_CHAT_BASIC,
    SCOPE_TOOL_WEB_SEARCH,
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TASK_READ_SELF,
    SCOPE_DEVICE_NOTIFY,
)

#: 管理 API / CLI（持有本地 token 的通道）：等同主人私聊。
API_OWNER: Tuple[str, ...] = PRIVATE_OWNER


class BindingLookup(Protocol):
    """会话绑定查询（S2/S5 提供真实存储；S1 允许为空实现）。

    返回的必须是**服务端已授权**的绑定；解析器会再做一道 vault 过滤。
    """

    def persona_for(self, conversation_key: str) -> Optional[str]: ...

    def memory_spaces_for(self, conversation_key: str) -> Tuple[MemorySpaceRef, ...]: ...


class GrantLookup(Protocol):
    """S4 提供：查询某主体在某会话中的有效临时租约。"""

    def active_grants(
        self, principal_id: str, conversation_key: str, *, now: float
    ) -> Tuple[TemporaryGrantRef, ...]: ...


def derive_kind(channel: str, conversation_id: str) -> ConversationKind:
    """从渠道与现有 conversation 字符串推导会话类型。

    适配器现状：QQ 拼 ``private:{uid}`` / ``group:{gid}``（qq_onebot.py）；
    这里做显式归一，不要求适配器改格式。
    """
    cid = conversation_id or ""
    if channel == "qq":
        if cid.startswith("group:"):
            return ConversationKind.QQ_GROUP
        if cid.startswith("private:"):
            return ConversationKind.QQ_PRIVATE
    if channel == "web":
        return ConversationKind.WEB
    if channel in ("voice", "phone"):
        return ConversationKind.VOICE
    if channel in ("api", "cli"):
        return ConversationKind.API
    return ConversationKind.UNKNOWN


class ConversationContextResolver:
    """每条输入进入模型/记忆/工具前的唯一上下文建立点。"""

    def __init__(
        self,
        *,
        pairing_lookup: Optional[Callable[[str, str], Optional[str]]] = None,
        bindings: Optional[BindingLookup] = None,
        grants: Optional[GrantLookup] = None,
        owner_id: str = "owner",
        policy_version: int = 1,
    ) -> None:
        # pairing_lookup(channel, sender_id) -> owner_id | None
        self._pairing = pairing_lookup
        self._bindings = bindings
        self._grants = grants
        self._owner_id = owner_id
        self._policy_version = policy_version

    # -- 主入口 --------------------------------------------------------

    def resolve(
        self,
        *,
        channel: str,
        sender_id: str,
        conversation_id: str,
        conversation_kind: Optional[ConversationKind] = None,
    ) -> ResolvedContext:
        """为一条输入建立不可变授权上下文。

        只接受渠道级事实（channel/sender/conversation）；权限信息一律由
        服务端内部查询得出，调用方无法通过本接口注入 scope 或 grant。
        """
        kind = conversation_kind or derive_kind(channel, conversation_id)
        conv = ConversationRef(channel, kind, conversation_id)
        principal = self._resolve_principal(channel, sender_id)
        scopes = self._base_scopes(kind, principal)

        grant_refs: Tuple[TemporaryGrantRef, ...] = ()
        if self._grants is not None:
            now = time.time()
            grant_refs = tuple(
                g
                for g in self._grants.active_grants(
                    principal.principal_id, conv.as_key(), now=now
                )
                if not g.revoked and g.expires_at > now and g.used_count < g.max_uses
            )
            # 租约 scope 也不能突破会话类型上限（群里租约不提权）。
            ceiling = GROUP_CEILING if _is_group(kind) else None
            for g in grant_refs:
                for s in g.scopes:
                    if ceiling is not None and s not in ceiling:
                        continue
                    if s not in scopes:
                        scopes = scopes + (s,)

        spaces: Tuple[MemorySpaceRef, ...] = ()
        persona_id: Optional[str] = None
        if self._bindings is not None:
            persona_id = self._bindings.persona_for(conv.as_key())
            bound = self._bindings.memory_spaces_for(conv.as_key())
            if _is_group(kind):
                # INV-03：群会话不得绑定任何主人私人副本 / 原始库。
                bound = tuple(
                    s
                    for s in bound
                    if s.type
                    in (MemorySpaceType.GROUP_SPACE, MemorySpaceType.SHAREABLE_FORK)
                )
            # MEM-HARD-01：无论绑定配置如何，protected_original 永不下发。
            spaces = tuple(
                s
                for s in bound
                if s.type is not MemorySpaceType.PROTECTED_ORIGINAL
            )

        return ResolvedContext(
            conversation=conv,
            principal=principal,
            persona_id=persona_id,
            allowed_memory_spaces=spaces,
            capability_scopes=scopes,
            grants=grant_refs,
            policy_version=self._policy_version,
        )

    # -- 内部 ----------------------------------------------------------

    def _resolve_principal(self, channel: str, sender_id: str) -> Principal:
        owner = None
        if self._pairing is not None and sender_id:
            try:
                owner = self._pairing(channel, sender_id)
            except Exception:
                owner = None  # 证据源故障 = 未认证（fail closed）
        if owner:
            return Principal(
                principal_id=f"paired:{channel}:{sender_id}",
                paired=True,
                is_owner=True,
                evidence=f"pairing:{channel}",
            )
        return Principal(
            principal_id=f"anon:{channel}:{sender_id or 'unknown'}",
            paired=False,
            is_owner=False,
            evidence="none",
        )

    def _base_scopes(
        self, kind: ConversationKind, principal: Principal
    ) -> Tuple[str, ...]:
        if _is_group(kind):
            return GROUP_CEILING  # INV-03：主人在群里同样受限
        if kind is ConversationKind.API:
            return API_OWNER if principal.is_owner else PRIVATE_ANON
        if kind in (ConversationKind.QQ_PRIVATE, ConversationKind.WEB):
            return PRIVATE_OWNER if principal.is_owner else PRIVATE_ANON
        if kind is ConversationKind.VOICE:
            return PRIVATE_OWNER if principal.is_owner else PRIVATE_ANON
        return (SCOPE_CHAT_BASIC,)


def _is_group(kind: ConversationKind) -> bool:
    return kind is ConversationKind.QQ_GROUP


def pairing_lookup_from_mapping(
    pairings: Mapping[tuple, str],
) -> Callable[[str, str], Optional[str]]:
    """把 ``{(channel, sender_id): owner_id}`` 映射适配成 pairing_lookup。

    OwnerRegistry.resolve 的签名本就兼容，可直接传入；本助手用于宿主
    已持有内存配对表（如 qq_live_workflow 的 bindings）的场景。
    """
    table = dict(pairings)
    return lambda channel, sender_id: table.get((channel, sender_id))
