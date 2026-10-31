"""Whale Studio 领域契约（S0 冻结版）。

本模块只定义**不可变值对象**与类型，不含任何 IO / 鉴权实现。
实现（S1 起）必须遵守：

- 所有身份证据来自可信渠道适配器；昵称、消息正文、客户端字段一律不构成证明。
- ``ResolvedContext`` 由服务端在调用模型 / 记忆 / 工具之前建立，外部不得直接构造提交
  （构造点必须位于宿主进程内部的解析器，见 ``ConversationContextResolver`` 协议）。
- 任何 ``protected_original`` 记忆空间永不进入会话召回路径（INV-01）。

对应文档：docs/studio/whale-studio-prd-2026-10-31.md §7.1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# 会话与主体
# ---------------------------------------------------------------------------


class ConversationKind(str, Enum):
    """可信渠道归一化后的会话类型。"""

    QQ_GROUP = "qq_group"
    QQ_PRIVATE = "qq_private"
    WEB = "web"
    VOICE = "voice"
    PHONE = "phone"
    API = "api"  # 管理 API / CLI 入口
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConversationRef:
    """一条会话的稳定标识。kind 必须与渠道实际语义一致，禁止由正文推断。"""

    channel: str  # e.g. "qq" / "web" / "voice"
    kind: ConversationKind
    conversation_id: str  # 群号 / 私聊对端 uid / web 会话 id

    def as_key(self) -> str:
        return f"{self.channel}:{self.kind.value}:{self.conversation_id}"


@dataclass(frozen=True)
class Principal:
    """已认证的逻辑主体。未认证发送者也是 Principal（paired=False）。"""

    principal_id: str  # 稳定内部 ID；未认证时为 "anon:<channel>:<sender_id>"
    paired: bool  # 是否完成可信配对（OwnerRegistry 等）
    is_owner: bool  # 是否主人账号（仅可信配对证据得出）
    evidence: str  # 证据来源描述，如 "owner_registry:qq:3899713055"；禁止记录秘密


# ---------------------------------------------------------------------------
# 记忆空间
# ---------------------------------------------------------------------------


class MemorySpaceType(str, Enum):
    PROTECTED_ORIGINAL = "protected_original"  # 永不直连会话（MEM-HARD-01）
    OWNER_WORKING_COPY = "owner_working_copy"
    GROUP_SPACE = "group_space"
    SHAREABLE_FORK = "shareable_fork"


@dataclass(frozen=True)
class MemorySpaceRef:
    space_id: str
    type: MemorySpaceType
    version: int  # 缓存分区键的一部分
    can_write: bool = False  # 会话绑定实际写权限；类型本身不构成授权


# ---------------------------------------------------------------------------
# 能力作用域与临时授权
# ---------------------------------------------------------------------------

# 细粒度能力命名（§6.2）。敏感动作禁止靠全局 is_owner 放行。
SCOPE_CHAT_BASIC = "chat.basic"
SCOPE_OUTBOX_SUBMIT = "outbox.submit"
SCOPE_TOOL_WEB_SEARCH = "tool.web.search"
SCOPE_TOOL_SHELL_SAFE = "tool.shell.safe"
SCOPE_TASK_READ_SELF = "task.read:self"
SCOPE_DEVICE_NOTIFY = "device.notify"

# ---- 设备面（whale-mobile，D3）scope 族 --------------------------------
# 命名与 contracts/v1/PROTOCOL.md §5 能力枚举逐字一致；裁决矩阵见
# context_resolver（screen.* 仅 owner 私聊 / API owner 可授，群聊硬上限拒绝）。
SCOPE_SCREEN_CONTEXT_READ = "screen.context.read"
SCOPE_SCREEN_HISTORY_SEARCH = "screen.history.search"
SCOPE_SCREEN_EVIDENCE_READ = "screen.evidence.read"
SCOPE_SCREEN_LIVE_SUBSCRIBE = "screen.live.subscribe"
SCOPE_PHONE_NOTIFICATION_POST = "phone.notification.post"
SCOPE_PHONE_NOTIFICATION_READ = "phone.notification.read"
SCOPE_PHONE_CALL_RING = "phone.call.ring"
SCOPE_PHONE_CALL_AUDIO = "phone.call.audio"
SCOPE_PHONE_DEVICE_SAFE_ACTION = "phone.device.safe_action"

#: 设备面 scope 全集（screen.* + phone.*）。
DEVICE_SCOPES: Tuple[str, ...] = (
    SCOPE_SCREEN_CONTEXT_READ,
    SCOPE_SCREEN_HISTORY_SEARCH,
    SCOPE_SCREEN_EVIDENCE_READ,
    SCOPE_SCREEN_LIVE_SUBSCRIBE,
    SCOPE_PHONE_NOTIFICATION_POST,
    SCOPE_PHONE_NOTIFICATION_READ,
    SCOPE_PHONE_CALL_RING,
    SCOPE_PHONE_CALL_AUDIO,
    SCOPE_PHONE_DEVICE_SAFE_ACTION,
)


def scope_memory_read(space_id: str) -> str:
    return f"memory.read:{space_id}"


def scope_memory_write(space_id: str) -> str:
    return f"memory.write:{space_id}"


@dataclass(frozen=True)
class TemporaryGrantRef:
    """已签发临时租约的只读引用（租约本体与校验在 S4 的服务端存储中）。"""

    grant_id: str
    principal_id: str
    conversation_key: str  # ConversationRef.as_key()
    scopes: Tuple[str, ...]
    expires_at: float  # epoch 秒
    max_uses: int  # 操作次数（与口令次数分账，INV-05）
    used_count: int
    proof_method: str  # "otp" / "device" / "totp"
    revoked: bool = False


# ---------------------------------------------------------------------------
# ResolvedContext：单消息不可变授权上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedContext:
    """服务端为单条输入建立的只读权限上下文。

    构建者必须是宿主内部的 resolver；模型、角色卡、消息正文、HTTP 客户端
    均不得直接提供本对象或其字段来换取权限（INV-04/06/08）。
    """

    conversation: ConversationRef
    principal: Principal
    persona_id: Optional[str]  # 会话绑定角色；None = 低负担默认人格
    allowed_memory_spaces: Tuple[MemorySpaceRef, ...] = ()
    capability_scopes: Tuple[str, ...] = (SCOPE_CHAT_BASIC,)
    grants: Tuple[TemporaryGrantRef, ...] = ()
    # 其中由临时租约贡献的 scope 子集：这些能力每次执行都要经 grant_gate
    # 消耗操作次数（F-01/红队：租约能力不是免费基础能力）。
    grant_scopes: Tuple[str, ...] = ()
    policy_version: int = 1

    def allows_memory_read(self, space: MemorySpaceRef) -> bool:
        if space.type is MemorySpaceType.PROTECTED_ORIGINAL:
            return False  # MEM-HARD-01，无例外
        return any(s.space_id == space.space_id for s in self.allowed_memory_spaces)

    def has_scope(self, scope: str) -> bool:
        return scope in self.capability_scopes


# ---------------------------------------------------------------------------
# 解析器协议（S1 实现）
# ---------------------------------------------------------------------------


class ConversationContextResolver:
    """宿主侧上下文解析器协议。S1 提供真实实现；此处冻结方法签名。"""

    def resolve(
        self,
        *,
        channel: str,
        sender_id: str,
        conversation_kind: ConversationKind,
        conversation_id: str,
    ) -> ResolvedContext:  # pragma: no cover - 契约声明
        raise NotImplementedError
