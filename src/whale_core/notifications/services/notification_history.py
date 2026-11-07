"""通知历史：每设备最近 N=500 条，kv 持久化，可查询/标记已读。

kv 键域：``notify:history:{device}`` → JSON list（新在前，截断到 limit）。

诚实性纪律（§13.2）：「已读」是**用户在 Core/端侧 UI 里的显式动作**
（``mark_read``），送达回执链（尤其 displayed）绝不在此置位——
displayed ≠ 用户已看到。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from whale_core.devices.transports.base import now_ms
from whale_core.notifications.models import NotificationEvent

#: kv 键域前缀
HISTORY_PREFIX = "notify:history:"

#: 每设备历史上限（超出丢弃最旧）
DEFAULT_HISTORY_LIMIT = 500


def history_key(device_id: str) -> str:
    return f"{HISTORY_PREFIX}{device_id}"


class NotificationHistory:
    """每设备通知历史（新在前）。值为 JSON list，双后端语义一致。"""

    def __init__(self, store, *, limit: int = DEFAULT_HISTORY_LIMIT,
                 now_fn=None) -> None:
        if store is None:
            raise ValueError("store 必填（MemoryStore kv 抽象）")
        if limit <= 0:
            raise ValueError("limit 须为正整数")
        self._store = store
        self.limit = int(limit)
        self._now: Callable[[], int] = now_fn or now_ms

    def add(self, event: NotificationEvent) -> None:
        """记录一条通知（同 notification_id 覆盖更新，如分类内容修正重发）。"""
        entries = self._load(event.device_id)
        entries = [e for e in entries
                   if e.get("notification_id") != event.notification_id]
        entries.insert(0, {
            "notification_id": event.notification_id,
            "event_id": event.event_id,
            "device_id": event.device_id,
            "category": event.category,
            "title": event.title,
            "body": event.body,
            "priority": event.priority,
            "created_at": event.created_at,
            "expires_at": event.expires_at,
            "read": False,
        })
        del entries[self.limit:]
        self._store.put(history_key(event.device_id), entries)

    def list(self, device_id: str, *, limit: int | None = None,
             unread_only: bool = False) -> list[dict]:
        """查询历史（新在前）；``unread_only=True`` 只回未读。"""
        entries = self._load(device_id)
        if unread_only:
            entries = [e for e in entries if not e.get("read")]
        if limit is not None:
            entries = entries[:max(0, int(limit))]
        return entries

    def get(self, device_id: str, notification_id: str) -> dict | None:
        for entry in self._load(device_id):
            if eid_match(entry, notification_id):
                return entry
        return None

    def mark_read(self, device_id: str, notification_id: str) -> bool:
        """显式标记已读（用户动作）；不存在返回 False。"""
        entries = self._load(device_id)
        hit = False
        for entry in entries:
            if eid_match(entry, notification_id):
                entry["read"] = True
                hit = True
        if hit:
            self._store.put(history_key(device_id), entries)
        return hit

    def mark_all_read(self, device_id: str) -> int:
        """全部标记已读，返回本次置位条数。"""
        entries = self._load(device_id)
        count = 0
        for entry in entries:
            if not entry.get("read"):
                entry["read"] = True
                count += 1
        if count:
            self._store.put(history_key(device_id), entries)
        return count

    def unread_count(self, device_id: str) -> int:
        return sum(1 for e in self._load(device_id) if not e.get("read"))

    def _load(self, device_id: str) -> list[dict]:
        data = self._store.get(history_key(device_id))
        if not isinstance(data, list):
            return []
        return [dict(e) for e in data if isinstance(e, Mapping)]


def eid_match(entry: Mapping[str, Any], notification_id: str) -> bool:
    return entry.get("notification_id") == notification_id
