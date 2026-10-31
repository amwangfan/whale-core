"""issue #9：设备感知事件消费 → Initiative 决策 → 手机通知闭环。

管道（每环都可观测、可负例复现）：

1. **来源裁决**：事件必须来自 DeviceRegistry 中 active 且具备
   ``phone.notification.post`` 能力的已配对设备；revoked/未知设备 →
   丢弃并 ack（不再重投），计数可观测。
2. **隐私级**：``privacy_class=private`` 的事件不进任何主动提醒管道
   （不进入普通会话默认召回——本模块根本不读原始 OCR/截图，只吃
   设备侧已摘要的 summary，且 summary 一律按**不可信数据**处理：
   控制字符剥离、长度截断、只作为模板文本的宾语，永不拼进任何
   模型/系统指令）。
3. **低价值丢弃与合并**：空 summary、忽略类 kind 直接丢；同
   (device, kind) 在时间窗内合并，只留最新一条。
4. **规则化建议**（能用规则先用规则）：命中提醒关键词（截止/会议/缴费
   等）才生成候选通知文本；模板化生成，不调模型。
5. **策略闸门**：候选只进 ProactiveOutbox 草稿（channel=phone_notice），
   由 inspect 统一裁决静默/冷却/配额/全局暂停——设备事件触发的通知
   属于**主动**行为（response_origin=unsolicited，issue #22 补充：
   深夜不借「被动」名义越过静默）。
6. **幂等下发**：经 DeviceGateway.send_action（action.request），幂等键
   ``notify:{device}:{event}``；等待 executed/failed/unknown 终态回执。
7. **ack 纪律**：处理到达终态（已通知/应抑制/失败已记录）才
   ``adapter.ack``；回执 unknown → 记录 ``device:notify-state:`` =
   unknown 并 ack（不盲目重发——宁可漏报不可重复打扰）；崩溃恢复
   由持久 inbox 的未 ack 重投保证「至少一次」，幂等键保证「通知不重」。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox

#: 提醒候选的关键词（规则先行；只读设备摘要文本）。
_NOTIFY_HINT_RE = re.compile(
    r"截止|ddl|deadline|会议|上课|考试|缴费|还款|预约|取票|登机|"
    r"闹钟|提醒|别忘|记得要", re.I)

#: 不产出提醒的低价值 kind（心跳/状态类）。
_IGNORE_KIND_PREFIXES = ("device.heartbeat", "device.battery", "device.state")

#: 同 (device, kind) 合并窗口：窗口内只处理最新一条。
_MERGE_WINDOW = timedelta(minutes=10)

#: 摘要文本注入防护：剥离控制字符后的长度上限。
_SUMMARY_MAX = 160

#: 设备须具备的通知能力。
_NOTIFY_CAPABILITY = "phone.notification.post"

#: 回执终态记录键前缀（unknown 的可核对证据）。
_NOTIFY_STATE_PREFIX = "device:notify-state:"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_summary(text: Any) -> str:
    """设备摘要按不可信数据处理：剥离控制字符/压平空白/截断。"""
    cleaned = _CONTROL_RE.sub(" ", str(text or ""))
    return " ".join(cleaned.split())[:_SUMMARY_MAX]


class DeviceEventConsumer:
    """持久设备事件 → 授权通知的消费者（轮询式；宿主按节奏调用）。"""

    def __init__(self, store, adapter, *, gateway=None, registry=None,
                 outbox: Optional[ProactiveOutbox] = None,
                 prefs=None,
                 now_fn: Optional[Callable[[], datetime]] = None) -> None:
        self.store = store
        self.adapter = adapter
        self.gateway = gateway
        self.registry = registry
        self.outbox = outbox
        # 既有 Initiative 策略（静默/冷却/配额/全局暂停）由宿主注入；
        # 缺省见 _prefs()（保守：phone_notice 一档，静默窗默认生效）。
        self._injected_prefs = prefs
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # -- 单事件判定（纯规则，可单测） ----------------------------------------

    def _device_allowed(self, device_id: str) -> bool:
        if self.registry is None:
            return False  # 无注册表 → 不放行（fail closed）
        meta = self.registry.get(device_id)
        if not isinstance(meta, dict):
            return False
        if meta.get("status") != "active":
            return False
        caps = meta.get("capabilities") or []
        return _NOTIFY_CAPABILITY in caps

    def _should_notify(self, summary: str, kind: str) -> bool:
        if not summary:
            return False
        if any(kind.startswith(p) for p in _IGNORE_KIND_PREFIXES):
            return False
        return bool(_NOTIFY_HINT_RE.search(summary))

    # -- 主管道 ---------------------------------------------------------------

    def process_pending(self, *, max_events: int = 32) -> dict[str, int]:
        """处理持久 inbox 中的待办事件。返回计数统计（可观测）。"""
        stats = {"polled": 0, "dropped_unauthorized": 0, "dropped_private": 0,
                 "dropped_low_value": 0, "dropped_merged": 0,
                 "notified": 0, "suppressed_by_policy": 0, "failed": 0,
                 "unknown": 0, "acked": 0}
        events = list(self.adapter.poll())[:max(0, int(max_events))]
        stats["polled"] = len(events)

        # 同 (device, kind) 在合并窗口内只处理最新一条（时间窗合并）；
        # 窗口外的旧事件照常独立处理（不静默丢）。
        groups: dict[tuple, list] = {}
        for ev in events:
            key = (str(ev.payload.get("device_id") or ""), str(ev.kind))
            groups.setdefault(key, []).append(ev)
        merged_events: list = []
        for group_events in groups.values():
            group_events.sort(key=lambda e: e.occurred_at)
            keep = group_events[-1]
            for old in group_events[:-1]:
                if keep.occurred_at - old.occurred_at <= _MERGE_WINDOW:
                    stats["dropped_merged"] += 1
                    # 被合并的旧事件直接 ack（其内容已被更新事件覆盖）
                    self.adapter.ack(
                        device_id=str(old.payload.get("device_id") or ""),
                        event_id=str(old.event_id))
                    stats["acked"] += 1
                else:
                    merged_events.append(old)
            merged_events.append(keep)

        for ev in merged_events:
            device_id = str(ev.payload.get("device_id") or "")
            event_id = str(ev.event_id or "")
            if not device_id or not event_id:
                stats["dropped_low_value"] += 1
                continue
            if not self._device_allowed(device_id):
                stats["dropped_unauthorized"] += 1
                self.adapter.ack(device_id=device_id, event_id=event_id)
                stats["acked"] += 1
                continue
            if str(ev.payload.get("privacy_class") or "") == "private":
                stats["dropped_private"] += 1
                self.adapter.ack(device_id=device_id, event_id=event_id)
                stats["acked"] += 1
                continue
            summary = _sanitize_summary(ev.payload.get("summary"))
            if not self._should_notify(summary, str(ev.kind)):
                stats["dropped_low_value"] += 1
                self.adapter.ack(device_id=device_id, event_id=event_id)
                stats["acked"] += 1
                continue
            outcome = self._deliver(device_id, event_id, summary)
            stats[outcome] += 1
            # 到达终态（notified/suppressed/failed/unknown）即 ack——
            # 幂等键兜底保证重投也不会重复通知。
            self.adapter.ack(device_id=device_id, event_id=event_id)
            stats["acked"] += 1
        return stats

    def _deliver(self, device_id: str, event_id: str, summary: str) -> str:
        """草稿 → Outbox 裁决 → 网关幂等下发 → 终态回执。返回结果计数键。"""
        state_key = f"{_NOTIFY_STATE_PREFIX}{device_id}:{event_id}"
        prior = self.store.get(state_key)
        if isinstance(prior, dict) and prior.get("status") == "executed":
            return "notified"  # 已通知过（幂等键之外的第二道防线）
        draft_id = f"device-notify:{device_id}:{event_id}"
        if self.outbox is None or self.gateway is None:
            self.store.put(state_key, {"status": "failed",
                                       "reason": "no gateway/outbox wired"})
            return "failed"
        self.outbox.propose(ProactiveDraft(
            draft_id=draft_id, text=f"注意到：{summary}",
            reason=f"设备事件触发提醒（{event_id[:12]}）",
            evidence=(f"device:event:{device_id}:{event_id}",),
            channel="phone_notice"))
        verdict = self.outbox.dispatch(
            draft_id, self._prefs(), now=self._now(), port=_NullPort())
        if verdict.status != "ready" and verdict.status != "accepted":
            # 策略抑制（静默/配额/暂停）：不是错误，可观测即可
            self.store.put(state_key, {"status": "suppressed",
                                       "reason": verdict.status})
            return "suppressed_by_policy"
        try:
            receipt = self.gateway.send_action(
                device_id, capability=_NOTIFY_CAPABILITY,
                args={"text": f"注意到：{summary}"},
                idempotency_key=f"notify:{device_id}:{event_id}")
        except Exception as exc:
            # 外部副作用不可知：标 unknown，不盲目重试
            self.store.put(state_key, {"status": "unknown",
                                       "reason": type(exc).__name__})
            return "unknown"
        status = str(receipt.get("status") or "unknown")
        self.store.put(state_key, {"status": status,
                                   "receipt": receipt.get("receipt_id", "")})
        return "notified" if status == "executed" else (
            "failed" if status == "failed" else "unknown")

    def _prefs(self):
        """宿主注入的 Initiative 策略；缺省为保守默认（phone_notice 允许、
        静默窗 22–07 生效——设备事件通知属主动行为，深夜不打扰）。"""
        if self._injected_prefs is not None:
            return self._injected_prefs
        from whale_core.life.initiative import ProactivePrefs
        return ProactivePrefs(enabled=True,
                              allowed_channels=frozenset({"phone_notice"}))


class _NullPort:
    """Outbox 派发占位口：真实外发由 gateway.send_action 承担，
    Outbox 在此只作策略裁决/记账（accepted=渠道占位接受）。"""

    def send(self, draft) -> bool:  # noqa: ARG002 - 端口协议
        return True
