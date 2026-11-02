"""D2 全流程：websockets 客户端模拟设备，打真实 DeviceGateway + 管理 API。

覆盖：配对 → challenge → hello 验签 → 心跳/离线 → event+ack+去重 →
query/result 往返 → action/receipt 状态机 → 伪造签名拒 → 未知 major 拒 →
schema 违例 invalid_message → 吊销踢线。全部回环（127.0.0.1 + 临时端口）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.client import HTTPConnection

import pytest

websockets = pytest.importorskip("websockets")
cryptography = pytest.importorskip("cryptography")

from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.sync.client import connect  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from whale_core.api.server import ApiContext, WhaleApiServer  # noqa: E402
from whale_core.devices.gateway import WS_PATH, DeviceGateway  # noqa: E402
from whale_core.devices.perception import DevicePerceptionAdapter  # noqa: E402
from whale_core.devices.registry import DeviceRegistry  # noqa: E402
from whale_core.memory.store import InMemoryStore  # noqa: E402

TOKEN = "device-test-token"


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def rig():
    """registry + adapter + gateway（无 HTTP 层，配对直接走 registry）。"""
    store = InMemoryStore()
    registry = DeviceRegistry(store)
    adapter = DevicePerceptionAdapter()
    gateway = DeviceGateway(registry, store, host="127.0.0.1", port=0,
                            adapter=adapter, heartbeat_interval=1)
    yield registry, adapter, gateway
    gateway.close()


@pytest.fixture()
def api():
    """完整 HTTP API（设备服务于首个 /devices 请求懒装配）。"""
    ctx = ApiContext(store=InMemoryStore(), profile="demo")
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server
    server.stop()


def _req(server, method, path, *, token=TOKEN, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=15)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- 设备模拟器

class DeviceSim:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.pubkey_hex = self.key.public_key().public_bytes_raw().hex()
        self.device_id: str | None = None
        self.seq = 0
        self.ws = None

    # -- 管理面 --
    def pair(self, registry: DeviceRegistry, scopes=None) -> str:
        result = registry.request_pairing(
            label="sim", pubkey_hex=self.pubkey_hex, scopes=scopes)
        self.device_id = result["device_id"]
        return result["code"]

    # -- 协议帧 --
    def frame(self, msg_type: str, payload: dict, **override) -> dict:
        self.seq += 1
        msg = {"v": "1.0", "id": uuid.uuid4().hex, "type": msg_type,
               "ts": now_ms(), "seq": self.seq, "dir": "uplink",
               "payload": payload}
        msg.update(override)
        return msg

    def recv(self, timeout: float = 5.0) -> dict:
        return json.loads(self.ws.recv(timeout=timeout))

    def send(self, msg) -> None:
        self.ws.send(json.dumps(msg) if isinstance(msg, dict) else msg)

    # -- 握手 --
    def connect(self, port: int, *, expect_ok: bool = True,
                capabilities=("screen.context.read", "phone.notification.post"),
                sign: bool = True, challenge_override: str | None = None):
        self.ws = connect(f"ws://127.0.0.1:{port}{WS_PATH}", open_timeout=5)
        challenge_msg = self.recv()
        assert challenge_msg["type"] == "auth.challenge"
        challenge = challenge_msg["payload"]["challenge"]
        signature = self.key.sign(challenge.encode("ascii")).hex() if sign \
            else Ed25519PrivateKey.generate().sign(challenge.encode("ascii")).hex()
        hello = self.frame("device.hello", {
            "protocol_version": "1.0",
            "device_id": self.device_id or "0" * 32,
            "capabilities": list(capabilities),
            "app_version": "0.1.0-test",
            "signature": signature,
            "challenge": challenge_override or challenge,
        })
        self.send(hello)
        reply = self.recv()
        if expect_ok:
            assert reply["type"] == "device.hello_ack", reply
        return challenge_msg, reply

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


def _paired_active(registry: DeviceRegistry, scopes=None) -> DeviceSim:
    sim = DeviceSim()
    registry.approve(sim.pair(registry, scopes=scopes))
    return sim


# ---------------------------------------------------------------- 管理 API

def test_api_requires_bearer(api):
    status, body = _req(api, "POST", "/api/v1/devices/pair",
                        token="", body={"label": "x", "pubkey_hex": "0" * 64})
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_http_pair_approve_list_revoke(api):
    sim = DeviceSim()
    status, body = _req(api, "POST", "/api/v1/devices/pair",
                        body={"label": "pixel", "pubkey_hex": sim.pubkey_hex})
    assert status == 200, body
    result = body["result"]
    assert len(result["code"]) == 6
    sim.device_id = result["device_id"]

    status, body = _req(api, "POST", "/api/v1/devices/pair/approve",
                        body={"code": result["code"]})
    assert status == 200 and body["result"]["status"] == "active"

    status, body = _req(api, "GET", "/api/v1/devices")
    assert status == 200
    assert body["result"]["devices"][0]["device_id"] == sim.device_id
    assert "pairing_code" not in body["result"]["devices"][0]
    assert body["result"]["ws_port"]  # gateway 已懒装配

    status, body = _req(api, "GET", f"/api/v1/devices/{sim.device_id}")
    assert status == 200 and body["result"]["status"] == "active"

    status, body = _req(api, "DELETE", f"/api/v1/devices/{sim.device_id}")
    assert status == 200 and body["result"]["status"] == "revoked"


def test_http_approve_expired_code(api):
    # 经 registry 造一个立即过期的配对码，再走 HTTP 批准
    from whale_core.devices.registry import DeviceRegistry as _Reg
    sim = DeviceSim()
    reg: _Reg = api.ctx.device_registry or _Reg(api.ctx.store)
    result = reg.request_pairing(label="x", pubkey_hex=sim.pubkey_hex,
                                 code_ttl_s=0)
    status, body = _req(api, "POST", "/api/v1/devices/pair/approve",
                        body={"code": result["code"]})
    assert status == 410 and body["error"]["code"] == "expired"


# ---------------------------------------------------------------- 握手

def test_handshake_ok_and_capabilities_narrowed(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry, scopes=["screen.context.read"])
    try:
        _, ack = sim.connect(gateway.port)
        p = ack["payload"]
        assert p["session_id"] and p["heartbeat_interval"] == 1
        # scopes 收窄：phone.notification.post 不在设备上限内
        assert p["accepted_capabilities"] == ["screen.context.read"]
        assert gateway.is_online(sim.device_id)
    finally:
        sim.close()


def test_first_frame_must_be_hello(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    ws = connect(f"ws://127.0.0.1:{gateway.port}{WS_PATH}", open_timeout=5)
    try:
        challenge = json.loads(ws.recv(timeout=5))
        assert challenge["type"] == "auth.challenge"
        sim.ws = ws
        # 首帧发心跳而不是 hello → auth_failed 并关闭
        sim.send(sim.frame("device.heartbeat", {
            "device_id": sim.device_id, "observed_at": now_ms(),
            "status": "online"}))
        err = sim.recv()
        assert err["type"] == "error" and err["payload"]["code"] == "auth_failed"
        with pytest.raises(ConnectionClosed):
            sim.recv()
    finally:
        sim.close()


def test_bad_signature_rejected(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    _, err = sim.connect(gateway.port, expect_ok=False, sign=False)
    assert err["type"] == "error" and err["payload"]["code"] == "auth_failed"
    with pytest.raises(ConnectionClosed):
        sim.recv()
    sim.close()


def test_challenge_mismatch_rejected(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    # 用别的 challenge 签名（签名本身有效但 challenge 不匹配）
    sim.ws = connect(f"ws://127.0.0.1:{gateway.port}{WS_PATH}", open_timeout=5)
    challenge_msg = sim.recv()
    wrong = "ab" * 32
    sig = sim.key.sign(wrong.encode("ascii")).hex()
    sim.send(sim.frame("device.hello", {
        "protocol_version": "1.0", "device_id": sim.device_id,
        "capabilities": ["screen.context.read"], "app_version": "t",
        "signature": sig, "challenge": wrong}))
    err = sim.recv()
    assert err["payload"]["code"] == "auth_failed"
    sim.close()


def test_unknown_device_rejected(rig):
    _, _, gateway = rig
    sim = DeviceSim()  # 未配对
    _, err = sim.connect(gateway.port, expect_ok=False)
    assert err["payload"]["code"] == "auth_failed"
    sim.close()


def test_unapproved_device_rejected(rig):
    registry, _, gateway = rig
    sim = DeviceSim()
    sim.pair(registry)  # pending，未批准
    _, err = sim.connect(gateway.port, expect_ok=False)
    assert err["payload"]["code"] == "auth_failed"
    sim.close()


def test_revoked_device_hello_rejected(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    registry.revoke(sim.device_id)
    _, err = sim.connect(gateway.port, expect_ok=False)
    assert err["payload"]["code"] == "device_revoked"
    sim.close()


# ---------------------------------------------------------------- 帧纪律

def test_unknown_major_closed(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    try:
        sim.send(sim.frame("device.heartbeat", {
            "device_id": sim.device_id, "observed_at": now_ms(),
            "status": "online"}, v="2.0"))
        err = sim.recv()
        assert err["payload"]["code"] == "protocol_version"
        with pytest.raises(ConnectionClosed):
            sim.recv()
    finally:
        sim.close()


def test_schema_violation_invalid_message_keeps_connection(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    try:
        # sensor.event 缺 required 字段 → invalid_message，但连接保持
        sim.send(sim.frame("sensor.event", {"event_id": "x"}))
        err = sim.recv()
        assert err["payload"]["code"] == "invalid_message"
        # 连接仍可用：随后正常心跳被接受（无 error 返回，状态翻转）
        sim.send(sim.frame("device.heartbeat", {
            "device_id": sim.device_id, "observed_at": now_ms(),
            "status": "paused"}))
        time.sleep(0.3)
        st = gateway.online_state(sim.device_id)
        assert st is not None and st.status == "paused"
    finally:
        sim.close()


def test_invalid_json_frame_keeps_connection(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    try:
        sim.send("not-json{")
        err = sim.recv()
        assert err["payload"]["code"] == "invalid_message"
        sim.send(sim.frame("device.heartbeat", {
            "device_id": sim.device_id, "observed_at": now_ms(),
            "status": "online"}))
        time.sleep(0.3)
        assert gateway.is_online(sim.device_id)
    finally:
        sim.close()


def test_unknown_type_ignored(rig):
    registry, _, gateway = rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    try:
        sim.send(sim.frame("call.invite", {  # 有 schema 但无处理器 → 忽略
            "session_id": uuid.uuid4().hex, "media_params": {},
            "expires_at": now_ms() + 1000}))
        sim.send({"v": "1.0", "id": uuid.uuid4().hex, "type": "future.thing",
                  "ts": now_ms(), "seq": 99, "dir": "uplink", "payload": {}})
        # 无 error 返回；连接保持，后续心跳生效
        sim.send(sim.frame("device.heartbeat", {
            "device_id": sim.device_id, "observed_at": now_ms(),
            "status": "online"}))
        time.sleep(0.4)
        assert gateway.is_online(sim.device_id)
    finally:
        sim.close()


# ---------------------------------------------------------------- 心跳/离线

def test_heartbeat_offline_and_recovery():
    store = InMemoryStore()
    registry = DeviceRegistry(store)
    gateway = DeviceGateway(registry, store, host="127.0.0.1", port=0,
                            heartbeat_interval=1, heartbeat_timeout=1)
    sim = _paired_active(registry)
    try:
        sim.connect(gateway.port)
        assert gateway.is_online(sim.device_id)
        # 停发心跳 > 1s → sweeper 标记离线（连接保持）
        deadline = time.monotonic() + 5
        while gateway.is_online(sim.device_id):
            assert time.monotonic() < deadline, "应被标记离线"
            time.sleep(0.1)
        st = gateway.online_state(sim.device_id)
        assert st is not None and st.status == "offline"
        # 心跳恢复 → 重新在线
        sim.send(sim.frame("device.heartbeat", {
            "device_id": sim.device_id, "observed_at": now_ms(),
            "status": "online"}))
        deadline = time.monotonic() + 5
        while not gateway.is_online(sim.device_id):
            assert time.monotonic() < deadline, "应恢复在线"
            time.sleep(0.1)
    finally:
        sim.close()
        gateway.close()


# ---------------------------------------------------------------- 上行事件

def _event_payload(event_id: str, kind="screen.memo.created") -> dict:
    return {"event_id": event_id, "kind": kind, "occurred_at": now_ms(),
            "privacy_class": "owner_private", "summary": "一段脱敏摘要",
            "schema_version": "1.0"}


def test_sensor_event_ack_dedup_and_perception_adapter(rig):
    registry, adapter, gateway = rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    try:
        event_id = uuid.uuid4().hex
        sim.send(sim.frame("sensor.event", _event_payload(event_id)))
        ack = sim.recv()
        assert ack["type"] == "sensor.ack"
        assert ack["payload"]["event_ids"] == [event_id]
        # 进 perception 队列
        events = list(adapter.poll())
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "screen.memo.created"
        assert ev.payload["source"] == "screenmemo"
        assert ev.payload["device_id"] == sim.device_id
        assert ev.payload["privacy_class"] == "owner_private"
        # 去重命中：重发同 event_id → 仍回 ack（幂等闭环），但不再入队
        sim.send(sim.frame("sensor.event", _event_payload(event_id)))
        ack2 = sim.recv()
        assert ack2["payload"]["event_ids"] == [event_id]
        assert list(adapter.poll()) == []
        # 去重键持久化在 store kv
        assert gateway.store.get(
            f"device:event:{sim.device_id}:{event_id}") is not None
    finally:
        sim.close()


def test_adapter_is_perception_adapter(rig):
    from whale_core.perception.adapters import PerceptionAdapter
    _, adapter, _ = rig
    assert isinstance(adapter, PerceptionAdapter)


# ---------------------------------------------------------------- 下行 query

def _http_in_thread(server, method, path, body):
    box: dict = {}

    def run():
        box["result"] = _req(server, method, path, body=body)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return box, thread


def _api_rig(api):
    """经 HTTP 完成配对/批准，返回 (sim, gateway)。"""
    sim = DeviceSim()
    status, body = _req(api, "POST", "/api/v1/devices/pair",
                        body={"label": "sim", "pubkey_hex": sim.pubkey_hex})
    assert status == 200, body
    sim.device_id = body["result"]["device_id"]
    status, _ = _req(api, "POST", "/api/v1/devices/pair/approve",
                     body={"code": body["result"]["code"]})
    assert status == 200
    return sim, api.ctx.device_gateway


def test_query_result_roundtrip(api):
    sim, gateway = _api_rig(api)
    sim.connect(gateway.port)
    try:
        box, thread = _http_in_thread(
            api, "POST", f"/api/v1/devices/{sim.device_id}/query",
            {"operation": "screen.history.search",
             "filters": {"k": "v"}, "max_items": 5, "timeout": 5})
        query = sim.recv()
        assert query["type"] == "sensor.query"
        qp = query["payload"]
        assert qp["operation"] == "screen.history.search"
        assert qp["deadline"] > now_ms()
        sim.send(sim.frame("sensor.result", {
            "request_id": qp["request_id"], "status": "ok",
            "data": [{"title": "x"}], "truncated": False,
            "source": "screenmemo"}))
        thread.join(timeout=15)
        status, body = box["result"]
        assert status == 200
        assert body["result"]["result"]["status"] == "ok"
        assert body["result"]["result"]["data"] == [{"title": "x"}]
    finally:
        sim.close()


def test_query_offline_device(api):
    sim, gateway = _api_rig(api)
    status, body = _req(api, "POST", f"/api/v1/devices/{sim.device_id}/query",
                        body={"operation": "screen.context.read"})
    assert status == 409 and body["error"]["code"] == "unavailable"


def test_query_scope_denied(api):
    # 设备上限只有 screen.context.read；查 history.search → scope_denied
    sim = DeviceSim()
    status, body = _req(api, "POST", "/api/v1/devices/pair",
                        body={"label": "s", "pubkey_hex": sim.pubkey_hex,
                              "scopes": ["screen.context.read"]})
    sim.device_id = body["result"]["device_id"]
    _req(api, "POST", "/api/v1/devices/pair/approve",
         body={"code": body["result"]["code"]})
    sim.connect(api.ctx.device_gateway.port)
    try:
        status, body = _req(
            api, "POST", f"/api/v1/devices/{sim.device_id}/query",
            body={"operation": "screen.history.search"})
        assert status == 403 and body["error"]["code"] == "scope_denied"
    finally:
        sim.close()


# ---------------------------------------------------------------- 下行 action

def test_action_receipt_state_machine(api):
    sim, gateway = _api_rig(api)
    sim.connect(gateway.port)
    try:
        box, thread = _http_in_thread(
            api, "POST", f"/api/v1/devices/{sim.device_id}/action",
            {"capability": "phone.notification.post",
             "args": {"text": "hi"}, "idempotency_key": "idem-test-0001",
             "timeout": 5})
        req = sim.recv()
        assert req["type"] == "action.request"
        ap = req["payload"]
        assert ap["capability"] == "phone.notification.post"
        assert ap["idempotency_key"] == "idem-test-0001"
        action_id = ap["action_id"]
        # 中间态 received/accepted：不 resolve HTTP 等待方
        for state in ("received", "accepted"):
            sim.send(sim.frame("action.receipt",
                               {"action_id": action_id, "state": state}))
            time.sleep(0.2)
            assert thread.is_alive(), f"{state} 不应 resolve"
        # 终态 executed：resolve
        sim.send(sim.frame("action.receipt", {
            "action_id": action_id, "state": "executed",
            "executed_at": now_ms()}))
        thread.join(timeout=15)
        status, body = box["result"]
        assert status == 200
        assert body["result"]["receipt"]["state"] == "executed"
        # 回执状态机全程落库
        receipts = gateway.store.get(
            f"device:receipts:{sim.device_id}:{action_id}")
        assert [r["state"] for r in receipts] == ["received", "accepted", "executed"]
        # The owner can reconcile by idempotency key without dispatching again.
        status, body = _req(
            api, "POST", f"/api/v1/devices/{sim.device_id}/action/status",
            body={"idempotency_key": "idem-test-0001"})
        assert status == 200
        assert body["result"]["state"] == "executed"
        assert body["result"]["receipt"]["state"] == "executed"
    finally:
        sim.close()


def test_action_idempotency_duplicate(api):
    sim, gateway = _api_rig(api)
    sim.connect(gateway.port)
    try:
        box, thread = _http_in_thread(
            api, "POST", f"/api/v1/devices/{sim.device_id}/action",
            {"capability": "phone.notification.post",
             "idempotency_key": "idem-dupe-0001", "timeout": 5})
        req = sim.recv()
        sim.send(sim.frame("action.receipt", {
            "action_id": req["payload"]["action_id"], "state": "failed",
            "error_code": "permission_missing"}))
        thread.join(timeout=15)
        assert box["result"][0] == 200
        # 同幂等键再次下发 → duplicate，不再产生新 action.request
        status, body = _req(
            api, "POST", f"/api/v1/devices/{sim.device_id}/action",
            body={"capability": "phone.notification.post",
                  "idempotency_key": "idem-dupe-0001"})
        assert status == 409 and body["error"]["code"] == "duplicate"
    finally:
        sim.close()


# ---------------------------------------------------------------- 吊销踢线

def test_revoke_kicks_online_connection(api):
    sim, gateway = _api_rig(api)
    sim.connect(gateway.port)
    kicked = {"done": False}

    def watch():
        try:
            err = sim.recv(timeout=10)
            kicked["code"] = err["payload"]["code"]
            sim.recv(timeout=5)
        except ConnectionClosed:
            kicked["done"] = True

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    status, body = _req(api, "DELETE", f"/api/v1/devices/{sim.device_id}")
    assert status == 200 and body["result"]["kicked"] is True
    watcher.join(timeout=10)
    assert kicked.get("code") == "device_revoked" and kicked["done"]
    assert not gateway.is_online(sim.device_id)
    sim.close()


def test_revoke_rechecked_per_frame(rig):
    """不经 kick（直接改 registry），下一帧也应触发 device_revoked 关闭。"""
    registry, _, gateway = rig
    sim = _paired_active(registry)
    sim.connect(gateway.port)
    try:
        registry.revoke(sim.device_id)  # 模拟绕过 API 的直接吊销
        sim.send(sim.frame("device.heartbeat", {
            "device_id": sim.device_id, "observed_at": now_ms(),
            "status": "online"}))
        err = sim.recv()
        assert err["payload"]["code"] == "device_revoked"
        with pytest.raises(ConnectionClosed):
            sim.recv()
    finally:
        sim.close()


def test_late_action_receipt_can_be_reconciled_after_waiter_is_gone(api):
    sim, gateway = _api_rig(api)
    sim.connect(gateway.port)
    try:
        box, thread = _http_in_thread(
            api, "POST", f"/api/v1/devices/{sim.device_id}/action",
            {"capability": "phone.notification.post",
             "idempotency_key": "idem-late-0001", "timeout": 1})
        request = sim.recv()
        action_id = request["payload"]["action_id"]
        # Simulate the HTTP waiter disappearing (including after a restart).
        with gateway.lock:
            gateway.pending.pop(action_id, None)
        sim.send(sim.frame("action.receipt", {
            "action_id": action_id, "state": "executed",
            "executed_at": now_ms()}))
        status, result = _req(
            api, "POST", f"/api/v1/devices/{sim.device_id}/action/status",
            body={"idempotency_key": "idem-late-0001"})
        # WS and HTTP handlers run in separate threads; eventual persisted
        # outcome must be observable even with no waiting request.
        deadline = time.monotonic() + 2
        while status == 200 and result["result"]["state"] != "executed" and time.monotonic() < deadline:
            time.sleep(0.02)
            status, result = _req(
                api, "POST", f"/api/v1/devices/{sim.device_id}/action/status",
                body={"idempotency_key": "idem-late-0001"})
        assert status == 200
        assert result["result"]["state"] == "executed"
        assert result["result"]["action_id"] == action_id
        thread.join(timeout=10)
    finally:
        sim.close()
