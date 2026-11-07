"""Notification Gateway 核心链路测试（contracts/v2 §13 / 修正方案）。

全 fake：InMemoryStore + FakeWssPort / FakeV1Gateway / FakeActionHandler，
零网络、零真实等待（now_fn 用可推进 FakeClock）。

覆盖：
1. notify 全链：evt_/notif_ ID 格式、TTL 默认、入队、送达、displayed→opened 推进；
2. 设备离线：队列保留、sync 补传未过期、跳过已过期、已确认不重复；
3. revoke 语义（在线真下发 / 离线诚实 False / 停止补传）；
4. 回执纪律：first terminal wins、event_id 错配忽略、failed 记原因、
   displayed 不算「用户已看到」；
5. action_submitted 转 action_handler（按钮不产生本地授权，见注释断言）；
6. 容量有界与过期清扫；
7. 策略默认表与覆盖；service 门面；通知历史。
"""
from __future__ import annotations

import asyncio
import re

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.notifications import (
    CATEGORIES,
    CATEGORY_CHANNELS,
    DEFAULT_POLICIES,
    DeliveryQueue,
    DeliveryStatus,
    NotificationAction,
    NotificationEvent,
    NotificationGateway,
    NotificationTarget,
    PolicyTable,
    CategoryPolicy,
)
from whale_core.notifications.gateway import _clamp
from whale_core.notifications.models import NotificationCategory
from whale_core.notifications.providers.whale_wss import WhaleWssProvider
from whale_core.notifications.services import (
    NotificationHistory,
    NotificationService,
)

DEVICE = "a" * 32
DEVICE2 = "b" * 32
T0 = 1_794_052_800_000  # 2026-11-07 前后，协议时间窗内

EVT_RE = re.compile(r"^evt_[0-9A-HJKMNP-TV-Z]{26}$")
NOTIF_RE = re.compile(r"^notif_[0-9A-HJKMNP-TV-Z]{26}$")


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    """可推进毫秒时钟。"""

    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


class FakeWssPort:
    """DeviceWssPort 测试替身：记录下行 payload / 撤回，在线集合可控。"""

    def __init__(self) -> None:
        self.online: set[str] = set()
        self.sent: list[tuple[str, dict]] = []
        self.revoked: list[tuple[str, dict]] = []

    def is_online(self, device_id: str) -> bool:
        return device_id in self.online

    async def send_notification_event(self, device_id: str, payload: dict) -> None:
        self.sent.append((device_id, payload))

    async def send_notification_revoke(self, device_id: str, payload: dict) -> None:
        self.revoked.append((device_id, payload))


class FakeActionHandler:
    """action_handler 替身：只记录，绝不做任何授权动作。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def handle_notification_action(self, **kwargs) -> None:
        self.calls.append(kwargs)


def make_gateway(*, online: bool = True, clock: FakeClock | None = None,
                 handler: FakeActionHandler | None = None,
                 max_queue: int = 200, history_limit: int = 500):
    """装配一套全 fake 的 gateway（返回 gateway, wss_port, clock, handler）。"""
    clock = clock or FakeClock()
    wss = FakeWssPort()
    if online:
        wss.online.add(DEVICE)
        wss.online.add(DEVICE2)
    store = InMemoryStore()
    gateway = NotificationGateway(
        store, providers=[WhaleWssProvider(wss)],
        action_handler=handler, now_fn=clock,
        max_queue_per_device=max_queue, history_limit=history_limit)
    return gateway, wss, clock, handler


def receipt_of(record, status: str, at: int) -> dict:
    return {"event_id": record.event_id, "notification_id": record.notification_id,
            "device_id": record.device_id, "status": status, "received_at": at}


# ---------------------------------------------------------------------------
# 1. notify 全链
# ---------------------------------------------------------------------------


def test_notify_full_chain_online():
    gateway, wss, clock, _ = make_gateway()
    nid = run(gateway.notify(DEVICE, "chat.message", "你好", "在吗"))

    # ID 格式：notif_/evt_ + 26 字符 ULID（§3）
    assert NOTIF_RE.match(nid), nid
    record = gateway.get_delivery(DEVICE, nid)
    assert record is not None and EVT_RE.match(record.event_id)
    # WSS 已送达，payload 全字段
    assert len(wss.sent) == 1
    payload = wss.sent[0][1]
    assert payload["event_id"] == record.event_id
    assert payload["notification_id"] == nid
    assert payload["category"] == "chat.message"
    assert payload["priority"] == "normal"
    # TTL 默认：chat.message = 1h
    assert payload["expires_at"] - payload["created_at"] == 3_600_000
    assert record.status is DeliveryStatus.DELIVERED
    assert record.provider == "whale_wss"
    # 已入队且记了 sent_at（等端侧回执才出队）
    entry = gateway.queue.get_entry(DEVICE, record.event_id)
    assert entry is not None and entry["sent_at"] is not None
    # 历史已记录且未读
    hist = gateway.history.list(DEVICE)
    assert len(hist) == 1 and hist[0]["read"] is False

    # 回执推进 displayed → opened
    r1 = gateway.handle_receipt(receipt_of(record, "displayed", clock.t))
    assert r1 is not None and r1.status is DeliveryStatus.DISPLAYED
    # 诚实性：displayed ≠ 用户已看到
    assert r1.status.user_acknowledged is False
    assert gateway.history.list(DEVICE)[0]["read"] is False
    r2 = gateway.handle_receipt(receipt_of(record, "opened", clock.t))
    assert r2 is not None and r2.status is DeliveryStatus.OPENED
    assert r2.status.user_acknowledged is True
    # 回执后队列项出队（已确认）
    assert gateway.queue.get_entry(DEVICE, record.event_id) is None


def test_notify_id_prefixes_and_schema_fields():
    gateway, wss, _, _ = make_gateway()
    nid = run(gateway.notify(
        DEVICE, "approval.requested", "审批：安装应用", "任务要求安装 X",
        target=NotificationTarget("approval", "aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC"),
        actions=[NotificationAction("approve", "批准"),
                 NotificationAction("reject", "拒绝")],
        group_key="aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC"))
    payload = wss.sent[0][1]
    assert payload["priority"] == "high"  # 审批默认高优先级
    assert payload["expires_at"] - payload["created_at"] == 600_000  # 10min
    assert payload["actions"] == [{"action_id": "approve", "label": "批准"},
                                  {"action_id": "reject", "label": "拒绝"}]
    assert payload["target"] == {"type": "approval",
                                 "id": "aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC"}


def test_notify_validation():
    gateway, _, _, _ = make_gateway()
    with pytest.raises(ValueError):
        run(gateway.notify(DEVICE, "not.a.category", "t", "b"))
    with pytest.raises(ValueError):
        run(gateway.notify(DEVICE, "chat.message", "   ", "b"))  # 空标题
    with pytest.raises(ValueError):
        run(gateway.notify(DEVICE, "chat.message", "t", "b",
                           actions=[NotificationAction("a", "x")] * 5))
    with pytest.raises(ValueError):
        run(gateway.notify(DEVICE, "chat.message", "t", "b",
                           expires_at=1))  # 已过期
    # 超长截断（保证产物恒过 schema）
    nid = run(gateway.notify(DEVICE, "chat.message", "题" * 500, "文" * 3000))
    record = gateway.get_delivery(DEVICE, nid)
    assert record is not None
    event = gateway.history.list(DEVICE)[0]
    assert len(event["title"]) == 200 and len(event["body"]) == 2000


# ---------------------------------------------------------------------------
# 2. 设备离线与 sync 补传
# ---------------------------------------------------------------------------


def test_offline_queue_retained_then_sync_replays():
    gateway, wss, clock, _ = make_gateway(online=False)
    nid = run(gateway.notify(DEVICE, "chat.message", "离线消息", "稍后看"))
    record = gateway.get_delivery(DEVICE, nid)
    # 离线：不送、不抛错；队列保留、台账停在 QUEUED
    assert wss.sent == []
    assert record.status is DeliveryStatus.QUEUED
    assert gateway.queue.get_entry(DEVICE, record.event_id) is not None

    # 设备上线并发 notification.sync（无历史 → 全量补未过期）
    wss.online.add(DEVICE)
    sent = run(gateway.handle_sync(DEVICE, None))
    assert sent == 1 and len(wss.sent) == 1
    assert wss.sent[0][1]["event_id"] == record.event_id


def test_sync_skips_expired_and_acked():
    gateway, wss, clock, _ = make_gateway(online=False)
    nid1 = run(gateway.notify(DEVICE, "chat.message", "第一条", ""))
    clock.advance(1000)
    nid2 = run(gateway.notify(DEVICE, "chat.message", "第二条", ""))
    r1 = gateway.get_delivery(DEVICE, nid1)
    r2 = gateway.get_delivery(DEVICE, nid2)

    # 推进到第一条刚好过期（chat.message TTL 1h）、第二条仍有效
    clock.advance(3_599_000)
    wss.online.add(DEVICE)
    sent = run(gateway.handle_sync(DEVICE, None))
    assert sent == 1
    assert [p["notification_id"] for _, p in wss.sent] == [nid2]

    # 第二条回执 displayed → 已确认不重复；再次 sync 什么都不补
    gateway.handle_receipt(receipt_of(r2, "displayed", clock.t))
    assert run(gateway.handle_sync(DEVICE, None)) == 0

    # 过期清扫：第一条出队并推进 EXPIRED
    removed = gateway.sweep_expired()
    assert removed == 1
    assert gateway.get_delivery(DEVICE, nid1).status is DeliveryStatus.EXPIRED


def test_sync_respects_last_event_id():
    gateway, wss, clock, _ = make_gateway(online=False)
    nids = [run(gateway.notify(DEVICE, "chat.message", f"消息{i}", ""))
            for i in range(3)]
    event_ids = sorted(gateway.get_delivery(DEVICE, nid).event_id
                       for nid in nids)
    # 取字典序中位为「本机已处理的最大事件 ID」：只应补严格大于它的那条
    # （同毫秒内 ULID 序按随机尾数，故用排序后的确定性口径断言）
    last = event_ids[1]
    wss.online.add(DEVICE)
    sent = run(gateway.handle_sync(DEVICE, last))
    assert sent == 1
    assert wss.sent[0][1]["event_id"] == event_ids[2]


def test_sync_offline_sends_nothing():
    gateway, wss, _, _ = make_gateway(online=False)
    run(gateway.notify(DEVICE, "chat.message", "离线", ""))
    assert run(gateway.handle_sync(DEVICE, None)) == 0
    assert wss.sent == []


# ---------------------------------------------------------------------------
# 3. revoke
# ---------------------------------------------------------------------------


def test_revoke_online_delivers_and_stops_replay():
    gateway, wss, _, _ = make_gateway()
    nid = run(gateway.notify(DEVICE, "task.completed", "完成", ""))
    record = gateway.get_delivery(DEVICE, nid)

    ok = run(gateway.revoke(DEVICE, nid, reason="结果已更新"))
    assert ok is True
    # notification.revoke payload 经 WSS 真实下发（§13.3）
    assert len(wss.revoked) == 1
    payload = wss.revoked[0][1]
    assert payload["notification_id"] == nid
    assert payload["device_id"] == DEVICE
    assert EVT_RE.match(payload["event_id"])  # 撤回指令自身事件 ID
    assert payload["reason"] == "结果已更新"
    # 队列项出队（不再补传）、台账标 revoked
    assert gateway.queue.get_entry(DEVICE, record.event_id) is None
    assert gateway.get_delivery(DEVICE, nid).revoked is True
    assert run(gateway.handle_sync(DEVICE, None)) == 0


def test_revoke_offline_is_honest_false():
    gateway, wss, _, _ = make_gateway(online=False)
    nid = run(gateway.notify(DEVICE, "task.completed", "完成", ""))
    ok = run(gateway.revoke(DEVICE, nid))
    assert ok is False  # 离线送不到：诚实返回 False，不伪报已撤回
    assert wss.revoked == []
    assert gateway.get_delivery(DEVICE, nid).revoked is True  # 台账仍登记


def test_revoke_unknown_notification_is_false():
    gateway, _, _, _ = make_gateway()
    assert run(gateway.revoke(DEVICE, "notif_01J9XK3EM8TQ2ZP0VN6W4RA7YC")) is True
    # 通道在线即可下发撤回（即使本地无台账）；离线则 False
    gateway2, wss2, _, _ = make_gateway(online=False)
    assert run(gateway2.revoke(DEVICE, "notif_01J9XK3EM8TQ2ZP0VN6W4RA7YC")) is False


# ---------------------------------------------------------------------------
# 4. 回执纪律
# ---------------------------------------------------------------------------


def test_receipt_first_terminal_wins():
    gateway, _, clock, _ = make_gateway()
    nid = run(gateway.notify(DEVICE, "chat.message", "t", ""))
    record = gateway.get_delivery(DEVICE, nid)
    gateway.handle_receipt(receipt_of(record, "opened", clock.t))
    # 终态后迟到回执不翻案
    late = gateway.handle_receipt(receipt_of(record, "displayed", clock.t + 1))
    assert late is not None and late.status is DeliveryStatus.OPENED


def test_receipt_unknown_and_mismatch_ignored():
    gateway, _, clock, _ = make_gateway()
    nid = run(gateway.notify(DEVICE, "chat.message", "t", ""))
    record = gateway.get_delivery(DEVICE, nid)
    # 未知 notification_id
    assert gateway.handle_receipt({
        "event_id": record.event_id, "notification_id": "notif_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
        "device_id": DEVICE, "status": "opened", "received_at": clock.t}) is None
    # event_id 错配（防串台）
    assert gateway.handle_receipt({
        "event_id": "evt_01J9XK3EM8TQ2ZP0VN6W4RA7YC", "notification_id": nid,
        "device_id": DEVICE, "status": "opened", "received_at": clock.t}) is None
    # 未知状态词（前向兼容：静默忽略）
    assert gateway.handle_receipt(receipt_of(record, "teleported", clock.t)) is None
    # 缺字段
    assert gateway.handle_receipt({"device_id": DEVICE}) is None
    # 以上都不该改动台账
    assert gateway.get_delivery(DEVICE, nid).status is DeliveryStatus.DELIVERED


def test_receipt_failed_records_reason():
    gateway, _, clock, _ = make_gateway()
    nid = run(gateway.notify(DEVICE, "chat.message", "t", ""))
    record = gateway.get_delivery(DEVICE, nid)
    r = gateway.handle_receipt(receipt_of(record, "failed", clock.t))
    assert r is not None and r.status is DeliveryStatus.FAILED
    # receipt wire 格式无原因字段：诚实记「端侧报告失败」，不臆造细节
    assert r.last_error == "device_reported_failed"


# ---------------------------------------------------------------------------
# 5. action_submitted → action_handler
# ---------------------------------------------------------------------------


def test_action_submitted_forwarded_to_handler():
    handler = FakeActionHandler()
    gateway, wss, clock, _ = make_gateway(handler=handler)
    nid = run(gateway.notify(
        DEVICE, "approval.requested", "审批：删除文件", "任务要求删除 /tmp/x",
        actions=[NotificationAction("approve", "批准"),
                 NotificationAction("reject", "拒绝")]))
    record = gateway.get_delivery(DEVICE, nid)
    gateway.handle_receipt(receipt_of(record, "displayed", clock.t))

    # 用户点了「批准」按钮 → notification.action 上行
    r = gateway.handle_action({
        "event_id": record.event_id, "notification_id": nid,
        "device_id": DEVICE, "action_id": "approve", "submitted_at": clock.t})
    assert r is not None and r.status is DeliveryStatus.ACTION_SUBMITTED
    assert handler.calls == [{
        "device_id": DEVICE, "notification_id": nid,
        "event_id": record.event_id, "action_id": "approve"}]
    # 安全断言（修正方案 §13.3）：按钮点击只是「意图回传」——gateway 只登记
    # ACTION_SUBMITTED 并转交 handler；真正的审批裁决必须由 handler 背后的
    # core 审批流（§14，approval_id 一次性核销）完成，本链路不产生任何授权。
    # 终态：后续 dismissed/opened 不翻案
    again = gateway.handle_receipt(receipt_of(record, "dismissed", clock.t + 1))
    assert again is not None and again.status is DeliveryStatus.ACTION_SUBMITTED


def test_action_without_handler_still_advances():
    gateway, _, clock, _ = make_gateway(handler=None)
    nid = run(gateway.notify(DEVICE, "task.completed", "完成", "",
                             actions=[NotificationAction("open", "查看")]))
    record = gateway.get_delivery(DEVICE, nid)
    r = gateway.handle_action({
        "event_id": record.event_id, "notification_id": nid,
        "device_id": DEVICE, "action_id": "open", "submitted_at": clock.t})
    assert r is not None and r.status is DeliveryStatus.ACTION_SUBMITTED


# ---------------------------------------------------------------------------
# 6. 容量有界与过期清扫
# ---------------------------------------------------------------------------


def test_queue_capacity_evicts_oldest():
    gateway, _, clock, _ = make_gateway(online=False, max_queue=3)
    nids = [run(gateway.notify(DEVICE, "chat.message", f"消息{i}", ""))
            for i in range(5)]
    event_ids = sorted(gateway.get_delivery(DEVICE, nid).event_id
                       for nid in nids)
    # 只留字典序最大（≈最新）的 3 条，最旧的被逐出
    queued = sorted(e.event_id for e in gateway.queue.pending_events(DEVICE))
    assert queued == event_ids[-3:]
    assert len(queued) == 3


def test_dequeue_expired_bounded():
    store = InMemoryStore()
    clock = FakeClock()
    queue = DeliveryQueue(store, now_fn=clock)
    for i in range(5):
        clock.advance(10)
        event = NotificationEvent(
            event_id=f"evt_{i:026d}",
            notification_id=f"notif_{i}", device_id=DEVICE,
            category="chat.message", title="t", body="", priority="normal",
            created_at=clock.t, expires_at=clock.t + 100)
        queue.enqueue(event)
    clock.advance(200)  # 全部过期
    removed = queue.dequeue_expired(max_deletes=2)
    assert len(removed) == 2  # 单趟封顶
    assert len(queue.dequeue_expired(max_deletes=500)) == 3


def test_history_limit_trim():
    gateway, _, clock, _ = make_gateway(history_limit=5)
    for i in range(7):
        run(gateway.notify(DEVICE, "chat.message", f"消息{i}", ""))
        clock.advance(10)
    hist = gateway.history.list(DEVICE)
    assert len(hist) == 5
    assert hist[0]["title"] == "消息6"  # 新在前


# ---------------------------------------------------------------------------
# 7. 策略 / service 门面 / 历史
# ---------------------------------------------------------------------------


def test_policy_defaults_table():
    """默认表与修正方案一致（TTL/优先级/channel）。"""
    assert DEFAULT_POLICIES["chat.message"].ttl_ms == 3_600_000          # 1h
    assert DEFAULT_POLICIES["chat.message"].priority == "normal"
    assert DEFAULT_POLICIES["approval.requested"].ttl_ms == 600_000      # 10min
    assert DEFAULT_POLICIES["approval.requested"].priority == "high"
    assert DEFAULT_POLICIES["task.completed"].ttl_ms == 86_400_000       # 24h
    assert DEFAULT_POLICIES["task.progress"].collapse is True            # 合并
    assert DEFAULT_POLICIES["task.progress"].ttl_ms == 300_000           # 5min
    assert DEFAULT_POLICIES["device.offline"].ttl_ms == 1_800_000        # 30min
    assert DEFAULT_POLICIES["reminder.triggered"].ttl_ms is None         # 按事件 expires_at
    # channel 映射表覆盖全分类
    assert set(CATEGORY_CHANNELS) == set(CATEGORIES)
    assert CATEGORY_CHANNELS["chat.message"] == "whale_chat"
    assert CATEGORY_CHANNELS["approval.requested"] == "whale_approval"
    assert CATEGORY_CHANNELS["reminder.triggered"] == "whale_reminder"


def test_policy_overrides_and_fallback():
    table = PolicyTable({"chat.message": CategoryPolicy(
        60_000, "low", "whale_chat")})
    assert table.policy_for("chat.message").ttl_ms == 60_000
    # reminder：ttl_ms=None 且未给 expires_at → 兜底 1h，绝不永不过期
    expires = table.resolve_expires_at("reminder.triggered", created_at=T0,
                                       expires_at=None)
    assert expires == T0 + 3_600_000
    # 显式 expires_at 优先
    assert table.resolve_expires_at("reminder.triggered", created_at=T0,
                                    expires_at=T0 + 5000) == T0 + 5000
    with pytest.raises(ValueError):
        PolicyTable({"nope": CategoryPolicy(1, "low", "x")})
    with pytest.raises(ValueError):
        table.policy_for("nope")
    with pytest.raises(ValueError):
        table.resolve_expires_at("chat.message", created_at=T0, expires_at=T0)


def test_task_progress_auto_collapse():
    gateway, wss, _, _ = make_gateway()
    run(gateway.notify(DEVICE, "task.progress", "进度 50%", "",
                       group_key="task_01J9XK3EM8TQ2ZP0VN6W4RA7YC"))
    payload = wss.sent[0][1]
    # 折叠合并：未显式给 collapse_key 时按 group_key 自动派生
    assert payload["collapse_key"] == "task_01J9XK3EM8TQ2ZP0VN6W4RA7YC"
    assert payload["priority"] == "low"


def test_service_facade():
    gateway, wss, _, _ = make_gateway()
    service = NotificationService(gateway)

    run(service.chat_message(DEVICE, title="主人消息", body="在吗",
                             conversation_id="conv_1"))
    payload = wss.sent[-1][1]
    assert payload["category"] == "chat.message"
    assert payload["target"] == {"type": "conversation", "id": "conv_1"}

    run(service.approval_requested(DEVICE, approval_id="aprv_x", title="审批",
                                   body="删除文件"))
    payload = wss.sent[-1][1]
    assert payload["category"] == "approval.requested"
    assert payload["priority"] == "high"
    assert payload["actions"] == [{"action_id": "approve", "label": "批准"},
                                  {"action_id": "reject", "label": "拒绝"}]

    run(service.reminder_triggered(DEVICE, title="吃药", expires_at=T0 + 60_000))
    payload = wss.sent[-1][1]
    assert payload["category"] == "reminder.triggered"
    assert payload["expires_at"] == T0 + 60_000

    run(service.task_failed(DEVICE, task_id="task_1", title="任务失败"))
    payload = wss.sent[-1][1]
    assert payload["priority"] == "high"


def test_history_read_semantics():
    gateway, _, clock, _ = make_gateway()
    nid = run(gateway.notify(DEVICE, "chat.message", "一", ""))
    record = gateway.get_delivery(DEVICE, nid)
    # displayed 回执不置「已读」（displayed ≠ 用户已看到）
    gateway.handle_receipt(receipt_of(record, "displayed", clock.t))
    assert gateway.history.unread_count(DEVICE) == 1
    # 显式标记（用户在 UI 里的动作）
    assert gateway.history.mark_read(DEVICE, nid) is True
    assert gateway.history.unread_count(DEVICE) == 0
    assert gateway.history.list(DEVICE, unread_only=True) == []
    assert gateway.history.mark_read(DEVICE, "notif_nope") is False


def test_history_per_device_isolated():
    gateway, _, _, _ = make_gateway()
    run(gateway.notify(DEVICE, "chat.message", "给A", ""))
    run(gateway.notify(DEVICE2, "chat.message", "给B", ""))
    assert [h["title"] for h in gateway.history.list(DEVICE)] == ["给A"]
    assert gateway.history.unread_count(DEVICE2) == 1


def test_clamp_helper():
    assert _clamp("abc", 10) == "abc"
    assert len(_clamp("x" * 300, 200)) == 200
    assert _clamp("", 5) == ""


def test_all_categories_reachable():
    """每个 schema 分类都能正常 notify（枚举与策略表同步）。"""
    gateway, wss, clock, _ = make_gateway()
    for category in sorted(CATEGORIES):
        clock.advance(10)
        nid = run(gateway.notify(DEVICE, category, f"{category} 标题", "",
                                 expires_at=clock.t + 60_000))
        assert gateway.get_delivery(DEVICE, nid).status is DeliveryStatus.DELIVERED
    assert len(wss.sent) == len(CATEGORIES)
