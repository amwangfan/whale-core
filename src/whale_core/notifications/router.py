"""DeliveryRouter：按设备在线状态与 provider 可用性选路（§13.4）。

选路规则：
1. ``whale_wss`` 在线 → 首选 WSS 直发（端侧 NotificationManager 展示）；
2. WSS 不在线 → 队列保留（gateway 已入队，等 ``notification.sync`` 补传），
   并尝试**用户显式开启**的外部出口兜底（如 ntfy）。

多通道同送去重（修正方案）：同一事件可能经「外部出口兜底 + WSS 补传」
多次到达端侧，**core 不做跨通道状态合并**，由端侧按 event_id 去重保证
「同一事件只展示一次」（§13 首部）。
"""
from __future__ import annotations

from typing import Callable, Iterable

from whale_core.devices.transports.base import now_ms
from whale_core.notifications.delivery_queue import DeliveryQueue
from whale_core.notifications.models import DeliveryRecord, DeliveryStatus, NotificationEvent
from whale_core.notifications.providers.base import NotificationProviderError
from whale_core.notifications.providers.whale_wss import OFFLINE_DELIVERY_ID


class DeliveryRouter:
    """通知选路器。provider 列表按注入顺序即兜底顺序。"""

    def __init__(self, providers: Iterable = (), *, queue: DeliveryQueue,
                 now_fn: Callable[[], int] | None = None) -> None:
        if queue is None:
            raise ValueError("queue 必填")
        self._providers = list(providers)
        self._queue = queue
        self._now = now_fn or now_ms

    @property
    def providers(self) -> tuple:
        return tuple(self._providers)

    async def dispatch(self, event: NotificationEvent) -> DeliveryRecord:
        """选路并送达；更新台账并返回。送不出去不抛错（队列保留等补传）。"""
        record = self._queue.get_delivery(event.device_id, event.notification_id)
        if record is None:
            record = DeliveryRecord(
                delivery_id="", event_id=event.event_id,
                notification_id=event.notification_id, device_id=event.device_id,
                provider="", created_at=self._now(), updated_at=self._now())
            record.advance(DeliveryStatus.QUEUED, at=self._now())

        # 1) WSS 首选（在线才调，离线返回哨兵不抛错）
        wss = self._find("whale_wss")
        if wss is not None and getattr(wss, "is_online", lambda _d: False)(event.device_id):
            if await self._try_send(wss, event, record):
                return record

        # 2) 外部出口兜底：仅显式开启（available）的参与
        for provider in self._providers:
            if provider is wss:
                continue
            if not getattr(provider, "available", False):
                continue  # 默认关闭的外部出口不参与
            if await self._try_send(provider, event, record):
                return record

        # 无通道可达：队列保留（等 sync 补传 / 过期清扫），台账记原因
        if not record.last_error:
            record.last_error = "no_route"
        self._queue.put_delivery(record)
        return record

    # ------------------------------------------------------------------

    def _find(self, name: str):
        for provider in self._providers:
            if getattr(provider, "name", "") == name:
                return provider
        return None

    async def _try_send(self, provider, event: NotificationEvent,
                        record: DeliveryRecord) -> bool:
        """尝试经某 provider 送达；成功更新台账（DELIVERED）返回 True。"""
        try:
            delivery_id = await provider.send(event.device_id, event)
        except NotificationProviderError as exc:
            record.attempts += 1
            record.last_error = exc.code
            self._queue.put_delivery(record)
            return False
        except Exception as exc:  # noqa: BLE001 —— 单通道失败不阻断选路
            record.attempts += 1
            record.last_error = type(exc).__name__
            self._queue.put_delivery(record)
            return False
        if delivery_id == OFFLINE_DELIVERY_ID:
            return False  # 离线哨兵：队列保留，不计送达
        record.attempts += 1
        record.delivery_id = delivery_id
        record.provider = provider.name
        record.last_error = ""
        record.advance(DeliveryStatus.DELIVERED, at=self._now())
        self._queue.put_delivery(record)
        # 记 sent_at（不出队——端侧回执才出队；sync 前重启仍有补传依据）
        self._queue.mark_sent(event.device_id, event.event_id)
        return True
