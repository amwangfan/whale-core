"""NotificationService：Core 各模块的**唯一**通知入口（修正方案 §6.2 /
contracts/v2 §13.4：「Core 业务只调 notification_service.notify(...)」）。

职责：
- 对业务模块屏蔽 category 枚举、target/actions 结构、TTL 策略等细节；
- 每个分类一个便捷方法，参数即业务语义（task_id / conversation_id /
  approval_id），内部组装跳转目标与按钮；
- 不持有任何通道细节（选路/补传/撤回都在 NotificationGateway）。

审批类通知的默认按钮（批准/拒绝）只是入口——真正的裁决在 core 审批流
（§14，approval_id 一次性核销），按钮点击经 notification.action 回传后
由注入 gateway 的 action_handler 转交，本服务不产生任何授权。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from whale_core.notifications.models import (
    NotificationAction,
    NotificationCategory,
    NotificationTarget,
)

if TYPE_CHECKING:  # 仅类型标注，避免 gateway → services 循环导入
    from whale_core.notifications.gateway import NotificationGateway

#: 审批通知默认按钮（§13.1：审批类带批准/拒绝）
DEFAULT_APPROVAL_ACTIONS: tuple[NotificationAction, ...] = (
    NotificationAction(action_id="approve", label="批准"),
    NotificationAction(action_id="reject", label="拒绝"),
)


class NotificationService:
    """业务门面。所有方法 async（送达链路是 async 的）。"""

    def __init__(self, gateway: NotificationGateway) -> None:
        if gateway is None:
            raise ValueError("gateway 必填")
        self._gateway = gateway

    # -- 通用入口（§13.4 调用形状） ----------------------------------------

    async def notify(self, device_id: str, category: str, title: str,
                     body: str = "", *,
                     target: NotificationTarget | None = None,
                     actions=None, group_key: str | None = None,
                     collapse_key: str | None = None,
                     expires_at: int | None = None) -> str:
        return await self._gateway.notify(
            device_id, category, title, body, target=target, actions=actions,
            group_key=group_key, collapse_key=collapse_key, expires_at=expires_at)

    # -- 分类便捷方法 ------------------------------------------------------

    async def chat_message(self, device_id: str, *, title: str, body: str,
                           conversation_id: str = "") -> str:
        target = (NotificationTarget("conversation", conversation_id)
                  if conversation_id else NotificationTarget("chat"))
        return await self.notify(device_id, NotificationCategory.CHAT_MESSAGE,
                                 title, body, target=target,
                                 group_key=conversation_id or None)

    async def task_started(self, device_id: str, *, task_id: str,
                           title: str, body: str = "") -> str:
        return await self.notify(device_id, NotificationCategory.TASK_STARTED,
                                 title, body,
                                 target=NotificationTarget("task", task_id),
                                 group_key=task_id)

    async def task_progress(self, device_id: str, *, task_id: str,
                            title: str, body: str = "") -> str:
        # 进度类默认折叠合并（policies.collapse=True）：同任务新通知替换旧通知
        return await self.notify(device_id, NotificationCategory.TASK_PROGRESS,
                                 title, body,
                                 target=NotificationTarget("task", task_id),
                                 group_key=task_id)

    async def task_completed(self, device_id: str, *, task_id: str,
                             title: str, body: str = "") -> str:
        return await self.notify(device_id, NotificationCategory.TASK_COMPLETED,
                                 title, body,
                                 target=NotificationTarget("task", task_id),
                                 group_key=task_id)

    async def task_failed(self, device_id: str, *, task_id: str,
                          title: str, body: str = "") -> str:
        return await self.notify(device_id, NotificationCategory.TASK_FAILED,
                                 title, body,
                                 target=NotificationTarget("task", task_id),
                                 group_key=task_id)

    async def approval_requested(self, device_id: str, *, approval_id: str,
                                 title: str, body: str,
                                 actions=DEFAULT_APPROVAL_ACTIONS) -> str:
        return await self.notify(device_id,
                                 NotificationCategory.APPROVAL_REQUESTED,
                                 title, body,
                                 target=NotificationTarget("approval",
                                                           approval_id),
                                 actions=actions, group_key=approval_id)

    async def device_offline(self, device_id: str, *, title: str,
                             body: str = "") -> str:
        return await self.notify(device_id, NotificationCategory.DEVICE_OFFLINE,
                                 title, body,
                                 target=NotificationTarget("device", device_id))

    async def device_online(self, device_id: str, *, title: str,
                            body: str = "") -> str:
        return await self.notify(device_id, NotificationCategory.DEVICE_ONLINE,
                                 title, body,
                                 target=NotificationTarget("device", device_id))

    async def memory_updated(self, device_id: str, *, title: str,
                             body: str = "") -> str:
        return await self.notify(device_id, NotificationCategory.MEMORY_UPDATED,
                                 title, body)

    async def reminder_triggered(self, device_id: str, *, title: str,
                                 body: str = "", expires_at: int) -> str:
        """提醒类：expires_at 必填（提醒业务知道自己的截止时刻）。"""
        return await self.notify(device_id,
                                 NotificationCategory.REMINDER_TRIGGERED,
                                 title, body, expires_at=expires_at)
