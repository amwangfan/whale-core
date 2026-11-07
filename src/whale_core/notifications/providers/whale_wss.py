"""WSS provider：contracts/v2 控制通道首选送达链路（§13.4）。

注入 ``DeviceWssPort``（未来的 embedded_wss transport 实现它）：
- ``send_notification_event(device_id, payload)``：把 notification.event 的
  **payload** 写入设备长连。信封（v/id/ts/seq/dir）由 WSS 连接层按 §2
  封装——``seq`` 是每连接单调序号，归连接层管，本模块不伪造；
- ``is_online(device_id)``：连接层在线视图。

离线语义：**不抛错**。``send`` 返回 ``OFFLINE_DELIVERY_ID`` 哨兵，router
据此保留队列项，等设备重连后经 ``notification.sync`` 补传（§13.3）。

撤回：port 若实现可选方法 ``send_notification_revoke(device_id, payload)``
则真实下发 notification.revoke；否则诚实返回 False（队列/端侧 TTL 兜底）。
"""
from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable

from whale_core.notifications.models import DeliveryStatus, NotificationEvent
from whale_core.notifications.providers.base import NotificationProviderError

#: 离线保留哨兵：send 的返回值；router 见到它 → 队列保留、不计送达
OFFLINE_DELIVERY_ID = "wss:offline"

#: 本地 delivery 状态缓存上限（只记「我发出的」，displayed/opened 以回执为准）
_MAX_TRACKED = 10_000


@runtime_checkable
class DeviceWssPort(Protocol):
    """设备 WSS 连接层端口（embedded_wss v2 就位后由其实现）。"""

    async def send_notification_event(self, device_id: str,
                                      payload: dict) -> None:
        """向在线设备写一条 notification.event payload；不在线勿调（先问 is_online）。"""
        ...

    def is_online(self, device_id: str) -> bool:
        """设备当前是否在线（心跳超时标离线后即 False）。"""
        ...


class WhaleWssProvider:
    """首选 provider。name="whale_wss"。"""
    name = "whale_wss"

    def __init__(self, port: DeviceWssPort) -> None:
        if port is None:
            raise ValueError("DeviceWssPort 必填")
        self._port = port
        self._lock = threading.Lock()
        self._sent: dict[str, DeliveryStatus] = {}

    def is_online(self, device_id: str) -> bool:
        """供 router 选路：直接透传连接层在线视图。"""
        return bool(self._port.is_online(device_id))

    async def send(self, device_id: str, event: NotificationEvent) -> str:
        if not self._port.is_online(device_id):
            # 离线不抛错：返回哨兵让 router 保留队列（sync 补传兜底）
            return OFFLINE_DELIVERY_ID
        try:
            await self._port.send_notification_event(device_id, event.to_payload())
        except NotificationProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 统一归一为 provider 错误
            raise NotificationProviderError(
                "unavailable", f"wss 发送失败：{type(exc).__name__}") from exc
        delivery_id = f"wss:{event.event_id}"
        with self._lock:
            if len(self._sent) >= _MAX_TRACKED:
                self._sent.clear()  # 有界：满了整体清空（状态以回执为准，缓存只是观测）
            self._sent[delivery_id] = DeliveryStatus.DELIVERED
        return delivery_id

    async def revoke(self, device_id: str, notification_id: str, *,
                     reason: str = "") -> bool:
        if not self._port.is_online(device_id):
            return False
        sender = getattr(self._port, "send_notification_revoke", None)
        if sender is None:
            return False  # 连接层尚无撤回口：诚实返回 False，不伪报
        from whale_core.devices.transports.base import new_id
        payload: dict[str, Any] = {
            "event_id": new_id("evt"),  # 撤回指令自身的事件 ID（§13.3）
            "notification_id": notification_id,
            "device_id": device_id,
        }
        if reason:
            payload["reason"] = reason[:200]
        try:
            await sender(device_id, payload)
        except Exception:  # noqa: BLE001 —— 撤回失败不升级为异常（尽力而为）
            return False
        return True

    async def get_status(self, delivery_id: str) -> DeliveryStatus | None:
        if delivery_id == OFFLINE_DELIVERY_ID:
            return None  # 哨兵不是真实送达
        with self._lock:
            return self._sent.get(delivery_id)
