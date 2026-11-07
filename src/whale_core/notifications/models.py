"""Notification Gateway 数据模型（contracts/v2 §13 通知子协议）。

- ``NotificationEvent`` 对齐 ``messages/notification.event.schema.json`` 的
  payload 全字段（``to_payload`` 产出的 dict 可直接过 schema）；
- ``DeliveryRecord`` 是 core 侧的送达台账：状态机
  ``created → queued → delivered → displayed → opened/dismissed/action_submitted``，
  任何非终态可转 ``expired/failed``；终态「first terminal wins」
  （对齐 v1 action.receipt 纪律，迟到回执不翻案）；
- 诚实性（§13.2）：``displayed`` 只表示「已提交 Android 通知系统」，
  **不等于用户已看到**——``DeliveryStatus.user_acknowledged`` 只对
  ``opened`` / ``action_submitted`` 为真，通知历史的「已读」也绝不因
  displayed 回执而置位。
"""
from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 分类与优先级词表（contracts/v2 §13.1 schema 枚举，单一事实源在 contracts）
# ---------------------------------------------------------------------------


class NotificationCategory:
    """通知分类词表（与 notification.event.schema.json 的 category 枚举一致）。"""

    CHAT_MESSAGE = "chat.message"
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    APPROVAL_REQUESTED = "approval.requested"
    DEVICE_OFFLINE = "device.offline"
    DEVICE_ONLINE = "device.online"
    MEMORY_UPDATED = "memory.updated"
    REMINDER_TRIGGERED = "reminder.triggered"


#: schema 枚举全集（校验用）
CATEGORIES: frozenset[str] = frozenset({
    NotificationCategory.CHAT_MESSAGE,
    NotificationCategory.TASK_STARTED,
    NotificationCategory.TASK_PROGRESS,
    NotificationCategory.TASK_COMPLETED,
    NotificationCategory.TASK_FAILED,
    NotificationCategory.APPROVAL_REQUESTED,
    NotificationCategory.DEVICE_OFFLINE,
    NotificationCategory.DEVICE_ONLINE,
    NotificationCategory.MEMORY_UPDATED,
    NotificationCategory.REMINDER_TRIGGERED,
})

#: 优先级词表（schema 枚举）
PRIORITIES: frozenset[str] = frozenset({"low", "normal", "high"})

#: 跳转目标类型词表（schema 枚举）
TARGET_TYPES: frozenset[str] = frozenset(
    {"task", "conversation", "device", "approval", "chat", "none"})

#: 信封/字段上限（schema 约束）
TITLE_MAX_CHARS = 200
BODY_MAX_CHARS = 2000
ACTION_LABEL_MAX_CHARS = 32
ACTION_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_ACTIONS = 4
GROUP_KEY_MAX_CHARS = 128
TS_MAX = 4102444800000  # 2100-01-01 UTC（contracts/v2 §2）


# ---------------------------------------------------------------------------
# 送达状态机
# ---------------------------------------------------------------------------


class DeliveryStatus(str, enum.Enum):
    """delivery 状态机（core 侧台账；receipt 状态词映射见 ``RECEIPT_STATUS_MAP``）。"""

    CREATED = "created"                  # 已建事件，尚未入队
    QUEUED = "queued"                    # 已入持久化队列，等待送达
    DELIVERED = "delivered"              # 通道已受理（WSS 已写帧/端侧回 received）
    DISPLAYED = "displayed"              # 端侧已提交 Android 通知系统（≠用户已看到）
    OPENED = "opened"                    # 终态：用户点开
    DISMISSED = "dismissed"              # 终态：用户划掉
    ACTION_SUBMITTED = "action_submitted"  # 终态：用户点了通知按钮
    EXPIRED = "expired"                  # 终态：过期待清理
    FAILED = "failed"                    # 终态：明确失败

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def user_acknowledged(self) -> bool:
        """用户真实交互过才算「已看到」；displayed 不算（§13.2 诚实性）。"""
        return self in (DeliveryStatus.OPENED, DeliveryStatus.ACTION_SUBMITTED)


_TERMINAL_STATUSES = frozenset({
    DeliveryStatus.OPENED,
    DeliveryStatus.DISMISSED,
    DeliveryStatus.ACTION_SUBMITTED,
    DeliveryStatus.EXPIRED,
    DeliveryStatus.FAILED,
})

#: 允许的状态迁移（终态不在键中 → first terminal wins）
_ALLOWED_TRANSITIONS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.CREATED: frozenset({
        DeliveryStatus.QUEUED, DeliveryStatus.DELIVERED,
        DeliveryStatus.EXPIRED, DeliveryStatus.FAILED}),
    DeliveryStatus.QUEUED: frozenset({
        DeliveryStatus.DELIVERED, DeliveryStatus.EXPIRED, DeliveryStatus.FAILED}),
    DeliveryStatus.DELIVERED: frozenset({
        DeliveryStatus.DISPLAYED, DeliveryStatus.OPENED, DeliveryStatus.DISMISSED,
        DeliveryStatus.ACTION_SUBMITTED, DeliveryStatus.EXPIRED,
        DeliveryStatus.FAILED}),
    DeliveryStatus.DISPLAYED: frozenset({
        DeliveryStatus.OPENED, DeliveryStatus.DISMISSED,
        DeliveryStatus.ACTION_SUBMITTED, DeliveryStatus.EXPIRED,
        DeliveryStatus.FAILED}),
}

#: notification.receipt.status → DeliveryStatus（§13.2 枚举逐字对齐）
RECEIPT_STATUS_MAP: dict[str, DeliveryStatus] = {
    "received": DeliveryStatus.DELIVERED,
    "displayed": DeliveryStatus.DISPLAYED,
    "opened": DeliveryStatus.OPENED,
    "dismissed": DeliveryStatus.DISMISSED,
    "action_submitted": DeliveryStatus.ACTION_SUBMITTED,
    "expired": DeliveryStatus.EXPIRED,
    "failed": DeliveryStatus.FAILED,
}


# ---------------------------------------------------------------------------
# 通知事件（notification.event payload 的运行时表示）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationTarget:
    """点击跳转目标（payload.target；type 必填，id 可空）。"""

    type: str                  # TARGET_TYPES 之一
    id: str = ""

    def __post_init__(self) -> None:
        if self.type not in TARGET_TYPES:
            raise ValueError(f"未知 target.type: {self.type!r}")
        if len(self.id) > 128:
            raise ValueError("target.id 超长（≤128）")

    def to_payload(self) -> dict:
        data: dict[str, Any] = {"type": self.type}
        if self.id:
            data["id"] = self.id
        return data

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> NotificationTarget:
        return cls(type=str(data.get("type") or "none"),
                   id=str(data.get("id") or ""))


@dataclass(frozen=True)
class NotificationAction:
    """通知操作按钮（payload.actions 项；最多 MAX_ACTIONS 个）。

    注意（§13.3）：按钮只是「把用户意图回传 core」的入口，**不是本地授权
    旁路**——审批类按钮（approve/reject）的真正裁决在 core 审批流
    （approval_id 一次性核销，§14），端侧与本模块都不发放任何授权。
    """

    action_id: str             # ^[a-z0-9_]{1,32}$，如 approve/reject/open
    label: str                 # 按钮文案 1..32

    def __post_init__(self) -> None:
        if not ACTION_ID_RE.match(self.action_id or ""):
            raise ValueError(f"非法 action_id: {self.action_id!r}")
        if not (1 <= len(self.label) <= ACTION_LABEL_MAX_CHARS):
            raise ValueError("action label 长度须为 1..32")

    def to_payload(self) -> dict:
        return {"action_id": self.action_id, "label": self.label}

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> NotificationAction:
        return cls(action_id=str(data.get("action_id") or ""),
                   label=str(data.get("label") or ""))


@dataclass(frozen=True)
class NotificationEvent:
    """一条原生通知事件（notification.event payload 全字段）。"""

    event_id: str              # evt_+ULID（幂等键，端侧按它去重）
    notification_id: str       # notif_+ULID（更新/撤回复用）
    device_id: str             # 32 hex
    category: str              # CATEGORIES 之一
    title: str
    body: str
    priority: str              # low|normal|high
    created_at: int            # Unix ms
    expires_at: int            # Unix ms；过期不再弹出，可留历史
    target: NotificationTarget | None = None
    actions: tuple[NotificationAction, ...] = ()
    group_key: str | None = None
    collapse_key: str | None = None

    def to_payload(self) -> dict:
        """产出严格对齐 schema 的 payload dict（可选字段缺省不输出）。"""
        data: dict[str, Any] = {
            "event_id": self.event_id,
            "notification_id": self.notification_id,
            "device_id": self.device_id,
            "category": self.category,
            "title": self.title,
            "body": self.body,
            "priority": self.priority,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }
        if self.target is not None:
            data["target"] = self.target.to_payload()
        if self.actions:
            data["actions"] = [a.to_payload() for a in self.actions]
        if self.group_key:
            data["group_key"] = self.group_key
        if self.collapse_key:
            data["collapse_key"] = self.collapse_key
        return data

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> NotificationEvent:
        """从 payload dict 还原（队列/历史反序列化用；宽松读取）。"""
        target = data.get("target")
        actions = tuple(NotificationAction.from_payload(a)
                        for a in (data.get("actions") or ()))
        return cls(
            event_id=str(data["event_id"]),
            notification_id=str(data["notification_id"]),
            device_id=str(data["device_id"]),
            category=str(data["category"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            priority=str(data.get("priority") or "normal"),
            created_at=int(data.get("created_at") or 0),
            expires_at=int(data.get("expires_at") or 0),
            target=(NotificationTarget.from_payload(target)
                    if isinstance(target, Mapping) else None),
            actions=actions,
            group_key=(str(data["group_key"]) if data.get("group_key") else None),
            collapse_key=(str(data["collapse_key"])
                          if data.get("collapse_key") else None),
        )


# ---------------------------------------------------------------------------
# 送达台账
# ---------------------------------------------------------------------------


@dataclass
class DeliveryRecord:
    """一条送达台账（kv 持久化于 ``notify:delivery:{device}:{notif_id}``）。

    可变dataclass（状态推进改自身）；对外读取请用 ``get_delivery`` 快照。
    """

    delivery_id: str           # provider 返回的送达标识（如 wss:evt_...）
    event_id: str
    notification_id: str
    device_id: str
    provider: str              # 实际承运 provider 名；未送达为 ""
    status: DeliveryStatus = DeliveryStatus.CREATED
    created_at: int = 0
    updated_at: int = 0
    attempts: int = 0          # 送达尝试次数（观测用）
    last_error: str = ""       # 末次失败原因（类型名/错误码，不含正文）
    revoked: bool = False      # 已被 notification.revoke 撤回

    def advance(self, status: DeliveryStatus, *, at: int, reason: str = "") -> bool:
        """按状态机推进；非法迁移/终态后翻案 → 返回 False 且不改动。

        幂等：推进到当前同一状态视为成功（重复回执不报错）。
        """
        if status is self.status:
            self.updated_at = at
            if reason:
                self.last_error = reason
            return True
        allowed = _ALLOWED_TRANSITIONS.get(self.status, frozenset())
        if status not in allowed:
            return False
        self.status = status
        self.updated_at = at
        if reason:
            self.last_error = reason
        return True

    def to_dict(self) -> dict:
        return {
            "delivery_id": self.delivery_id,
            "event_id": self.event_id,
            "notification_id": self.notification_id,
            "device_id": self.device_id,
            "provider": self.provider,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeliveryRecord:
        return cls(
            delivery_id=str(data.get("delivery_id") or ""),
            event_id=str(data.get("event_id") or ""),
            notification_id=str(data.get("notification_id") or ""),
            device_id=str(data.get("device_id") or ""),
            provider=str(data.get("provider") or ""),
            status=DeliveryStatus(str(data.get("status") or "created")),
            created_at=int(data.get("created_at") or 0),
            updated_at=int(data.get("updated_at") or 0),
            attempts=int(data.get("attempts") or 0),
            last_error=str(data.get("last_error") or ""),
            revoked=bool(data.get("revoked") or False),
        )
