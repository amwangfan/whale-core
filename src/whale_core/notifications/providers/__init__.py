"""通知送达通道：whale_wss（首选，contracts/v2 §13.4）/ external（可选出口）。

v1 设备面已随 contracts/v2 下线（§18），legacy_v1 桥随之删除——旧设备须
升级到 v2 协议再接入。
"""

from whale_core.notifications.providers.base import (
    NotificationProvider,
    NotificationProviderError,
)
from whale_core.notifications.providers.external import (
    BarkProvider,
    NtfyProvider,
    PushDeerProvider,
)
from whale_core.notifications.providers.whale_wss import (
    OFFLINE_DELIVERY_ID,
    DeviceWssPort,
    WhaleWssProvider,
)

__all__ = [
    "BarkProvider",
    "DeviceWssPort",
    "NotificationProvider",
    "NotificationProviderError",
    "NtfyProvider",
    "OFFLINE_DELIVERY_ID",
    "PushDeerProvider",
    "WhaleWssProvider",
]
