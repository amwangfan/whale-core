"""NotificationProvider 协议（contracts/v2 §13.4：WSS 首选，其余为可选适配器）。

实现方：``whale_wss``（首选链路）、
``external``（ntfy 等可选外部出口，默认关闭）。

纪律：
- ``send`` 返回 delivery_id（provider 侧送达标识）；设备离线等「暂时送不到」
  的场景**不抛错**，返回 provider 自定义的保留哨兵（如 whale_wss 的
  ``OFFLINE_DELIVERY_ID``），由 router 决定保留队列等补传；
- ``revoke`` 返回是否真实下发了撤回；通道不支持撤回时诚实返回 False
  （绝不伪报已撤回）；
- ``get_status`` 只能报告 provider 自己确知的状态（如「已写帧」），端侧
  展示/交互状态一律以 notification.receipt 为准。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from whale_core.notifications.models import DeliveryStatus, NotificationEvent


class NotificationProviderError(RuntimeError):
    """provider 发送明确失败（区别于「离线保留」：这是重试也没用的失败）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class NotificationProvider(Protocol):
    """通知送达通道协议。全部方法 async（真实实现会做网络 IO）。"""

    @property
    def name(self) -> str:
        """provider 标识（台账/观测用），如 ``whale_wss`` / ``ntfy``。"""
        ...

    async def send(self, device_id: str, event: NotificationEvent) -> str:
        """送达一条通知事件，返回 delivery_id。

        设备离线：返回保留哨兵（不抛错）；明确失败：抛
        ``NotificationProviderError``。
        """
        ...

    async def revoke(self, device_id: str, notification_id: str, *,
                     reason: str = "") -> bool:
        """按 notification_id 撤回；通道不支持/设备不可达 → False。"""
        ...

    async def get_status(self, delivery_id: str) -> DeliveryStatus | None:
        """查 provider 自知的送达状态；未知 delivery_id → None。"""
        ...
