"""EmbeddedWssTransport（DeviceTransport 实现）打真实 WSS 链路 + 编排器集成。

覆盖：execute 成功/超时/离线三态（超时→TIMEOUT 回执不抛；离线→FAILED/
unavailable）、observe（action.request device.observe → output_ref 拉取组装
Observation）、read_output（sensor.query device.output.read 分页）、
capabilities（hello 声明 ∩ registry scopes）、health、DeviceGatewayV2
注册选路与幂等。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

websockets = pytest.importorskip("websockets")
cryptography = pytest.importorskip("cryptography")

from websockets.sync.client import connect  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from whale_core.devices.embedded_wss import WS_PATH, EmbeddedWssGateway  # noqa: E402
from whale_core.devices.gateway_v2 import DeviceGatewayV2  # noqa: E402
from whale_core.devices.registry import DeviceRegistry  # noqa: E402
from whale_core.devices.transports.base import (  # noqa: E402
    ActionRequest,
    ObserveRequest,
    ReceiptStatus,
    new_id,
    new_ulid,
    now_ms,
)
from whale_core.devices.transports.embedded_wss import EmbeddedWssTransport  # noqa: E402
from whale_core.memory.store import InMemoryStore  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 设备模拟器

class DeviceSim:
    """v2 设备模拟：握手 + 帧收发 + 脚本化应答循环。"""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.pubkey_hex = self.key.public_key().public_bytes_raw().hex()
        self.device_id: str | None = None
        self.seq = 0
        self.ws = None
        self.frames: list[dict] = []
        self._stop = threading.Event()

    def pair(self, registry: DeviceRegistry, scopes=None) -> None:
        result = registry.request_pairing(
            label="sim", pubkey_hex=self.pubkey_hex, scopes=scopes)
        self.device_id = result["device_id"]
        registry.approve(result["code"])

    def frame(self, msg_type: str, payload: dict) -> dict:
        self.seq += 1
        return {"v": "2.0", "id": new_ulid(), "type": msg_type,
                "ts": now_ms(), "seq": self.seq, "dir": "uplink",
                "payload": payload}

    def connect(self, port: int,
                capabilities=("device.tap", "device.observe",
                              "device.output.read", "device.status")) -> None:
        self.ws = connect(f"ws://127.0.0.1:{port}{WS_PATH}", open_timeout=5)
        challenge = json.loads(self.ws.recv(timeout=5))["payload"]["challenge"]
        sig = self.key.sign(challenge.encode("ascii")).hex()
        self.ws.send(json.dumps(self.frame("device.hello", {
            "protocol_version": "2.0", "device_id": self.device_id,
            "capabilities": list(capabilities),
            "features": {"root_shell": True, "accessibility": True,
                         "media_projection": True, "telecom": False,
                         "ocr_mlkit": True},
            "device": {"model": "m", "android_release": "15",
                       "api_level": 35},
            "app_version": "t", "app_flavor": "root",
            "signature": sig, "challenge": challenge})))
        ack = json.loads(self.ws.recv(timeout=5))
        assert ack["type"] == "device.hello_ack", ack

    def serve(self, handler) -> threading.Thread:
        """后台读循环：每个下行帧交给 handler(sim, frame)（可回复 None）。"""
        def loop():
            while not self._stop.is_set():
                try:
                    msg = json.loads(self.ws.recv(timeout=0.5))
                except TimeoutError:
                    continue
                except Exception:
                    return
                self.frames.append(msg)
                reply = handler(self, msg)
                if reply is not None:
                    self.ws.send(json.dumps(reply))
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def close(self):
        self._stop.set()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


@pytest.fixture()
def rig():
    store = InMemoryStore()
    registry = DeviceRegistry(store)
    gateway = EmbeddedWssGateway(registry, store, host="127.0.0.1", port=0)
    transport = EmbeddedWssTransport(gateway)
    sim = DeviceSim()
    sim.pair(registry)
    yield registry, gateway, transport, sim
    sim.close()
    gateway.close()


# ---------------------------------------------------------------- execute

def test_execute_succeeded_maps_receipt(rig):
    _, gateway, transport, sim = rig
    sim.connect(gateway.port)

    def handler(sim, msg):
        if msg["type"] == "action.request":
            p = msg["payload"]
            assert p["capability"] == "device.tap"
            assert p["arguments"] == {"x": 100, "y": 200}
            assert p["task_id"].startswith("task_")
            return sim.frame("action.receipt", {
                "action_id": p["action_id"], "status": "succeeded",
                "task_id": p.get("task_id"),
                "started_at": now_ms(), "completed_at": now_ms(),
                "result": {"summary": "已点击",
                           "verification": {"status": "passed",
                                            "summary": "页面已切换"}}})
        return None

    sim.serve(handler)
    req = ActionRequest(
        action_id=new_id("act"), capability="device.tap",
        arguments={"x": 100, "y": 200}, idempotency_key="t:tap:0001",
        timeout_ms=5000, task_id=new_id("task"))
    receipt = run(transport.execute(sim.device_id, req))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    assert receipt.summary == "已点击"
    assert receipt.verification is not None
    assert receipt.verification.status.value == "passed"
    assert receipt.completed_at is not None


def test_execute_offline_is_failed_unavailable_not_raised(rig):
    """设备不在线 → FAILED/unavailable 回执（不抛、不悬挂 received）。"""
    _, gateway, transport, sim = rig
    req = ActionRequest(
        action_id=new_id("act"), capability="device.tap",
        arguments={}, idempotency_key="t:offline-1", timeout_ms=1000)
    receipt = run(transport.execute(sim.device_id, req))
    assert receipt.status is ReceiptStatus.FAILED
    assert receipt.error_code == "unavailable"


def test_execute_timeout_returns_timeout_terminal(rig):
    """等待回执超时 → TIMEOUT 终态回执（§7.2：不抛异常，副作用可能已发生）。"""
    _, gateway, transport, sim = rig
    sim.connect(gateway.port)
    sim.serve(lambda sim, msg: None)  # 收到也不回
    req = ActionRequest(
        action_id=new_id("act"), capability="device.tap",
        arguments={}, idempotency_key="t:timeout-1", timeout_ms=100)
    receipt = run(transport.execute(sim.device_id, req))
    assert receipt.status is ReceiptStatus.TIMEOUT
    assert receipt.error_code == "timeout"
    # 幂等记录已占位（不提前释放；迟到回执可对账）
    record = gateway.store.get(f"device:action:{sim.device_id}:t:timeout-1")
    assert record is not None


def test_execute_idempotent_replay_returns_recorded(rig):
    """§7.1：同幂等键第二次执行回已记录终态，设备侧不重收帧。"""
    _, gateway, transport, sim = rig
    sim.connect(gateway.port)

    def handler(sim, msg):
        if msg["type"] == "action.request":
            return sim.frame("action.receipt", {
                "action_id": msg["payload"]["action_id"],
                "status": "succeeded", "completed_at": now_ms(),
                "result": {"summary": "ok"}})
        return None

    sim.serve(handler)
    req = ActionRequest(
        action_id=new_id("act"), capability="device.tap",
        arguments={}, idempotency_key="t:dupe:0001", timeout_ms=5000)
    first = run(transport.execute(sim.device_id, req))
    assert first.status is ReceiptStatus.SUCCEEDED
    n_frames = len([f for f in sim.frames if f["type"] == "action.request"])
    # 重放：新 action_id + 同幂等键 → 已记录终态，不重发
    req2 = ActionRequest(
        action_id=new_id("act"), capability="device.tap",
        arguments={}, idempotency_key="t:dupe:0001", timeout_ms=5000)
    second = run(transport.execute(sim.device_id, req2))
    assert second.status is ReceiptStatus.SUCCEEDED
    assert second.action_id == first.action_id
    assert second.provider_ref == "idempotent:replay-suppressed"
    assert len([f for f in sim.frames
                if f["type"] == "action.request"]) == n_frames


# ---------------------------------------------------------------- observe / read_output

_OBS_PAYLOAD = {
    "observation_id": None,  # 由 transport 缺省生成
    "foreground_package": "com.example.app",
    "activity": ".MainActivity",
    "screen_width": 1080, "screen_height": 2400,
    "elements": [
        {"element_id": 1, "class_name": "android.widget.Button",
         "text": "搜索", "resource_id": "com.example:id/search",
         "content_desc": "搜索按钮", "bounds": [10, 20, 200, 80],
         "clickable": True, "fingerprint": "ab" * 8},
    ],
}


def test_observe_roundtrip_via_output_ref(rig):
    """§8：观察本体经 output_ref 拉取，不占回执帧预算。"""
    _, gateway, transport, sim = rig
    sim.connect(gateway.port)

    def handler(sim, msg):
        p = msg["payload"]
        if msg["type"] == "action.request" and p["capability"] == "device.observe":
            return sim.frame("action.receipt", {
                "action_id": p["action_id"], "status": "succeeded",
                "completed_at": now_ms(),
                "result": {"summary": "observed",
                           "output_ref": "agent:ui/latest"}})
        if msg["type"] == "sensor.query":
            assert p["operation"] == "device.output.read"
            assert p["filters"]["output_ref"] == "agent:ui/latest"
            return sim.frame("sensor.result", {
                "request_id": p["request_id"], "status": "ok",
                "data": [{"text": json.dumps(_OBS_PAYLOAD)}],
                "truncated": False, "source": "capture.local"})
        return None

    sim.serve(handler)
    obs = run(transport.observe(sim.device_id, ObserveRequest()))
    assert obs.observation_id.startswith("obs_")
    assert obs.foreground_package == "com.example.app"
    assert obs.screen_width == 1080 and obs.screen_height == 2400
    assert len(obs.elements) == 1
    el = obs.elements[0]
    assert el.text == "搜索" and el.clickable and el.center == (105, 50)
    assert obs.screenshot_ref == "agent:ui/latest"
    assert obs.ui_signature  # sha256 签名存在


def test_observe_failed_receipt_raises_gateway_error(rig):
    from whale_core.devices.embedded_wss import GatewayError
    _, gateway, transport, sim = rig
    sim.connect(gateway.port)
    sim.serve(lambda sim, msg: sim.frame("action.receipt", {
        "action_id": msg["payload"]["action_id"], "status": "failed",
        "error_code": "permission_missing"})
        if msg["type"] == "action.request" else None)
    with pytest.raises(GatewayError) as excinfo:
        run(transport.observe(sim.device_id, ObserveRequest()))
    assert excinfo.value.code == "permission_missing"


def test_read_output_text_and_truncated(rig):
    _, gateway, transport, sim = rig
    sim.connect(gateway.port)
    sim.serve(lambda sim, msg: sim.frame("sensor.result", {
        "request_id": msg["payload"]["request_id"], "status": "ok",
        "data": [{"text": "chunk-bytes"}], "truncated": True,
        "source": "capture.local"})
        if msg["type"] == "sensor.query" else None)
    chunk, truncated = run(transport.read_output(
        sim.device_id, "agent:out/act_x", offset=0, max_bytes=4096))
    assert chunk == b"chunk-bytes"
    assert truncated is True


# ---------------------------------------------------------------- capabilities / health

def test_capabilities_intersection_hello_and_scopes(rig):
    """capabilities = hello 声明 ∩ registry scopes（在线视图）。"""
    registry, gateway, transport, sim = rig
    registry.set_scopes(sim.device_id, ["device.tap", "device.observe"])
    sim.connect(gateway.port)  # hello 声明含 device.status/device.output.read
    caps = set(run(transport.capabilities(sim.device_id)))
    assert caps == {"device.tap", "device.observe"}  # scopes 收窄生效


def test_health_online_offline(rig):
    _, gateway, transport, sim = rig
    h = run(transport.health(sim.device_id))
    assert h.connected is False and h.detail == "not_connected"
    sim.connect(gateway.port)
    h = run(transport.health(sim.device_id))
    assert h.connected is True


# ---------------------------------------------------------------- 编排器集成

def test_gateway_v2_routes_to_embedded_and_records_receipt(rig):
    """DeviceGatewayV2 注册 embedded_wss：选路 + 幂等表（§7.1 编排层）。"""
    registry, gateway, transport, sim = rig
    sim.connect(gateway.port)
    sim.serve(lambda sim, msg: sim.frame("action.receipt", {
        "action_id": msg["payload"]["action_id"], "status": "succeeded",
        "completed_at": now_ms(), "result": {"summary": "ok"}})
        if msg["type"] == "action.request" else None)
    orchestrator = DeviceGatewayV2(registry)
    orchestrator.register_transport(transport)
    receipt = run(orchestrator.execute(
        sim.device_id, "device.tap", {"x": 1, "y": 2},
        idempotency_key="orch:0001", timeout_ms=5000))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    # 编排层幂等：同键直接回已记录终态
    again = run(orchestrator.execute(
        sim.device_id, "device.tap", {"x": 1, "y": 2},
        idempotency_key="orch:0001", timeout_ms=5000))
    assert again is receipt
    # 健康报告：embedded_wss connected
    report = run(orchestrator.health_report(sim.device_id))
    assert report == {"embedded_wss": "connected"}
