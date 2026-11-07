"""IM provider 实现。首批：QQ dry-run（模拟，不发送任何真实消息）。"""

from whale_core.integrations.im.providers.qq_dryrun import (
    ALIAS_PREFIX,
    QqDryRunProvider,
    RecordingOneBotTransport,
)

__all__ = [
    "ALIAS_PREFIX",
    "QqDryRunProvider",
    "RecordingOneBotTransport",
]
