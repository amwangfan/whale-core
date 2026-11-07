"""Notification providers 测试：whale_wss / external（v2 下线后无 legacy 桥）。

全 fake：零网络（ntfy 注入 post_fn 替身）、零真实等待（sleep_fn 注入）。

覆盖：
- whale_wss：离线返回保留哨兵不抛错；在线发 §13.1 payload；revoke 诚实语义；
  离线留队 → 重连 sync 补传（§13.3）；
- external：ntfy 默认关闭不发送（enabled=True 缺配置直接拒绝构造）；
  开启后发最小文本（title+body，结构化字段不出门）、Authorization 头、
  有界重试；Bark/PushDeer 占位抛 NotImplementedError。
"""
from __future__ import annotations

import asyncio

import pytest

from whale_core.devices.transports.base import new_id
from whale_core.memory.store import InMemoryStore
from whale_core.notifications import NotificationGateway
from whale_core.notifications.models import (
    DeliveryStatus,
    NotificationAction,
    NotificationEvent,
)
from whale_core.notifications.providers.base import NotificationProviderError
from whale_core.notifications.providers.external import (
    BarkProvider,
    NtfyProvider,
    PushDeerProvider,
)
from whale_core.notifications.providers.whale_wss import (
    OFFLINE_DELIVERY_ID,
    WhaleWssProvider,
)

DEVICE = "a" * 32
T0 = 1_794_052_800_000


def run(coro):
    return asyncio.run(coro)


def make_event(*, actions=(), body="正文") -> NotificationEvent:
    return NotificationEvent(
        event_id=new_id("evt"), notification_id=new_id("notif"),
        device_id=DEVICE, category="task.completed", title="标题", body=body,
        priority="normal", created_at=T0, expires_at=T0 + 3_600_000,
        actions=tuple(actions))


class FakeWssPort:
    def __init__(self, online: bool = True) -> None:
        self.online = online
        self.sent: list[tuple[str, dict]] = []
        self.revoked: list[tuple[str, dict]] = []

    def is_online(self, device_id: str) -> bool:
        return self.online

    async def send_notification_event(self, device_id: str, payload: dict) -> None:
        self.sent.append((device_id, payload))

    async def send_notification_revoke(self, device_id: str, payload: dict) -> None:
        self.revoked.append((device_id, payload))


# ---------------------------------------------------------------------------
# whale_wss
# ---------------------------------------------------------------------------


def test_wss_offline_returns_sentinel_without_raising():
    provider = WhaleWssProvider(FakeWssPort(online=False))
    delivery_id = run(provider.send(DEVICE, make_event()))
    # 离线不抛错：返回保留哨兵，由 router 决定队列保留/补传
    assert delivery_id == OFFLINE_DELIVERY_ID
    assert run(provider.get_status(delivery_id)) is None  # 哨兵不是真实送达


def test_wss_online_sends_event_payload():
    port = FakeWssPort(online=True)
    provider = WhaleWssProvider(port)
    event = make_event()
    delivery_id = run(provider.send(DEVICE, event))
    assert delivery_id == f"wss:{event.event_id}"
    assert port.sent == [(DEVICE, event.to_payload())]
    assert run(provider.get_status(delivery_id)) is DeliveryStatus.DELIVERED


def test_wss_revoke_semantics():
    port = FakeWssPort(online=True)
    provider = WhaleWssProvider(port)
    assert run(provider.revoke(DEVICE, "notif_x", reason="过期")) is True
    assert port.revoked[0][1]["notification_id"] == "notif_x"
    # 离线 → 诚实 False
    port.online = False
    assert run(provider.revoke(DEVICE, "notif_x")) is False

    # 连接层没有撤回口（早期 embedded_wss）→ 诚实 False，不伪报
    class NoRevokePort:
        def is_online(self, device_id): return True
        async def send_notification_event(self, device_id, payload): ...

    provider2 = WhaleWssProvider(NoRevokePort())
    assert run(provider2.revoke(DEVICE, "notif_x")) is False


def test_wss_send_failure_raises_provider_error():
    class BrokenPort(FakeWssPort):
        async def send_notification_event(self, device_id, payload):
            raise ConnectionError("ws closed")

    provider = WhaleWssProvider(BrokenPort(online=True))
    with pytest.raises(NotificationProviderError) as excinfo:
        run(provider.send(DEVICE, make_event()))
    assert excinfo.value.code == "unavailable"


def test_wss_offline_queue_then_sync_replay():
    """§13.3：离线留队（不丢不伪报），设备重连 notification.sync 后补传。"""
    wss = FakeWssPort(online=False)
    gateway = NotificationGateway(
        InMemoryStore(), providers=[WhaleWssProvider(wss)])
    nid = run(gateway.notify(DEVICE, "task.completed", "完成", ""))
    record = gateway.get_delivery(DEVICE, nid)
    # 离线：不计送达，队列保留
    assert record.status is not DeliveryStatus.DELIVERED
    assert gateway.queue.get_entry(DEVICE, record.event_id) is not None
    assert wss.sent == []
    # 重连后 sync 补传仍在有效期内的事件
    wss.online = True
    sent = run(gateway.handle_sync(DEVICE, None))
    assert sent == 1
    assert wss.sent[0][1]["notification_id"] == nid


# ---------------------------------------------------------------------------
# external（ntfy / Bark / PushDeer）
# ---------------------------------------------------------------------------


def test_ntfy_disabled_by_default_never_sends():
    posts = []
    provider = NtfyProvider(post_fn=lambda *a: posts.append(a) or (200, "ok"))
    assert provider.available is False
    with pytest.raises(NotificationProviderError) as excinfo:
        run(provider.send(DEVICE, make_event()))
    assert excinfo.value.code == "disabled"
    assert posts == []  # 默认关闭：一次都不发


def test_ntfy_enabled_requires_endpoint_and_token():
    with pytest.raises(ValueError):
        NtfyProvider(enabled=True, endpoint="", token="t")
    with pytest.raises(ValueError):
        NtfyProvider(enabled=True, endpoint="https://ntfy.example/t", token="")


def test_ntfy_enabled_sends_minimal_text():
    posts = []

    def fake_post(url, headers, body_text, timeout_s):
        posts.append({"url": url, "headers": headers, "body": body_text,
                      "timeout": timeout_s})
        return 200, "ok"

    provider = NtfyProvider(enabled=True, endpoint="https://ntfy.example/whale",
                            token="tk-secret", post_fn=fake_post)
    assert provider.available is True
    event = make_event(actions=[NotificationAction("open", "查看")])
    delivery_id = run(provider.send(DEVICE, event))
    assert delivery_id == f"ntfy:{event.event_id}"
    assert len(posts) == 1
    post = posts[0]
    assert post["url"] == "https://ntfy.example/whale"
    assert post["headers"]["Authorization"] == "Bearer tk-secret"
    # 最小文本：只有 title+body；actions/target 等结构化字段不出门
    assert post["body"] == "标题\n正文"
    assert "open" not in post["body"]


def test_ntfy_bounded_retry():
    attempts = []
    sleeps = []

    def flaky_post(url, headers, body_text, timeout_s):
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("boom")
        return 200, "ok"

    provider = NtfyProvider(enabled=True, endpoint="https://ntfy.example/whale",
                            token="t", max_attempts=3, post_fn=flaky_post,
                            sleep_fn=sleep_fn_append(sleeps))
    run(provider.send(DEVICE, make_event()))
    assert len(attempts) == 2  # 第一次失败重试，第二次成功
    assert sleeps == [0.5]     # 指数退避起步

    def always_fail(url, headers, body_text, timeout_s):
        raise ConnectionError("boom")

    provider2 = NtfyProvider(enabled=True, endpoint="https://x", token="t",
                             max_attempts=2, post_fn=always_fail,
                             sleep_fn=lambda _s: None)
    with pytest.raises(NotificationProviderError):
        run(provider2.send(DEVICE, make_event()))  # 重试耗尽抛错


def sleep_fn_append(sleeps):
    return lambda s: sleeps.append(s)


def test_external_placeholders_not_implemented():
    with pytest.raises(NotImplementedError):
        BarkProvider(enabled=True, endpoint="https://api.day.app/x")
    with pytest.raises(NotImplementedError):
        PushDeerProvider(enabled=True, sendkey="k")


def test_external_offline_fallback_via_gateway():
    """设备离线 + 用户显式开启 ntfy → 外部出口兜底；队列仍保留等 WSS 补传。"""
    wss = FakeWssPort(online=False)
    posts = []
    ntfy = NtfyProvider(enabled=True, endpoint="https://ntfy.example/whale",
                        token="t",
                        post_fn=lambda *a: posts.append(a) or (200, "ok"))
    gateway = NotificationGateway(
        InMemoryStore(), providers=[WhaleWssProvider(wss), ntfy])
    nid = run(gateway.notify(DEVICE, "task.failed", "任务失败", "看看"))
    assert len(posts) == 1
    record = gateway.get_delivery(DEVICE, nid)
    assert record.provider == "ntfy"
    # 队列保留（WSS 重连后 sync 仍会补传；端侧按 event_id 去重只展示一次）
    assert gateway.queue.get_entry(DEVICE, record.event_id) is not None
