"""送达队列与台账持久化（注入 MemoryStore kv，双后端语义一致）。

kv 键域（对齐 devices v1 的 kv 约定，值为 JSON 可序列化 dict）：

| 键模式 | 值 | 写入方 |
|---|---|---|
| ``notify:queue:{device}:{event_id}`` | ``{"event": payload, "enqueued_at", "sent_at"}`` | 本模块 |
| ``notify:delivery:{device}:{notif_id}`` | ``DeliveryRecord.to_dict()`` | 本模块 |

设计要点：
- **至少一次送达**：``mark_sent`` 只在队列项上记 ``sent_at``，**不出队**——
  端侧回执（receipt/action）到达后 ``ack_event`` 才出队；设备重连
  ``notification.sync`` 时 ``replay_since`` 补发仍在有效期内的队列项，
  端侧按 event_id 去重（contracts/v2 §13.3），重复展示由端侧防；
- **容量有界**：每设备队列上限 ``max_per_device``（默认 200），超限逐出
  最旧项（event_id 内嵌 ULID 时间戳，字典序≈时间序；同一毫秒内创建的
  事件序按随机尾数字典序，是近似最旧——可接受，淘汰纪律只为有界性服务）；
- ULID 比较：``replay_since(last_event_id)`` 直接用字符串 ``>`` 比较
  （``evt_`` 前缀等长，ULID 前 10 字符是毫秒时间戳）。端侧「已处理的最
  大事件 ID」同样按字符串取 max，两侧口径一致；正确性由 event_id 幂等
  去重保证，不依赖时间序的严格性。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from whale_core.devices.transports.base import now_ms
from whale_core.notifications.models import (
    DeliveryRecord,
    DeliveryStatus,
    NotificationEvent,
)

#: kv 键域前缀
QUEUE_PREFIX = "notify:queue:"
DELIVERY_PREFIX = "notify:delivery:"

#: 每设备待送达队列上限（超出逐出最旧）
MAX_QUEUE_PER_DEVICE = 200
#: 单趟清扫删除上限（有界清扫，对齐 v1 sweep_action_retention）
SWEEP_MAX_DELETES = 500
#: delivery 台账保留期：终态后 7 天删除（对齐 v1 ACTION_RETENTION_MS）
DELIVERY_RETENTION_MS = 7 * 24 * 3_600_000


def queue_key(device_id: str, event_id: str) -> str:
    return f"{QUEUE_PREFIX}{device_id}:{event_id}"


def delivery_key(device_id: str, notification_id: str) -> str:
    return f"{DELIVERY_PREFIX}{device_id}:{notification_id}"


class DeliveryQueue:
    """待送达队列 + 送达台账。线程安全交给底层 store（InMemory/Sqlite 均有锁）。"""

    def __init__(self, store, *, max_per_device: int = MAX_QUEUE_PER_DEVICE,
                 now_fn: Callable[[], int] | None = None) -> None:
        if store is None:
            raise ValueError("store 必填（MemoryStore kv 抽象）")
        if max_per_device <= 0:
            raise ValueError("max_per_device 须为正整数")
        self._store = store
        self.max_per_device = int(max_per_device)
        self._now = now_fn or now_ms

    # ------------------------------------------------------------------
    # 队列（待送达事件）
    # ------------------------------------------------------------------

    def enqueue(self, event: NotificationEvent) -> None:
        """入队（同 event_id 覆盖，幂等）；超容量逐出最旧项。"""
        device_id = event.device_id
        key = queue_key(device_id, event.event_id)
        existing = self._store.get(key)
        entry = {
            "event": event.to_payload(),
            "enqueued_at": self._now(),
            # 重投时保留原 sent_at（重复入队不清空送达痕迹）
            "sent_at": (existing or {}).get("sent_at"),
        }
        self._store.put(key, entry)
        self._enforce_capacity(device_id)

    def get_entry(self, device_id: str, event_id: str) -> dict | None:
        """只读单个队列项（观测/测试用）。"""
        entry = self._store.get(queue_key(device_id, event_id))
        return dict(entry) if isinstance(entry, Mapping) else None

    def mark_sent(self, device_id: str, event_id: str, *, at: int | None = None) -> bool:
        """标记已送达（记 sent_at，**不出队**——等端侧回执或过期才出队）。"""
        key = queue_key(device_id, event_id)
        entry = self._store.get(key)
        if not isinstance(entry, Mapping):
            return False
        entry = dict(entry)
        entry["sent_at"] = self._now() if at is None else at
        self._store.put(key, entry)
        return True

    def ack_event(self, device_id: str, event_id: str) -> bool:
        """端侧已确认（收到任意有效回执）→ 出队，后续 sync 不再补发。"""
        key = queue_key(device_id, event_id)
        if self._store.get(key) is None:
            return False
        self._store.delete(key)
        return True

    def pending_events(self, device_id: str) -> list[NotificationEvent]:
        """该设备全部在队事件（按 event_id 升序 = 时间序）。"""
        prefix = f"{QUEUE_PREFIX}{device_id}:"
        events: list[NotificationEvent] = []
        for key in self._store.keys():
            if not key.startswith(prefix):
                continue
            entry = self._store.get(key)
            if isinstance(entry, Mapping) and isinstance(entry.get("event"), Mapping):
                events.append(NotificationEvent.from_payload(entry["event"]))
        events.sort(key=lambda e: e.event_id)
        return events

    def replay_since(self, device_id: str, last_event_id: str | None = None,
                     *, now: int | None = None) -> list[NotificationEvent]:
        """补传候选：event_id > last_event_id 且未过期的在队事件（升序）。

        ``last_event_id=None`` 表示端侧无历史 → 全部未过期事件都补。
        已确认（回执出队）与已过期（清扫出队）的事件不会出现在结果里。
        """
        at = self._now() if now is None else now
        return [e for e in self.pending_events(device_id)
                if (last_event_id is None or e.event_id > last_event_id)
                and e.expires_at > at]

    def dequeue_expired(self, *, now: int | None = None,
                        max_deletes: int = SWEEP_MAX_DELETES) -> list[tuple[str, str]]:
        """过期清扫：删除过期队列项并把对应台账推进到 EXPIRED（单趟有界）。

        返回 ``(device_id, event_id)`` 列表（观测/测试用）。
        """
        at = self._now() if now is None else now
        removed: list[tuple[str, str]] = []
        for key in self._store.keys():
            if len(removed) >= max_deletes:
                break
            if not key.startswith(QUEUE_PREFIX):
                continue
            entry = self._store.get(key)
            if not isinstance(entry, Mapping):
                continue
            payload = entry.get("event")
            if not isinstance(payload, Mapping):
                continue
            try:
                event = NotificationEvent.from_payload(payload)
            except (KeyError, TypeError, ValueError):
                continue  # 畸形项留给容量逐出，不在清扫路径上放大故障
            if event.expires_at > at:
                continue
            self._store.delete(key)
            removed.append((event.device_id, event.event_id))
            record = self.get_delivery(event.device_id, event.notification_id)
            if record is not None:
                record.advance(DeliveryStatus.EXPIRED, at=at,
                               reason="ttl_elapsed")
                self.put_delivery(record)
        return removed

    def _enforce_capacity(self, device_id: str) -> None:
        """容量纪律：超上限逐出最旧（event_id 字典序最小者）。"""
        prefix = f"{QUEUE_PREFIX}{device_id}:"
        keys = sorted(k for k in self._store.keys() if k.startswith(prefix))
        overflow = len(keys) - self.max_per_device
        for key in keys[:max(0, overflow)]:
            self._store.delete(key)

    # ------------------------------------------------------------------
    # 送达台账（DeliveryRecord）
    # ------------------------------------------------------------------

    def put_delivery(self, record: DeliveryRecord) -> None:
        self._store.put(delivery_key(record.device_id, record.notification_id),
                        record.to_dict())

    def get_delivery(self, device_id: str,
                     notification_id: str) -> DeliveryRecord | None:
        data = self._store.get(delivery_key(device_id, notification_id))
        if not isinstance(data, Mapping):
            return None
        return DeliveryRecord.from_dict(data)

    def sweep_deliveries(self, *, now: int | None = None,
                         max_age_ms: int = DELIVERY_RETENTION_MS,
                         max_deletes: int = SWEEP_MAX_DELETES) -> int:
        """台账保留期清扫：终态且超过 max_age_ms 的记录删除（单趟有界）。"""
        at = self._now() if now is None else now
        deleted = 0
        for key in self._store.keys():
            if deleted >= max_deletes:
                break
            if not key.startswith(DELIVERY_PREFIX):
                continue
            data = self._store.get(key)
            if not isinstance(data, Mapping):
                continue
            record = DeliveryRecord.from_dict(data)
            if record.status.terminal and at - record.updated_at > max_age_ms:
                self._store.delete(key)
                deleted += 1
        return deleted
