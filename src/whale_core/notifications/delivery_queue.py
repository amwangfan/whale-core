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

import heapq
import threading
from collections import deque
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
SWEEP_MAX_SCANNED = 500
SCAN_PAGE_SIZE = 500
#: delivery 台账保留期：终态后 7 天删除（对齐 v1 ACTION_RETENTION_MS）
DELIVERY_RETENTION_MS = 7 * 24 * 3_600_000


def queue_key(device_id: str, event_id: str) -> str:
    return f"{QUEUE_PREFIX}{device_id}:{event_id}"


def delivery_key(device_id: str, notification_id: str) -> str:
    return f"{DELIVERY_PREFIX}{device_id}:{notification_id}"


class DeliveryQueue:
    """待送达队列 + 送达台账；复合状态更新走底层 store 原子事务。"""

    def __init__(self, store, *, max_per_device: int = MAX_QUEUE_PER_DEVICE,
                 now_fn: Callable[[], int] | None = None) -> None:
        if store is None:
            raise ValueError("store 必填（MemoryStore kv 抽象）")
        if type(max_per_device) is not int or max_per_device <= 0:
            raise ValueError("max_per_device 须为正整数")
        self._store = store
        self.max_per_device = int(max_per_device)
        self._now = now_fn or now_ms
        self._maintenance_lock = threading.RLock()
        self._expiry_cursor = ""
        self._delivery_cursor = ""

    # ------------------------------------------------------------------
    # 队列（待送达事件）
    # ------------------------------------------------------------------

    def enqueue(self, event: NotificationEvent) -> None:
        """入队（同 event_id 覆盖，幂等）；超容量逐出最旧项。"""
        device_id = event.device_id
        key = queue_key(device_id, event.event_id)
        def apply(tx):
            existing = tx.get(key)
            tx.put(key, {
                "event": event.to_payload(),
                "enqueued_at": self._now(),
                "sent_at": existing.get("sent_at") if isinstance(existing, Mapping) else None,
            })
            self._enforce_capacity(device_id, tx)
        self._store.transact(apply)

    def get_entry(self, device_id: str, event_id: str) -> dict | None:
        """只读单个队列项（观测/测试用）。"""
        entry = self._store.get(queue_key(device_id, event_id))
        return dict(entry) if isinstance(entry, Mapping) else None

    def mark_sent(self, device_id: str, event_id: str, *, at: int | None = None) -> bool:
        """标记已送达（记 sent_at，**不出队**——等端侧回执或过期才出队）。"""
        key = queue_key(device_id, event_id)
        def apply(tx):
            entry = tx.get(key)
            if not isinstance(entry, Mapping):
                return False
            entry = dict(entry)
            entry["sent_at"] = self._now() if at is None else at
            tx.put(key, entry)
            return True
        return self._store.transact(apply)

    def ack_event(self, device_id: str, event_id: str) -> bool:
        """端侧已确认（收到任意有效回执）→ 出队，后续 sync 不再补发。"""
        key = queue_key(device_id, event_id)
        def apply(tx):
            if tx.get(key) is None:
                return False
            tx.delete(key)
            return True
        return self._store.transact(apply)

    def pending_events(self, device_id: str) -> list[NotificationEvent]:
        """该设备全部在队事件（按 event_id 升序 = 时间序）。"""
        prefix = f"{QUEUE_PREFIX}{device_id}:"
        events: list[NotificationEvent] = []
        for key in self._iter_keys(prefix):
            entry = self._store.get(key)
            event = self._decode_event(key, entry)
            if event is not None:
                events.append(event)
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
                        max_deletes: int = SWEEP_MAX_DELETES,
                        max_scanned: int = SWEEP_MAX_SCANNED) -> list[tuple[str, str]]:
        """过期清扫：删除过期队列项并把对应台账推进到 EXPIRED（单趟有界）。

        返回 ``(device_id, event_id)`` 列表（观测/测试用）。
        """
        at = self._now() if now is None else now
        def visit(key):
            entry = self._store.get(key)
            event = self._decode_event(key, entry)
            if event is None or event.expires_at > at:
                return None
            def expire(tx):
                # Re-read inside the transaction; acknowledgement may have won.
                current = tx.get(key)
                if current != entry:
                    return False
                tx.delete(key)
                dkey = delivery_key(event.device_id, event.notification_id)
                data = tx.get(dkey)
                if isinstance(data, Mapping):
                    try:
                        record = DeliveryRecord.from_dict(data)
                    except (TypeError, ValueError, OverflowError):
                        record = None
                    if record is not None and record.event_id == event.event_id and not record.revoked \
                            and not record.status.terminal:
                        record.advance(DeliveryStatus.EXPIRED, at=at, reason="ttl_elapsed")
                        tx.put(dkey, record.to_dict())
                return True
            return (event.device_id, event.event_id) if self._store.transact(expire) else None
        return self._sweep_keys(QUEUE_PREFIX, "_expiry_cursor", visit,
                                max_scanned=max_scanned, max_deletes=max_deletes)

    def _enforce_capacity(self, device_id: str, tx) -> None:
        """容量纪律：超上限逐出最旧（event_id 字典序最小者）。"""
        prefix = f"{QUEUE_PREFIX}{device_id}:"
        kept = deque()
        for key in self._iter_keys(prefix):
            if len(kept) == self.max_per_device:
                tx.delete(kept.popleft())
            kept.append(key)

    def _iter_keys(self, prefix: str, *, after: str = "", max_keys: int | None = None):
        """Native prefix paging; legacy stores retain a keys() fallback."""
        remaining = max_keys
        scan = getattr(self._store, "scan_keys", None)
        while remaining is None or remaining > 0:
            limit = SCAN_PAGE_SIZE if remaining is None else min(SCAN_PAGE_SIZE, remaining)
            if callable(scan):
                page = scan(prefix, after=after, limit=limit)
            else:
                page = heapq.nsmallest(limit, (key for key in self._store.keys()
                                              if key.startswith(prefix) and key > after))
            if not page:
                return
            yield from page
            after = page[-1]
            if remaining is not None:
                remaining -= len(page)
            if len(page) < limit:
                return

    @staticmethod
    def _decode_event(key: str, entry) -> NotificationEvent | None:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("event"), Mapping):
            return None
        try:
            event = NotificationEvent.from_payload(entry["event"])
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return None
        return event if queue_key(event.device_id, event.event_id) == key else None

    def _sweep_keys(self, prefix, cursor_name, visit, *, max_scanned, max_deletes):
        for value in (max_scanned, max_deletes):
            if type(value) is not int or value < 0:
                raise ValueError("sweep budgets must be nonnegative integers")
        if not max_scanned or not max_deletes:
            return []
        with self._maintenance_lock:
            results = []
            scanned = 0
            for key in self._iter_keys(prefix, after=getattr(self, cursor_name), max_keys=max_scanned):
                result = visit(key)
                # Commit progress only after visit succeeds; exceptions retry this key.
                setattr(self, cursor_name, key)
                scanned += 1
                if result is not None:
                    results.append(result)
                if scanned >= max_scanned or len(results) >= max_deletes:
                    break
            else:
                setattr(self, cursor_name, "")
            return results

    # ------------------------------------------------------------------
    # 送达台账（DeliveryRecord）
    # ------------------------------------------------------------------

    def put_delivery(self, record: DeliveryRecord) -> None:
        self._store.put(delivery_key(record.device_id, record.notification_id),
                        record.to_dict())

    def update_delivery(self, device_id: str, notification_id: str,
                        change: Callable[[DeliveryRecord], bool],
                        *, event_id: str | None = None, acknowledge: bool = False,
                        initial: DeliveryRecord | None = None) -> tuple[DeliveryRecord | None, bool]:
        """Read/check/mutate/ack atomically; change must have no external effects.

        Returns the latest matching snapshot and whether change accepted it.
        External handlers/providers run only after this transaction commits.
        """
        key = delivery_key(device_id, notification_id)

        def apply(tx):
            data = tx.get(key)
            created = not isinstance(data, Mapping) and initial is not None
            if isinstance(data, Mapping):
                record = DeliveryRecord.from_dict(data)
            else:
                record = DeliveryRecord.from_dict(initial.to_dict()) if initial is not None else None
            if record is None or (event_id is not None and record.event_id != event_id):
                return None, False
            changed = change(record)
            if changed or created:
                tx.put(key, record.to_dict())
            if acknowledge:
                tx.delete(queue_key(device_id, record.event_id))
            return record, changed

        return self._store.transact(apply)

    def accept_receipt(self, device_id: str, notification_id: str, event_id: str,
                       status: DeliveryStatus, *, at: int, reason: str = "",
                       action_id: str = "") -> tuple[DeliveryRecord | None, bool]:
        """Accept a receipt, or claim one action callback, against current state."""
        def change(record):
            if record.revoked:
                return False
            if action_id:
                if record.action_id or (record.status.terminal and
                                        record.status is not DeliveryStatus.ACTION_SUBMITTED):
                    return False
            elif record.status.terminal or record.status is status:
                return False
            # A valid device receipt proves delivery even if send() is pending.
            if record.status in (DeliveryStatus.CREATED, DeliveryStatus.QUEUED) and status in (
                    DeliveryStatus.DISPLAYED, DeliveryStatus.OPENED, DeliveryStatus.DISMISSED,
                    DeliveryStatus.ACTION_SUBMITTED):
                record.advance(DeliveryStatus.DELIVERED, at=at)
            if not record.advance(status, at=at, reason=reason):
                return False
            if action_id:
                record.action_id = action_id
            return True

        return self.update_delivery(device_id, notification_id, change,
                                    event_id=event_id, acknowledge=True)

    def revoke_delivery(self, device_id: str, notification_id: str,
                        *, at: int) -> DeliveryRecord | None:
        def change(record):
            if record.revoked:
                return False
            record.revoked = True
            record.updated_at = at
            return True
        return self.update_delivery(device_id, notification_id, change, acknowledge=True)[0]

    def get_delivery(self, device_id: str,
                     notification_id: str) -> DeliveryRecord | None:
        data = self._store.get(delivery_key(device_id, notification_id))
        if not isinstance(data, Mapping):
            return None
        return DeliveryRecord.from_dict(data)

    def sweep_deliveries(self, *, now: int | None = None,
                         max_age_ms: int = DELIVERY_RETENTION_MS,
                         max_deletes: int = SWEEP_MAX_DELETES,
                         max_scanned: int = SWEEP_MAX_SCANNED) -> int:
        """台账保留期清扫：终态且超过 max_age_ms 的记录删除（单趟有界）。"""
        if type(max_age_ms) is not int or max_age_ms < 0:
            raise ValueError("max_age_ms must be a nonnegative integer")
        at = self._now() if now is None else now
        def visit(key):
            data = self._store.get(key)
            if not isinstance(data, Mapping):
                return None
            try:
                record = DeliveryRecord.from_dict(data)
            except (TypeError, ValueError, OverflowError):
                return None
            if record.status.terminal and at - record.updated_at > max_age_ms:
                def remove(tx):
                    # A concurrent receipt/revoke may have refreshed this record.
                    if tx.get(key) != data:
                        return False
                    tx.delete(key)
                    return True
                if self._store.transact(remove):
                    return key
            return None
        return len(self._sweep_keys(DELIVERY_PREFIX, "_delivery_cursor", visit,
                                    max_scanned=max_scanned, max_deletes=max_deletes))
