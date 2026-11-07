"""notification.receipt / notification.action 上行处理（contracts/v2 §13.2/§13.3）。

职责：
- 状态推进：receipt.status 映射进 ``DeliveryRecord`` 状态机
  （received→delivered、displayed→displayed、……，见 models.RECEIPT_STATUS_MAP）；
- 诚实性：**displayed 不回填为「用户已看到」**——Android 可能因权限/渠道
  关闭/系统策略不展示，只有 opened/action_submitted 才算用户真实交互
  （``DeliveryStatus.user_acknowledged``）；通知历史的「已读」也不在此处置位；
- 失败记原因：receipt 的 wire 格式没有原因字段（schema 仅 5 字段），端侧报
  failed 时 core 只能诚实记 ``device_reported_failed``；core 侧失败（发送
  异常/过期）由 gateway/router 记具体原因；
- action 转交：``notification.action``（用户点了通知按钮）推进
  ACTION_SUBMITTED 并把操作转交注入的 ``action_handler``。

安全纪律（§13.3）：**通知按钮不是本地授权旁路**。``action_handler`` 的
实现方必须把审批类按钮（approve/reject）转交 core 审批流做一次性核销
（§14），本模块自身不发放、不核销任何授权。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from whale_core.devices.transports.base import now_ms
from whale_core.notifications.delivery_queue import DeliveryQueue
from whale_core.notifications.models import (
    RECEIPT_STATUS_MAP,
    DeliveryRecord,
    DeliveryStatus,
)


@runtime_checkable
class NotificationActionHandler(Protocol):
    """通知按钮操作的处理口（由 core 审批/任务模块实现注入）。"""

    def handle_notification_action(self, *, device_id: str, notification_id: str,
                                   event_id: str, action_id: str) -> None:
        """处理一次按钮操作。实现方负责真正的业务裁决（如审批核销）。"""
        ...


#: 可注入的处理回调形状（与 Protocol 方法同签名）
ActionHandlerFn = Callable[..., None]

#: 合法 receipt 状态词（schema 枚举；多余/未知一律丢弃）
_KNOWN_RECEIPT_STATUSES = frozenset(RECEIPT_STATUS_MAP)


class DeliveryReceipts:
    """回执/按钮操作处理器。查询与推进都落在 DeliveryQueue 的台账上。"""

    def __init__(self, queue: DeliveryQueue, *,
                 action_handler: NotificationActionHandler | ActionHandlerFn | None = None,
                 now_fn: Callable[[], int] | None = None) -> None:
        if queue is None:
            raise ValueError("queue 必填")
        self._queue = queue
        self._action_handler = action_handler
        self._now = now_fn or now_ms

    # ------------------------------------------------------------------
    # notification.receipt
    # ------------------------------------------------------------------

    def handle_receipt(self, payload: Mapping[str, Any]) -> DeliveryRecord | None:
        """处理一条 notification.receipt payload；无效/未知一律返回 None。

        宽容读取（端侧不可信）：缺字段、未知状态词、event_id 与台账不符、
        终态后迟到回执——都不报错、不改写台账（first terminal wins）。
        """
        device_id = str(payload.get("device_id") or "")
        notification_id = str(payload.get("notification_id") or "")
        event_id = str(payload.get("event_id") or "")
        status_word = str(payload.get("status") or "")
        received_at = int(payload.get("received_at") or 0) or self._now()
        if not device_id or not notification_id or not event_id:
            return None
        status = RECEIPT_STATUS_MAP.get(status_word)
        if status is None:
            return None  # 未知状态词：静默忽略（前向兼容）
        record = self._queue.get_delivery(device_id, notification_id)
        if record is None or record.event_id != event_id:
            return None  # 未知通知 / 事件错配：不写数据（防洪）
        # 端侧确实拿到了该事件 → 出队，sync 不再补发
        self._queue.ack_event(device_id, event_id)
        # receipt 无原因字段：端侧 failed 只能记「端侧报告失败」，不臆造细节
        reason = "device_reported_failed" if status is DeliveryStatus.FAILED else ""
        record.advance(status, at=received_at, reason=reason)
        self._queue.put_delivery(record)
        return record

    # ------------------------------------------------------------------
    # notification.action（通知按钮）
    # ------------------------------------------------------------------

    def handle_action(self, payload: Mapping[str, Any]) -> DeliveryRecord | None:
        """处理一条 notification.action payload 并转交 action_handler。

        按钮操作只是「用户意图回传」：本模块把它登记为 ACTION_SUBMITTED 并
        转交注入的处理口；**不产生任何本地授权**（审批裁决归 core §14）。
        handler 异常不影响状态推进（状态已落定，回调失败不改变事实）。
        """
        device_id = str(payload.get("device_id") or "")
        notification_id = str(payload.get("notification_id") or "")
        event_id = str(payload.get("event_id") or "")
        action_id = str(payload.get("action_id") or "")
        submitted_at = int(payload.get("submitted_at") or 0) or self._now()
        if not device_id or not notification_id or not event_id or not action_id:
            return None
        record = self._queue.get_delivery(device_id, notification_id)
        if record is None or record.event_id != event_id:
            return None
        self._queue.ack_event(device_id, event_id)
        record.advance(DeliveryStatus.ACTION_SUBMITTED, at=submitted_at)
        self._queue.put_delivery(record)
        handler = self._action_handler
        if handler is not None:
            try:
                if hasattr(handler, "handle_notification_action"):
                    handler.handle_notification_action(  # type: ignore[union-attr]
                        device_id=device_id, notification_id=notification_id,
                        event_id=event_id, action_id=action_id)
                else:
                    handler(device_id=device_id, notification_id=notification_id,
                            event_id=event_id, action_id=action_id)
            except Exception:  # noqa: BLE001 —— 回调异常不反噬送达台账
                pass
        return record
