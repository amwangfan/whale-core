"""D2/D3：DevicePerceptionAdapter —— 设备上行事件进入 perception/ 的入口。

实现 ``perception/adapters.py`` 的 ``PerceptionAdapter`` 协议：WS 网关线程把
已过幂等去重的 ``sensor.event`` 转成 ``PerceptionEvent`` 放进内存队列，
智能层经 ``poll()`` 拉取。事件幂等去重的持久化在网关侧（store kv），
本适配器只做内存中转，本身不落盘。

标记约定（D3）：payload 固定带 ``source="screenmemo"``、``device_id``、
``privacy_class``、事件真实发生时间 ``occurred_at``（毫秒，另映射到
``occurred_at`` 字段）；摘要不进日志、不进入默认全局召回（召回策略由
perception/reasoner 决定，本层只搬运）。
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from whale_core.perception.events import PerceptionEvent

#: 内存队列上限（背压保护：智能层长期不 poll 时丢弃最旧，保最新）
MAX_QUEUED_EVENTS = 1000


class DevicePerceptionAdapter:
    """线程安全的内存事件队列；poll 语义对齐 PerceptionAdapter 协议。"""

    def __init__(self, *, max_queued: int = MAX_QUEUED_EVENTS) -> None:
        self._queue: deque[PerceptionEvent] = deque()
        self._lock = threading.Lock()
        self._max_queued = max_queued
        self.dropped = 0  # 溢出丢弃计数（可观测性）

    def offer(self, *, device_id: str, payload: Mapping[str, Any]) -> PerceptionEvent:
        """网关线程调用：把一个已过 schema 校验与去重的 sensor.event 入队。"""
        occurred_ms = int(payload.get("occurred_at") or 0)
        event = PerceptionEvent(
            kind=str(payload.get("kind") or "device.unknown"),
            event_id=str(payload.get("event_id")),
            occurred_at=datetime.fromtimestamp(occurred_ms / 1000, tz=timezone.utc),
            payload={
                "source": "screenmemo",
                "device_id": device_id,
                "privacy_class": str(payload.get("privacy_class") or ""),
                "summary": str(payload.get("summary") or ""),
                "evidence_ref": payload.get("evidence_ref"),
                "schema_version": str(payload.get("schema_version") or ""),
                "occurred_at_ms": occurred_ms,
            },
        )
        with self._lock:
            while len(self._queue) >= self._max_queued:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(event)
        return event

    def poll(self) -> Iterator[PerceptionEvent]:
        """拉取并清空当前可用事件（可为空迭代器）。"""
        with self._lock:
            batch = list(self._queue)
            self._queue.clear()
        return iter(batch)


class DurableDevicePerceptionAdapter(DevicePerceptionAdapter):
    """At-least-once sensor inbox backed by the gateway's persisted event records.

    The gateway commits device:event:* BEFORE acknowledging to the device.
    This adapter reconstructs pending work after restart, and only removes it
    after the consumer explicitly acknowledges successful processing.
    """

    def __init__(self, store, *, max_queued: int = MAX_QUEUED_EVENTS):
        super().__init__(max_queued=max_queued)
        self.store = store

    def offer(self, *, device_id: str, payload: Mapping[str, Any]) -> PerceptionEvent:
        # Gateway owns durable write and dedup; no second volatile enqueue.
        return self._event(device_id, payload)

    @staticmethod
    def _event(device_id: str, payload: Mapping[str, Any]) -> PerceptionEvent:
        occurred_ms = int(payload.get("occurred_at") or 0)
        return PerceptionEvent(
            kind=str(payload.get("kind") or "device.unknown"),
            event_id=str(payload.get("event_id")),
            occurred_at=datetime.fromtimestamp(occurred_ms / 1000, tz=timezone.utc),
            payload={
                "source": "screenmemo",
                "device_id": device_id,
                "privacy_class": str(payload.get("privacy_class") or ""),
                "summary": str(payload.get("summary") or ""),
                "evidence_ref": payload.get("evidence_ref"),
                "schema_version": str(payload.get("schema_version") or ""),
                "occurred_at_ms": occurred_ms,
            },
        )

    def poll(self) -> Iterator[PerceptionEvent]:
        # Intentionally non-destructive; callers MUST ack after processing.
        # If a process crashes after poll, the next process sees the same work.
        entries = []
        for key in self.store.keys():
            if not key.startswith("device:event:"):
                continue
            record = self.store.get(key)
            if not isinstance(record, dict):
                continue
            device_id = str(record.get("device_id") or "")
            event_id = str(record.get("event_id") or "")
            if not device_id or not event_id:
                continue
            if self.store.get(self._ack_key(device_id, event_id)):
                continue
            entries.append(record)
        entries.sort(key=lambda r: (int(r.get("received_at") or 0),
                                    str(r.get("device_id") or ""),
                                    str(r.get("event_id") or "")))
        return iter(self._event(str(p["device_id"]), p)
                    for p in entries[:self._max_queued])

    @staticmethod
    def _ack_key(device_id: str, event_id: str) -> str:
        return f"device:processed:{device_id}:{event_id}"

    def ack(self, *, device_id: str, event_id: str) -> None:
        """Mark a successfully consumed event; do not ack merely on poll."""
        if not device_id or not event_id:
            raise ValueError("device_id and event_id are required")
        key = f"device:event:{device_id}:{event_id}"
        if self.store.get(key) is None:
            raise ValueError("cannot acknowledge an unknown sensor event")
        self.store.put(self._ack_key(device_id, event_id), {"processed": True})
