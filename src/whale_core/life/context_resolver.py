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
    principal_matches_private_conversation,
)

# ---- 能力上限（§6.2 权限矩阵的首版落地） ------------------------------

#: 群会话上限：任何成员（含主人本人）在群里都不超过这个集合。
GROUP_CEILING: Tuple[str, ...] = (SCOPE_CHAT_BASIC, SCOPE_TOOL_WEB_SEARCH)

#: 未配对私聊：仅对话。
PRIVATE_ANON: Tuple[str, ...] = (SCOPE_CHAT_BASIC,)

#: 已配对主人私聊：对话 + 常规工具 + 提交出站 + 查看本人任务 + 设备通知。
#: F-01 红队修复：capability scope 在执行端真校验后，主人私聊必须显式
#: 保留其既有工具能力，否则生图/语音等回归。高风险动作（shell、支付、
#: 记忆写 vault）不在此列，仍需既有审批链（policy.py）。
PRIVATE_OWNER: Tuple[str, ...] = (
    SCOPE_CHAT_BASIC,
    SCOPE_TOOL_WEB_SEARCH,
    "tool.web.fetch",
    "tool.media.image.generate",
    "tool.media.video.generate",
    "tool.tts.synthesize",
    "tool.asr.transcribe",
    SCOPE_OUTBOX_SUBMIT,
    SCOPE_TASK_READ_SELF,
    SCOPE_DEVICE_NOTIFY,
)

#: 管理 API / CLI（持有本地 token 的通道）：等同主人私聊。
API_OWNER: Tuple[str, ...] = PRIVATE_OWNER

#: D3：screen.* 族只允许主人本人在非群会话持有（群聊硬上限拒绝，INV-03 一致）。
#: 注意它们不在 PRIVATE_OWNER 基础集里——默认最小权限（§5），只能经临时
#: 租约显式授予；本守卫保证租约也无法把它们带进群聊或非主人会话。
SCREEN_SCOPE_PREFIX = "screen."


class BindingLookup(Protocol):
    """会话绑定查询（S2/S5 提供真实存储；S1 允许为空实现）。

    返回的必须是**服务端已授权**的绑定；解析器会再做一道 vault 过滤。
    """

    def persona_for(self, conversation_key: str) -> Optional[str]: ...

    def memory_spaces_for(self, conversation_key: str) -> Tuple[MemorySpaceRef, ...]: ...

    def capabilities_for(self, conversation_key: str) -> Optional[Tuple[str, ...]]: ...


class GrantLookup(Protocol):
    """S4 提供：查询某主体在某会话中的有效临时租约。"""

    def active_grants(
        self, principal_id: str, conversation_key: str, *, now: float
    ) -> Tuple[TemporaryGrantRef, ...]: ...


class MemberMemoryLookup(Protocol):
    """G2 提供：查询某成员的 member_private 空间（绑定 + 有效 consent 双门）。

    返回 None 表示该成员当前没有可下发的个人空间（未授权 / 已撤销 /
    空间已注销）。本协议只读，永不抛错 widening 权限。
    """

    def member_space_for(self, principal_id: str) -> Optional[MemorySpaceRef]: ...


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
    if channel == "mobile":
        # D3：whale-mobile 设备面归一渠道；类型走 PHONE，基础 scope 仍由
        # _base_scopes 决定（默认最小权限，screen.* 须经租约显式授予）。
        return ConversationKind.PHONE
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
        owner_default_space: Optional[MemorySpaceRef] = None,
        member_memory: Optional[MemberMemoryLookup] = None,
        policy_version: int = 1,
    ) -> None:
        # pairing_lookup(channel, sender_id) -> owner_id | None
        self._pairing = pairing_lookup
        self._bindings = bindings
        self._grants = grants
        self._owner_id = owner_id
        # G2：成员个人记忆目录（PrincipalMemoryBinding + 有效 consent 双门）。
        self._member_memory = member_memory
        # 主人私聊/控制台等已认证**非群**会话的默认工作副本（S2 首版策略）。
        self._owner_default_space = owner_default_space
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
        # S7：会话级能力收窄（S5 bind:caps:）——只许收窄，不许放宽。
        if self._bindings is not None:
            caps_fn = getattr(self._bindings, "capabilities_for", None)
            if caps_fn is not None:
                narrowed = caps_fn(conv.as_key())
                if narrowed is not None:
                    scopes = tuple(s for s in scopes if s in set(narrowed))

        grant_refs: Tuple[TemporaryGrantRef, ...] = ()
        grant_scopes: Tuple[str, ...] = ()
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
                    # D3：screen.* 仅 owner 私聊 / API owner 可授；
                    # 群聊与非主人会话硬上限拒绝（INV-03 一致）。
                    if s.startswith(SCREEN_SCOPE_PREFIX) and (
                            _is_group(kind) or not principal.is_owner):
                        continue
                    if s not in scopes:
                        scopes = scopes + (s,)
                    if s not in grant_scopes:
                        grant_scopes = grant_scopes + (s,)

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
            # MEM-HARD-01（owner 决策 2026-11-01 修订）：protected_original
            # 只允许在主人本人私聊下发；其余一律剥离。
            owner_priv = principal.is_owner and not _is_group(kind)
            spaces = tuple(
                s
                for s in bound
                if s.type is not MemorySpaceType.PROTECTED_ORIGINAL
                or owner_priv
            )
        # 首版默认策略：已认证主人的非群会话自动挂上主人工作副本
        # （绑定为空时）；群会话永不享受此默认（INV-03）。
        if (
            self._owner_default_space is not None
            and principal.is_owner
            and not _is_group(kind)
            and self._owner_default_space.type is not MemorySpaceType.PROTECTED_ORIGINAL
            and all(
                s.space_id != self._owner_default_space.space_id for s in spaces)
        ):
            spaces = spaces + (self._owner_default_space,)

        # G2：成员本人已认证私聊 → 下发其 member_private 空间（MEM-03）。
        # 闸门全部在服务端：私聊且会话可证明属于该 sender（结构性证据，
        # 不看昵称/正文）→ 目录内有绑定 + 有效 consent。群聊永不进入此分支；
        # 群聊的 INV-03 过滤上面也已把 member_private 排除（纵深防御）。
        if (
            self._member_memory is not None
            and not _is_group(kind)
            and principal_matches_private_conversation(
                channel, kind, conversation_id, principal.principal_id,
                store=getattr(self._bindings, "store", None))
        ):
            try:
                member_ref = self._member_memory.member_space_for(
                    principal.principal_id)
            except Exception:
                member_ref = None  # 证据源故障 = 不下发（fail closed）
            if (
                member_ref is not None
                and member_ref.type is MemorySpaceType.MEMBER_PRIVATE
                and all(s.space_id != member_ref.space_id for s in spaces)
            ):
                spaces = spaces + (member_ref,)

        return ResolvedContext(
            conversation=conv,
            principal=principal,
            persona_id=persona_id,
            allowed_memory_spaces=spaces,
            capability_scopes=scopes,
            grants=grant_refs,
            grant_scopes=grant_scopes,
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
