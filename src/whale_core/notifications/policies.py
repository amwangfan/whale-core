"""通知策略：每分类 TTL / 优先级 / Android channel 默认表（修正方案 §6.3）。

默认原则（contracts/v2 §13.1）：
- 不同业务不同 TTL：聊天 1h、审批 10min、任务完成 24h、设备离线 30min、
  提醒按事件自身 expires_at……过期不再弹出，可留通知历史；
- ``high`` 只给确需及时处理的分类（审批请求、任务失败、提醒触发），不滥用；
- ``task.progress`` 高频更新，走折叠合并（collapse_key 同键新替旧）；
- 全部默认值可经 ``PolicyTable(overrides=...)`` 按分类覆盖（部署侧配置）。

Android channel 映射（端侧 NotificationManager 渠道 id）：
``whale_chat / whale_task / whale_approval / whale_device / whale_reminder /
whale_system``。channel 只影响端侧展示分组与免打扰策略，不参与选路。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from whale_core.notifications.models import CATEGORIES, PRIORITIES

#: 兜底 TTL（分类 ttl_ms=None 且调用方未给 expires_at 时）：1 小时
FALLBACK_TTL_MS = 3_600_000

#: 分类 → Android channel（修正方案 §6.3 映射表）
CATEGORY_CHANNELS: dict[str, str] = {
    "chat.message": "whale_chat",
    "task.started": "whale_task",
    "task.progress": "whale_task",
    "task.completed": "whale_task",
    "task.failed": "whale_task",
    "approval.requested": "whale_approval",
    "device.offline": "whale_device",
    "device.online": "whale_device",
    "memory.updated": "whale_system",
    "reminder.triggered": "whale_reminder",
}


@dataclass(frozen=True)
class CategoryPolicy:
    """单分类策略。``ttl_ms=None`` 表示「按事件自带 expires_at」（提醒类）。"""

    ttl_ms: int | None
    priority: str              # low | normal | high
    channel: str               # Android channel id
    collapse: bool = False     # True → 未显式给 collapse_key 时自动按键合并

    def __post_init__(self) -> None:
        if self.priority not in PRIORITIES:
            raise ValueError(f"未知优先级: {self.priority!r}")
        if self.ttl_ms is not None and self.ttl_ms <= 0:
            raise ValueError("ttl_ms 须为正数或 None")
        if not self.channel:
            raise ValueError("channel 必填")


def _default_policies() -> dict[str, CategoryPolicy]:
    """出厂默认表（值见模块 docstring；改动请先同步修正方案）。"""
    def ch(category: str) -> str:
        return CATEGORY_CHANNELS[category]

    return {
        # 聊天消息：1 小时有效，正常优先级
        "chat.message": CategoryPolicy(3_600_000, "normal", ch("chat.message")),
        # 任务开始：30 分钟，低优先级（知道了就行）
        "task.started": CategoryPolicy(1_800_000, "low", ch("task.started")),
        # 任务进度：5 分钟 + 折叠合并（高频更新只留最新一条）
        "task.progress": CategoryPolicy(300_000, "low", ch("task.progress"),
                                        collapse=True),
        # 任务完成：24 小时，正常优先级
        "task.completed": CategoryPolicy(86_400_000, "normal",
                                         ch("task.completed")),
        # 任务失败：24 小时，高优先级（失败需要主人及时处理）
        "task.failed": CategoryPolicy(86_400_000, "high", ch("task.failed")),
        # 审批请求：10 分钟，高优先级（审批本身有有效期，通知不该比审批活得久）
        "approval.requested": CategoryPolicy(600_000, "high",
                                             ch("approval.requested")),
        # 设备离线：30 分钟，正常优先级
        "device.offline": CategoryPolicy(1_800_000, "normal",
                                         ch("device.offline")),
        # 设备上线：5 分钟，低优先级（转瞬即过时）
        "device.online": CategoryPolicy(300_000, "low", ch("device.online")),
        # 记忆更新：6 小时，低优先级
        "memory.updated": CategoryPolicy(21_600_000, "low",
                                         ch("memory.updated")),
        # 提醒触发：按事件自带 expires_at（提醒业务知道自已的截止时刻），
        # 高优先级；调用方未给 expires_at 时兜底 FALLBACK_TTL_MS
        "reminder.triggered": CategoryPolicy(None, "high",
                                             ch("reminder.triggered")),
    }


#: 模块级默认表（只读使用；要改请构造 PolicyTable(overrides=...)）
DEFAULT_POLICIES: dict[str, CategoryPolicy] = _default_policies()


def channel_for(category: str) -> str:
    """分类 → Android channel id；未知分类抛 ValueError。"""
    if category not in CATEGORY_CHANNELS:
        raise ValueError(f"未知通知分类: {category!r}")
    return CATEGORY_CHANNELS[category]


class PolicyTable:
    """策略表：默认表 + 可选覆盖。未知分类一律 ValueError（fail closed）。"""

    def __init__(self,
                 overrides: Mapping[str, CategoryPolicy] | None = None) -> None:
        self._policies = dict(DEFAULT_POLICIES)
        for category, policy in (overrides or {}).items():
            if category not in CATEGORIES:
                raise ValueError(f"未知通知分类: {category!r}")
            if not isinstance(policy, CategoryPolicy):
                raise TypeError("overrides 值必须是 CategoryPolicy")
            self._policies[category] = policy

    def policy_for(self, category: str) -> CategoryPolicy:
        if category not in CATEGORIES:
            raise ValueError(f"未知通知分类: {category!r}")
        return self._policies[category]

    def with_override(self, category: str, **changes) -> PolicyTable:
        """派生新表：对单分类做字段级覆盖（如 ttl_ms=120_000）。"""
        current = self.policy_for(category)
        return PolicyTable({category: replace(current, **changes)})

    def resolve_expires_at(self, category: str, *, created_at: int,
                           expires_at: int | None) -> int:
        """计算 expires_at：显式值 > 分类 TTL > 兜底 TTL。

        reminder.triggered 等 ttl_ms=None 的分类：优先用调用方给的
        expires_at，缺省退到兜底（绝不产生永不过期的通知）。
        """
        policy = self.policy_for(category)
        if expires_at is not None:
            resolved = int(expires_at)
        else:
            ttl = policy.ttl_ms if policy.ttl_ms is not None else FALLBACK_TTL_MS
            resolved = int(created_at) + ttl
        if resolved <= created_at:
            raise ValueError("expires_at 必须晚于 created_at（已过期通知不应发出）")
        return resolved
