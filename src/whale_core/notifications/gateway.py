"""NotificationGateway 门面（contracts/v2 §13.4 Core 侧送达）。

装配：注入 MemoryStore kv + 各通道端口（WSS 连接层 / v1 gateway / 外部出口），
内部组装队列、台账、回执处理器、选路器与通知历史。

调用形状（业务侧请走 services/notification_service.py）：

.. code-block:: python

    notification_id = await gateway.notify(
        device_id, "task.completed", "任务已完成", "Root 手机已完成应用检查。",
        target=NotificationTarget("task", task_id))

- ``notify``：生成 evt_/notif_ ID（§3）→ 应用 policies（TTL/优先级/折叠）→
  入队 + 台账 + 历史 → 尝试送达（在线 WSS 直发；离线留队等 sync 补传）；
- ``revoke``：按 notification_id 撤回（在线 WSS 真实下发；其余通道诚实降级）；
- ``handle_receipt`` / ``handle_action``：上行回执与按钮操作（§13.2/§13.3）；
- ``handle_sync``：端侧重连补传仍在有效期内的事件（§13.3）；
- ``sweep_expired``：过期清扫（有界，可由宿主定时器驱动）。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from whale_core.devices.transports.base import new_id, now_ms
from whale_core.notifications.delivery_queue import DeliveryQueue
from whale_core.notifications.delivery_receipts import DeliveryReceipts
from whale_core.notifications.models import (
    ACTION_ID_RE,
    BODY_MAX_CHARS,
    CATEGORIES,
    GROUP_KEY_MAX_CHARS,
    MAX_ACTIONS,
    TITLE_MAX_CHARS,
    TS_MAX,
    DeliveryRecord,
    DeliveryStatus,
    NotificationAction,
    NotificationEvent,
    NotificationTarget,
)
from whale_core.notifications.policies import PolicyTable
from whale_core.notifications.router import DeliveryRouter
from whale_core.notifications.services.notification_history import NotificationHistory


def _clamp(text: str, limit: int) -> str:
    """超长截断（保证产物恒过 schema；截断是通知路径的合理降级）。"""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


class NotificationGateway:
    """Notification Gateway 门面。每个 Core 进程一个实例。"""

    def __init__(self, store, *, providers: Iterable = (),
                 policies: PolicyTable | None = None,
                 action_handler=None,
                 history_limit: int = 500,
                 max_queue_per_device: int = 200,
                 now_fn: Callable[[], int] | None = None) -> None:
        if store is None:
            raise ValueError("store 必填（MemoryStore kv 抽象）")
        self._now = now_fn or now_ms
        self.queue = DeliveryQueue(
            store, max_per_device=max_queue_per_device, now_fn=self._now)
        self.history = NotificationHistory(store, limit=history_limit,
                                           now_fn=self._now)
        self.policies = policies if policies is not None else PolicyTable()
        self.receipts = DeliveryReceipts(
            self.queue, action_handler=action_handler, now_fn=self._now)
        self.router = DeliveryRouter(providers, queue=self.queue, now_fn=self._now)

    # ------------------------------------------------------------------
    # 下行：发起通知
    # ------------------------------------------------------------------

    async def notify(self, device_id: str, category: str, title: str,
                     body: str, *, target: NotificationTarget | None = None,
                     actions: Iterable[NotificationAction] | None = None,
                     group_key: str | None = None,
                     collapse_key: str | None = None,
                     expires_at: int | None = None) -> str:
        """发起一条原生通知，返回 notification_id（notif_+ULID）。"""
        if category not in CATEGORIES:
            raise ValueError(f"未知通知分类: {category!r}")
        title = _clamp((title or "").strip(), TITLE_MAX_CHARS)
        if not title:
            raise ValueError("title 必填（通知必须对人可解释）")
        body = _clamp(body or "", BODY_MAX_CHARS)
        action_tuple = tuple(actions or ())
        if len(action_tuple) > MAX_ACTIONS:
            raise ValueError(f"actions 最多 {MAX_ACTIONS} 个")
        for action in action_tuple:
            if not ACTION_ID_RE.match(action.action_id):
                raise ValueError(f"非法 action_id: {action.action_id!r}")
        if group_key and len(group_key) > GROUP_KEY_MAX_CHARS:
            raise ValueError("group_key 超长（≤128）")
        if collapse_key and len(collapse_key) > GROUP_KEY_MAX_CHARS:
            raise ValueError("collapse_key 超长（≤128）")

        created_at = self._now()
        policy = self.policies.policy_for(category)
        resolved_expires = self.policies.resolve_expires_at(
            category, created_at=created_at, expires_at=expires_at)
        if resolved_expires > TS_MAX:
            raise ValueError("expires_at 超出协议上限（2100-01-01）")
        # 折叠合并（task.progress 类）：未显式给 collapse_key 时按组键自动派生
        if collapse_key is None and policy.collapse:
            collapse_key = group_key or f"{category}:{device_id}"

        event = NotificationEvent(
            event_id=new_id("evt"),
            notification_id=new_id("notif"),
            device_id=device_id,
            category=category,
            title=title,
            body=body,
            priority=policy.priority,
            created_at=created_at,
            expires_at=resolved_expires,
            target=target,
            actions=action_tuple,
            group_key=group_key,
            collapse_key=collapse_key,
        )

        # 入队（断线保留）→ 台账（created→queued）→ 历史 → 尝试送达
        self.queue.enqueue(event)
        record = DeliveryRecord(
            delivery_id="", event_id=event.event_id,
            notification_id=event.notification_id, device_id=device_id,
            provider="", created_at=created_at, updated_at=created_at)
        record.advance(DeliveryStatus.QUEUED, at=created_at)
        self.queue.put_delivery(record)
        self.history.add(event)

        await self.router.dispatch(event)
        return event.notification_id

    async def revoke(self, device_id: str, notification_id: str, *,
                     reason: str = "") -> bool:
        """撤回一条通知（§13.3）。返回是否有通道真实下发了撤回。

        无论通道是否支持撤回：队列项立即出队（不再补传），台账标 revoked。
        通道不支持/设备离线时返回 False（诚实，不伪报）。
        """
        record = self.queue.get_delivery(device_id, notification_id)
        if record is not None:
            self.queue.ack_event(device_id, record.event_id)  # 停止补传
            record.revoked = True
            record.updated_at = self._now()
            self.queue.put_delivery(record)
        delivered = False
        for provider in self.router.providers:
            try:
                if await provider.revoke(device_id, notification_id,
                                         reason=reason):
                    delivered = True
            except Exception:  # noqa: BLE001 —— 单通道撤回失败不阻断其余通道
                continue
        return delivered

    # ------------------------------------------------------------------
    # 上行：回执 / 按钮 / 同步
    # ------------------------------------------------------------------

    def handle_receipt(self, payload: Mapping[str, Any]) -> DeliveryRecord | None:
        """notification.receipt（§13.2）。displayed 不代表用户已看到。"""
        return self.receipts.handle_receipt(payload)

    def handle_action(self, payload: Mapping[str, Any]) -> DeliveryRecord | None:
        """notification.action（§13.3）：按钮操作转交注入的 action_handler。

        安全纪律：按钮不是本地授权旁路——审批类按钮的真正裁决在 core
        审批流（approval_id 一次性核销，§14），本方法不产生任何授权。
        """
        return self.receipts.handle_action(payload)

    async def handle_sync(self, device_id: str,
                          last_event_id: str | None = None) -> int:
        """notification.sync（§13.3）：补传仍在有效期内的在队事件。

        返回本次补发条数。已确认（回执出队）与已过期的不会重发；
        补发仍走选路（设备不在线则一条也发不出，留队等下次 sync）。
        """
        events = self.queue.replay_since(device_id, last_event_id)
        sent = 0
        for event in events:
            record = await self.router.dispatch(event)
            if record.status is DeliveryStatus.DELIVERED:
                sent += 1
        return sent

    # ------------------------------------------------------------------
    # 维护与观测
    # ------------------------------------------------------------------

    def sweep_expired(self, *, now: int | None = None,
                      max_deletes: int = 500) -> int:
        """过期清扫（有界）：过期队列项出队 + 台账推进 EXPIRED。"""
        return len(self.queue.dequeue_expired(now=now, max_deletes=max_deletes))

    def get_delivery(self, device_id: str,
                     notification_id: str) -> DeliveryRecord | None:
        """送达台账快照（观测/测试用）。"""
        return self.queue.get_delivery(device_id, notification_id)
