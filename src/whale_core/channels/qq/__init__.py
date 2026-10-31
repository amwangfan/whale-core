"""QQ 渠道正式领域入口（issue #27）。

re-export 两个实现模块的公开名：

- ``whale_core.life.qq_onebot``：OneBot v11 适配器（身份入站 + 出站受理端口）；
- ``whale_core.life.qq_workflow``：QQ 入站 → AI 回复草稿 + 审批闸门工作流。

两个 ``life`` 模块保留为**实现细节**、原位不动（既有 import 不破坏）；
新代码请从 ``whale_core.channels.qq`` 导入。这里 re-export 的名字与 life
原模块是同一对象（``is`` 恒等），由 ``tests/test_arch_channels.py`` 守卫。
"""

from __future__ import annotations

from whale_core.life.qq_onebot import (
    CHANNEL_QQ,
    DEFAULT_ONEBOT_HOST,
    DEFAULT_ONEBOT_PORT,
    MAX_BODY_BYTES,
    MAX_TEXT_CHARS,
    HttpOneBotTransport,
    NullOneBotTransport,
    OneBotHttpListener,
    OneBotInboundBridge,
    OneBotOutboundPort,
    OneBotTransport,
    OneBotTransportError,
    bearer_authenticated,
    parse_onebot_message,
)
from whale_core.life.qq_workflow import REPLY_CHANNEL, QqInboundWorkflow

__all__ = [
    "CHANNEL_QQ",
    "DEFAULT_ONEBOT_HOST",
    "DEFAULT_ONEBOT_PORT",
    "MAX_BODY_BYTES",
    "MAX_TEXT_CHARS",
    "REPLY_CHANNEL",
    "HttpOneBotTransport",
    "NullOneBotTransport",
    "OneBotHttpListener",
    "OneBotInboundBridge",
    "OneBotOutboundPort",
    "OneBotTransport",
    "OneBotTransportError",
    "QqInboundWorkflow",
    "bearer_authenticated",
    "parse_onebot_message",
]
