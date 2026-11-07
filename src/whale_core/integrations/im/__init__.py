"""IM Gateway 抽象（whale-mobile-core-development-plan §7）。

双向对话与控制通道：接收外部消息、路由会话、投递结果、远程一次性审批、
斜杠命令（/stop /steer /status /new /model）。统一消息模型以稳定
``target_id`` 别名寻址，不暴露平台原生 chat id。

公开入口：模型（models）、网关（gateway）、路由（router）、
审批桥（approval_bridge）、投递（delivery）、provider（providers）。
"""

from whale_core.integrations.im.approval_bridge import ApprovalBridge
from whale_core.integrations.im.delivery import (
    BASE_DELAY_S,
    MAX_ATTEMPTS,
    DeliveryError,
    DeliveryService,
    format_approval_text,
)
from whale_core.integrations.im.gateway import (
    ImGateway,
    ImProvider,
    MessageHandlerFn,
    TaskControlPort,
)
from whale_core.integrations.im.models import (
    CHAT_GROUP,
    CHAT_PRIVATE,
    DECISION_APPROVED,
    DECISION_EXPIRED,
    DECISION_REJECTED,
    DEFAULT_APPROVAL_TTL_S,
    DEFAULT_REPLY_OPTIONS,
    KNOWN_COMMANDS,
    ApprovalRequest,
    ApprovalResolution,
    Command,
    DeadLetter,
    ImMessage,
    OutboundMessage,
    attachment_dict,
    parse_command,
)
from whale_core.integrations.im.router import (
    InMemorySessionBindingStore,
    SessionBindingStore,
    SessionRouter,
)

__all__ = [
    "ApprovalBridge",
    "ApprovalRequest",
    "ApprovalResolution",
    "BASE_DELAY_S",
    "CHAT_GROUP",
    "CHAT_PRIVATE",
    "Command",
    "DECISION_APPROVED",
    "DECISION_EXPIRED",
    "DECISION_REJECTED",
    "DEFAULT_APPROVAL_TTL_S",
    "DEFAULT_REPLY_OPTIONS",
    "DeadLetter",
    "DeliveryError",
    "DeliveryService",
    "ImGateway",
    "ImMessage",
    "ImProvider",
    "InMemorySessionBindingStore",
    "KNOWN_COMMANDS",
    "MAX_ATTEMPTS",
    "MessageHandlerFn",
    "OutboundMessage",
    "SessionBindingStore",
    "SessionRouter",
    "TaskControlPort",
    "attachment_dict",
    "format_approval_text",
    "parse_command",
]
