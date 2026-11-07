"""Notification Gateway：Core 侧原生通知系统（contracts/v2 §13）。

设计依据《Whale 原生通知系统修正方案 2026-11-07》：
- 原生通知是**产品能力**；ntfy/Bark/PushDeer 只是可选外部出口（默认关闭）；
- WSS 首选送达，FCM/厂商 Push 为可选适配器；断线留队、重连补传；
- 同一事件多通道送达只展示一次（端侧按 event_id 去重）；
- 通知按钮不是本地授权旁路（审批裁决归 core §14）；
- displayed ≠ 用户已看到；聊天/审批/任务完成不同 TTL（policies.py）。

公开入口：门面（gateway）、业务服务（services）、模型（models）、
策略（policies）、队列与回执（delivery_queue / delivery_receipts）、
送达通道（providers）。
"""

from whale_core.notifications.delivery_queue import (
    DELIVERY_PREFIX,
    MAX_QUEUE_PER_DEVICE,
    QUEUE_PREFIX,
    DeliveryQueue,
)
from whale_core.notifications.delivery_receipts import (
    DeliveryReceipts,
    NotificationActionHandler,
)
from whale_core.notifications.gateway import NotificationGateway
from whale_core.notifications.models import (
    CATEGORIES,
    PRIORITIES,
    RECEIPT_STATUS_MAP,
    TARGET_TYPES,
    DeliveryRecord,
    DeliveryStatus,
    NotificationAction,
    NotificationCategory,
    NotificationEvent,
    NotificationTarget,
)
from whale_core.notifications.policies import (
    CATEGORY_CHANNELS,
    DEFAULT_POLICIES,
    FALLBACK_TTL_MS,
    CategoryPolicy,
    PolicyTable,
    channel_for,
)
from whale_core.notifications.router import DeliveryRouter

__all__ = [
    "CATEGORIES",
    "CATEGORY_CHANNELS",
    "DEFAULT_POLICIES",
    "DELIVERY_PREFIX",
    "DeliveryQueue",
    "DeliveryReceipts",
    "DeliveryRecord",
    "DeliveryRouter",
    "DeliveryStatus",
    "FALLBACK_TTL_MS",
    "MAX_QUEUE_PER_DEVICE",
    "NotificationAction",
    "NotificationActionHandler",
    "NotificationCategory",
    "NotificationEvent",
    "NotificationGateway",
    "NotificationTarget",
    "PRIORITIES",
    "PolicyTable",
    "CategoryPolicy",
    "QUEUE_PREFIX",
    "RECEIPT_STATUS_MAP",
    "TARGET_TYPES",
    "channel_for",
]
