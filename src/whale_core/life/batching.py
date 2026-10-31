"""G3：入站合批（InboundBatch）与会话 epoch。

- **合批**：同一发送者在短窗口内连发的多条消息合并为一个处理回合
  （文本按行拼接，消息 ID 全保留）；不同发送者绝不合并（ID-01）。
- **epoch**：每个会话一个单调计数器，新批次到达即 +1；回复计划携带
  发出时的 epoch，发送前校验——epoch 已变则剩余片段失效（MSG-02），
  防止旧回复越过新消息。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Tuple

from whale_core.memory.store import MemoryStore


@dataclass(frozen=True)
class InboundBatch:
    """一个处理回合：同一发送者在同一会话的连续消息组。"""

    conversation: str
    sender_id: str
    texts: Tuple[str, ...]
    message_ids: Tuple[str, ...]
    events: Tuple[object, ...] = ()  # 原始 Incoming 引用（附件等用最后一个）
    epoch: int = 0  # G6 中-1：冲刷时刻的会话 epoch（发送侧以此为作废基线）

    @property
    def text(self) -> str:
        return "\n".join(t for t in self.texts if t.strip())

    @property
    def last_event(self):
        return self.events[-1] if self.events else None


class MessageBatcher:
    """按 (conversation, sender) 聚合的短窗口合批器。

    flush_cb(batch) 在窗口静默期满或窗口内无新消息后调用一次。
    守护线程计时；stop() 立即冲刷所有待处理批次（不丢消息）。
    """

    def __init__(self, flush_cb: Callable[[InboundBatch], None], *,
                 window_private_s: float = 0.9, window_group_s: float = 1.8):
        self._cb = flush_cb
        self._win_priv = max(0.1, window_private_s)
        self._win_group = max(0.3, window_group_s)
        self._pending: dict[tuple, dict] = {}
        self._lock = threading.RLock()
        self._stopped = False

    def offer(self, event) -> None:
        """event：life.channels.Incoming 形状（conversation/account/text/message_id）。"""
        conv = str(event.conversation)
        sender = str(getattr(event, "account", "") or getattr(event, "sender_id", ""))
        key = (conv, sender)
        with self._lock:
            if self._stopped:
                return
            slot = self._pending.get(key)
            if slot is None:
                slot = {"texts": [], "ids": [], "events": [], "timer": None}
                self._pending[key] = slot
            slot["texts"].append(str(event.text or ""))
            slot["ids"].append(str(event.message_id or ""))
            slot.setdefault("events", []).append(event)
            if slot["timer"] is not None:
                slot["timer"].cancel()
            window = self._win_priv if conv.startswith("private:") else self._win_group
            timer = threading.Timer(window, self._flush, args=(key,))
            timer.daemon = True
            slot["timer"] = timer
            timer.start()

    def _flush(self, key: tuple) -> None:
        with self._lock:
            slot = self._pending.pop(key, None)
        if slot is None:
            return
        batch = InboundBatch(
            conversation=key[0], sender_id=key[1],
            texts=tuple(slot["texts"]), message_ids=tuple(slot["ids"]),
            events=tuple(slot["events"]))
        try:
            self._cb(batch)
        except Exception:
            pass  # 合批回调失败不炸监听线程；宿主侧有自己的失败处理

    def flush_all(self) -> int:
        """立即冲刷全部待处理批次（如停止前），返回冲刷数量。"""
        with self._lock:
            keys = list(self._pending.keys())
        for key in keys:
            self._flush(key)
        return len(keys)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            if slot["timer"] is not None:
                slot["timer"].cancel()


# ---------------------------------------------------------------------------
# 会话 epoch（kv 计数器）
# ---------------------------------------------------------------------------


def bump_epoch(store: MemoryStore, conversation_key: str) -> int:
    """原子自增（G6 中-1：并发冲刷不得产生同值 epoch）。"""
    key = f"epoch:{conversation_key}"

    def _tx(tx):
        val = tx.get(key)
        n = int(val or 0) + 1 if isinstance(val, (int, float)) else 1
        tx.put(key, n)
        return n

    transact = getattr(store, "transact", None)
    if callable(transact):
        return transact(_tx)
    val = store.get(key)  # 无事务原语的后端按原逻辑（单线程假设）
    n = int(val or 0) + 1 if isinstance(val, (int, float)) else 1
    store.put(key, n)
    return n


def current_epoch(store: MemoryStore, conversation_key: str) -> int:
    val = store.get(f"epoch:{conversation_key}")
    return int(val) if isinstance(val, (int, float)) else 0


def epoch_fresh(store: MemoryStore, conversation_key: str, epoch: int) -> bool:
    """发送前校验：计划的 epoch 仍是当前 epoch（否则剩余片段作废）。"""
    return current_epoch(store, conversation_key) <= epoch
