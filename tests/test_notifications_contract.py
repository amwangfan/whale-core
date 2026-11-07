"""通知事件 schema 合规测试（contracts/v2 §19）。

把 NotificationGateway 真实生成的 notification.event payload 套上 §2 信封，
用 whale-mobile 仓 vendor 的 jsonschema（draft 2020-12）过
``messages/notification.event.schema.json`` 与 ``messages/envelope.schema.json``
（读本仓 contracts/v2/ 镜像——与 whale-mobile 同一事实源）。

vendor 目录缺失时整模块跳过（CI 无邻仓不阻塞；本地开发两仓并存必跑）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

# whale-mobile 的 tests/_vendor（jsonschema 及其依赖），与本仓平级
_WHALE_MOBILE_VENDOR = (
    Path(__file__).resolve().parents[2] / "whale-mobile" / "tests" / "_vendor")
if _WHALE_MOBILE_VENDOR.is_dir():
    sys.path.insert(0, str(_WHALE_MOBILE_VENDOR))

jsonschema = pytest.importorskip(
    "jsonschema",
    reason="需要 whale-mobile/tests/_vendor 的 jsonschema（邻仓缺失时跳过）")

from whale_core.devices.transports.base import new_ulid  # noqa: E402
from whale_core.memory.store import InMemoryStore  # noqa: E402
from whale_core.notifications import (  # noqa: E402
    NotificationAction,
    NotificationGateway,
    NotificationTarget,
)
from whale_core.notifications.providers.whale_wss import (  # noqa: E402
    WhaleWssProvider,
)

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "v2" / "messages"
DEVICE = "ba4bb14c75917cfc0e69fb7a1caf8ba3"  # 黄金示例同款 32-hex
T0 = 1_794_052_800_000


def load_schema(name: str) -> dict:
    with (CONTRACTS / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def envelope(msg_type: str, direction: str, payload: dict, seq: int = 1) -> dict:
    """§2 信封（WSS 连接层职责；这里按同一规则构造以校验 payload 可成帧）。"""
    return {"v": "2.0", "id": new_ulid(), "type": msg_type, "ts": T0,
            "seq": seq, "dir": direction, "payload": payload}


class CapturingWssPort:
    """DeviceWssPort 替身：捕获真实下发的 payload。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.revoked: list[dict] = []

    def is_online(self, device_id: str) -> bool:
        return True

    async def send_notification_event(self, device_id: str, payload: dict) -> None:
        self.sent.append(payload)

    async def send_notification_revoke(self, device_id: str, payload: dict) -> None:
        self.revoked.append(payload)


def make_gateway() -> tuple[NotificationGateway, CapturingWssPort]:
    port = CapturingWssPort()
    # 固定时钟到 T0：expires_at 基准是固定值，真实时间前进会把事件变成
    # 「已过期」导致定时炸弹（2026-11-08 跨午夜实测触发）。
    gateway = NotificationGateway(InMemoryStore(),
                                  providers=[WhaleWssProvider(port)],
                                  now_fn=lambda: T0)
    return gateway, port


def validate(name: str, message: dict) -> list:
    schema = load_schema(name)
    validator = jsonschema.Draft202012Validator(schema)
    return sorted(validator.iter_errors(message), key=lambda e: list(e.path))


def test_notification_event_full_fields_pass_schema():
    """全字段（target/actions/group/collapse）事件：消息 schema + 信封 schema。"""
    gateway, port = make_gateway()
    nid = asyncio.run(gateway.notify(
        DEVICE, "approval.requested", "审批：安装应用", "任务要求安装应用 X。",
        target=NotificationTarget("approval", "aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC"),
        actions=[NotificationAction("approve", "批准"),
                 NotificationAction("reject", "拒绝")],
        group_key="aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
        collapse_key="aprv_01J9XK3EM8TQ2ZP0VN6W4RA7YC",
        expires_at=T0 + 600_000))
    assert port.sent, "在线设备应收到 notification.event payload"
    message = envelope("notification.event", "downlink", port.sent[0])
    assert validate("notification.event.schema.json", message) == []
    assert validate("envelope.schema.json", message) == []


def test_notification_event_minimal_fields_pass_schema():
    """最小字段（无 target/actions/group/collapse）也应过 schema。"""
    gateway, port = make_gateway()
    asyncio.run(gateway.notify(DEVICE, "device.offline", "设备离线", "",
                               expires_at=T0 + 1_800_000))
    message = envelope("notification.event", "downlink", port.sent[0])
    assert validate("notification.event.schema.json", message) == []
    assert validate("envelope.schema.json", message) == []


def test_notification_revoke_passes_schema():
    """WSS provider 生成的 notification.revoke payload 过消息 schema。"""
    gateway, port = make_gateway()
    nid = asyncio.run(gateway.notify(DEVICE, "task.completed", "完成", "",
                                     expires_at=T0 + 86_400_000))
    assert asyncio.run(gateway.revoke(DEVICE, nid, reason="结果已更新")) is True
    assert port.revoked, "在线设备应收到 notification.revoke payload"
    message = envelope("notification.revoke", "downlink", port.revoked[0])
    assert validate("notification.revoke.schema.json", message) == []
    assert validate("envelope.schema.json", message) == []


def test_schema_negative_sanity():
    """负例 sanity：篡改 event_id 前缀后必须过不了 schema（证明校验在跑）。"""
    gateway, port = make_gateway()
    asyncio.run(gateway.notify(DEVICE, "chat.message", "你好", "",
                               expires_at=T0 + 3_600_000))
    message = envelope("notification.event", "downlink",
                       copy.deepcopy(port.sent[0]))
    message["payload"]["event_id"] = "BAD_01J9XK3EM8TQ2ZP0VN6W4RA7YC"
    errors = validate("notification.event.schema.json", message)
    assert errors, "非法 event_id 必须被 schema 拒绝"
    # 错误 dir 也必须被拒
    message2 = envelope("notification.event", "uplink",
                        copy.deepcopy(port.sent[0]))
    assert validate("notification.event.schema.json", message2) != []
