"""PR#4 移植：whale-io PCM 帧协议 + PCMVoiceProvider + 票据中继的离线测试。

全部在 127.0.0.1 回环上以真实 socket / WebSocket 跑通，不触外网；
**协议按 PR#4 契约实现，未与真实 whale-io 桥联调**。
"""

from __future__ import annotations

import json
import socket
import socketserver
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from whale_core.plugins.pcm_voice import (
    MAX_FRAME,
    PCMVoiceProvider,
    pack_frame,
    read_frame,
    register_pcm_voice,
)


# ---------- 帧协议 ----------


def _socketpair():
    a, b = socket.socketpair()
    return a, b


def test_frame_roundtrip_audio_and_control():
    a, b = _socketpair()
    try:
        a.sendall(pack_frame(0, b"\x01\x00\xff\x7f"))
        a.sendall(pack_frame(1, b"HELLO box mode=echo sample=16000"))
        assert read_frame(b) == (0, b"\x01\x00\xff\x7f")
        assert read_frame(b) == (1, b"HELLO box mode=echo sample=16000")
    finally:
        a.close()
        b.close()


def test_frame_roundtrip_exact_boundary_size():
    a, b = _socketpair()
    try:
        payload = b"\x00" * (MAX_FRAME - 1)
        # 512KiB 超过回环缓冲：发送放后台线程，接收端边收边排水
        sender = threading.Thread(target=a.sendall, args=(pack_frame(0, payload),))
        sender.start()
        assert read_frame(b) == (0, payload)
        sender.join(timeout=5)
    finally:
        a.close()
        b.close()


def test_pack_frame_rejects_bad_kind_and_oversize():
    with pytest.raises(ValueError):
        pack_frame(2, b"x")
    with pytest.raises(ValueError):
        pack_frame(0, b"x" * MAX_FRAME)


def test_read_frame_rejects_bad_length_and_type():
    a, b = _socketpair()
    try:
        a.sendall(struct.pack("!I", 0))
        with pytest.raises(ValueError):
            read_frame(b)
    finally:
        a.close()
        b.close()
    a, b = _socketpair()
    try:
        a.sendall(struct.pack("!I", MAX_FRAME + 1))
        with pytest.raises(ValueError):
            read_frame(b)
    finally:
        a.close()
        b.close()
    a, b = _socketpair()
    try:
        a.sendall(struct.pack("!I", 2) + b"\x09x")  # 未知类型 9
        with pytest.raises(ValueError):
            read_frame(b)
    finally:
        a.close()
        b.close()


def test_read_frame_eof_on_disconnect():
    a, b = _socketpair()
    try:
        a.sendall(struct.pack("!I", 10) + b"\x00\x01")  # 声明 10 字节只给 2
        a.close()
        with pytest.raises(EOFError):
            read_frame(b)
    finally:
        b.close()


# ---------- PCMVoiceProvider 校验（照搬 PR#4） ----------


def test_provider_defaults_loopback_ok():
    provider = PCMVoiceProvider()
    assert provider.host == "127.0.0.1" and provider.port == 18799
    assert provider.healthcheck() is True


def test_provider_public_address_requires_tls():
    with pytest.raises(ValueError):
        PCMVoiceProvider(host="8.8.8.8")  # 公网 IP + 无 TLS → 拒绝
    provider = PCMVoiceProvider(host="8.8.8.8", tls=True)
    assert provider.tls is True


def test_provider_loopback_and_private_allowed_plain():
    for host in ("127.0.0.1", "localhost", "::1", "10.0.0.2", "192.168.1.2"):
        PCMVoiceProvider(host=host)


def test_provider_rejects_invalid_address_and_transport():
    for kwargs in (
        {"host": ""},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 65536},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "port": "18799"},
        {"host": "127.0.0.1", "timeout": 0},
        {"host": "127.0.0.1", "timeout": 121},
    ):
        with pytest.raises(ValueError):
            PCMVoiceProvider(**kwargs)


def test_provider_rejects_bad_bridge_token():
    with pytest.raises(ValueError):
        PCMVoiceProvider(bridge_token="a\nb")
    with pytest.raises(ValueError):
        PCMVoiceProvider(bridge_token="a\rb")
    with pytest.raises(ValueError):
        PCMVoiceProvider(bridge_token="x" * 501)
    PCMVoiceProvider(bridge_token="x" * 500)


def test_provider_public_ws_url_rules():
    PCMVoiceProvider(public_ws_url="wss://bridge.example.com/voice")
    PCMVoiceProvider(public_ws_url="ws://127.0.0.1:8081")
    PCMVoiceProvider(public_ws_url="ws://localhost:8081")
    for url in (
        "ws://bridge.example.com",              # 远程 ws（非回环）必须 WSS
        "http://127.0.0.1:8081",                # 非 ws/wss 方案
        "wss://user@bridge.example.com",        # 内嵌凭据
        "wss://user:pw@bridge.example.com",
        "wss://bridge.example.com/voice?x=1",   # query
        "wss://bridge.example.com/voice#frag",  # fragment
        "wss:///voice",                         # 无主机
    ):
        with pytest.raises(ValueError):
            PCMVoiceProvider(public_ws_url=url)


# ---------- AUTH 帧（回环 TCP 夹具） ----------


class _TCPFixture:
    """最小 PCM 桥夹具：记录收到的帧，可脚本化应答。"""

    def __init__(self, handler):
        self.received: list[tuple[int, bytes]] = []

        fixture = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                handler(fixture, self.request)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def test_connect_sends_auth_frame_with_token():
    def handler(fixture, sock):
        try:
            fixture.received.append(read_frame(sock))
        except (EOFError, OSError, ValueError):
            pass

    fixture = _TCPFixture(handler)
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, bridge_token="fixture-bridge-secret"
        )
        conn = provider.connect()
        conn.close()
        deadline = time.monotonic() + 3
        while not fixture.received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fixture.received == [(1, b"AUTH fixture-bridge-secret")]
    finally:
        fixture.close()


def test_connect_without_token_sends_no_auth():
    def handler(fixture, sock):
        sock.settimeout(0.5)
        try:
            fixture.received.append(read_frame(sock))
        except (EOFError, OSError, ValueError, socket.timeout):
            pass

    fixture = _TCPFixture(handler)
    try:
        provider = PCMVoiceProvider(host="127.0.0.1", port=fixture.port)
        conn = provider.connect()
        time.sleep(0.2)
        conn.close()
        deadline = time.monotonic() + 3
        while not fixture.received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fixture.received == []  # 无 token → 没有 AUTH 帧
    finally:
        fixture.close()


def test_register_pcm_voice_manifest():
    from whale_core.plugins.registry import PluginRegistry

    registry = PluginRegistry()
    provider = PCMVoiceProvider()
    register_pcm_voice(registry, provider)
    assert registry.provider_of("voice_bridge") is provider
    owners = registry.providers_for("voice.pcm_bridge")
    assert [pid for pid, _ in owners] == ["voice_bridge"]
    # 与 realtime chunk/poll 能力名不同，互不冒充
    assert registry.providers_for("voice.realtime") == []


# ---------- 票据与 WebSocket 中继（可选依赖） ----------

websockets = pytest.importorskip("websockets")

from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from whale_core.api.server import ApiContext, WhaleApiServer  # noqa: E402
from whale_core.api.voice import VoiceGateway  # noqa: E402
from whale_core.memory.store import InMemoryStore  # noqa: E402
from whale_core.plugins.manager import PluginManager  # noqa: E402
from whale_core.plugins.registry import PluginRegistry  # noqa: E402

TOKEN = "test-token-123"


def _ctx_with_provider(provider, events=None):
    store = InMemoryStore()
    registry = PluginRegistry()
    manager = PluginManager(registry, store)
    if provider is not None:
        register_pcm_voice(registry, provider)
    ctx = ApiContext(store=store, manager=manager)
    if events is not None:
        ctx.log_event = lambda kind, detail="": events.append((kind, detail))
    return ctx


def test_ticket_shape_one_time_and_quota():
    ctx = _ctx_with_provider(PCMVoiceProvider())
    gateway = VoiceGateway(ctx, host="127.0.0.1", port=0)
    try:
        provider = ctx.manager.registry.provider_of("voice_bridge")
        first = gateway.ticket(provider)
        assert first["protocol"] == "whale-io-pcm"
        assert first["url"].startswith("ws://127.0.0.1:")
        assert first["ticket"] and first["session_id"]
        assert first["sample_rate"] == 16000
        assert first["ticket"] != first["session_id"]
        # 票据 + 会话总数上限 4
        gateway.ticket(provider)
        gateway.ticket(provider)
        gateway.ticket(provider)
        with pytest.raises(ValueError):
            gateway.ticket(provider)
        # 模拟一次成功消费后额度释放
        with gateway.lock:
            gateway.tickets.pop(first["ticket"], None)
        gateway.ticket(provider)
    finally:
        gateway.close()


def test_ticket_expires():
    ctx = _ctx_with_provider(PCMVoiceProvider())
    gateway = VoiceGateway(ctx, host="127.0.0.1", port=0)
    try:
        provider = ctx.manager.registry.provider_of("voice_bridge")
        session = gateway.ticket(provider)
        with gateway.lock:
            gateway.tickets[session["ticket"]]["expires"] = time.monotonic() - 1
        with connect(session["url"], subprotocols=["whale", session["ticket"]],
                     proxy=None) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv()
            assert closed.value.rcvd.code == 1008
    finally:
        gateway.close()


def test_ticket_requires_exactly_one_bearer_subprotocol():
    ctx = _ctx_with_provider(PCMVoiceProvider())
    gateway = VoiceGateway(ctx, host="127.0.0.1", port=0)
    try:
        provider = ctx.manager.registry.provider_of("voice_bridge")
        session = gateway.ticket(provider)
        # 不带票据：握手成功（"whale" 命中），随即被 1008 关闭
        with connect(session["url"], subprotocols=["whale"], proxy=None) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv()
            assert closed.value.rcvd.code == 1008
        # 未知票据 → 1008 关闭，且真票据不受影响（一次性语义只认消费）
        with connect(session["url"], subprotocols=["whale", "wrong-ticket"],
                     proxy=None) as ws:
            with pytest.raises(ConnectionClosed):
                ws.recv()
        with gateway.lock:
            assert session["ticket"] in gateway.tickets
    finally:
        gateway.close()


def test_public_ws_url_overrides_ticket_url():
    provider = PCMVoiceProvider(public_ws_url="wss://bridge.example.com/voice")
    ctx = _ctx_with_provider(provider)
    gateway = VoiceGateway(ctx, host="127.0.0.1", port=0)
    try:
        session = gateway.ticket(provider)
        assert session["url"] == "wss://bridge.example.com/voice"
    finally:
        gateway.close()


def _echo_bridge(fixture_received):
    """PCM 桥夹具：收 AUTH → 回 HELLO → 收一帧音频 → 原样回送，挂住等关闭。"""

    def handler(fixture, sock):
        kind, auth = read_frame(sock)
        fixture.received.append((kind, auth))
        sock.sendall(pack_frame(1, b"HELLO box mode=echo sample=16000"))
        kind, audio = read_frame(sock)
        fixture.received.append((kind, audio))
        sock.sendall(pack_frame(0, audio))
        try:
            sock.recv(1)  # 保持传输打开直到浏览器结束通话
        except OSError:
            pass

    return handler


@pytest.fixture()
def api():
    store = InMemoryStore()
    registry = PluginRegistry()
    manager = PluginManager(registry, store)
    ctx = ApiContext(store=store, manager=manager, profile="demo")
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server
    server.stop()


def _post_session(server, token=TOKEN):
    from http.client import HTTPConnection

    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    conn.request("POST", "/api/v1/voice/session", body="{}", headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


def test_voice_session_503_without_bridge_plugin(api):
    status, data = _post_session(api)
    assert status == 503
    assert data["error"]["code"] == "unavailable"


def test_voice_session_503_when_websockets_missing(api, monkeypatch):
    register_pcm_voice(api.ctx.manager.registry, PCMVoiceProvider())
    monkeypatch.setitem(__import__("sys").modules, "websockets", None)
    status, data = _post_session(api)
    assert status == 503
    assert "voice" in data["error"]["message"]


def test_voice_session_requires_bearer_token(api):
    register_pcm_voice(api.ctx.manager.registry, PCMVoiceProvider())
    status, _ = _post_session(api, token="wrong")
    assert status == 401


def test_real_browser_voice_relay_pcm_auth_and_one_use_ticket(api):
    fixture = _TCPFixture(_echo_bridge(None))
    events: list[tuple[str, str]] = []
    api.ctx.log_event = lambda kind, detail="": events.append((kind, detail))
    provider = PCMVoiceProvider(
        host="127.0.0.1", port=fixture.port, bridge_token="fixture-bridge-secret"
    )
    register_pcm_voice(api.ctx.manager.registry, provider)
    try:
        status, data = _post_session(api)
        assert status == 200, data
        session = data["result"]
        # 桥永久 token 不出现在会话响应里；票据与日志也绝不含它
        assert "fixture-bridge-secret" not in json.dumps(session)
        with connect(session["url"], subprotocols=["whale", session["ticket"]],
                     proxy=None) as ws:
            ready = json.loads(ws.recv())
            assert ready["type"] == "ready"
            assert ready["session_id"] == session["session_id"]
            sample = b"\x01\x00\xff\x7f"
            ws.send(sample)
            assert ws.recv() == sample
            ws.send("stop")
        assert fixture.received == [(1, b"AUTH fixture-bridge-secret"), (0, sample)]
        # 票据一次性：重放被拒
        with connect(session["url"], subprotocols=["whale", session["ticket"]],
                     proxy=None) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv()
            assert closed.value.rcvd.code == 1008
        # 票据值与桥 token 都不进事件日志（只记 session_id）
        logged = json.dumps(events)
        assert session["ticket"] not in logged
        assert "fixture-bridge-secret" not in logged
        assert ("voice:closed", session["session_id"]) in events
    finally:
        fixture.close()


def test_voice_session_relay_closes_when_plugin_disabled(api):
    fixture = _TCPFixture(_echo_bridge(None))
    provider = PCMVoiceProvider(host="127.0.0.1", port=fixture.port)
    register_pcm_voice(api.ctx.manager.registry, provider)
    try:
        status, data = _post_session(api)
        assert status == 200, data
        session = data["result"]
        api.ctx.manager.registry.unregister("voice_bridge")
        with connect(session["url"], subprotocols=["whale", session["ticket"]],
                     proxy=None) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv()
            assert closed.value.rcvd.code == 1008
    finally:
        fixture.close()


def test_gateway_close_rejects_pending_tickets(api):
    register_pcm_voice(api.ctx.manager.registry, PCMVoiceProvider())
    status, data = _post_session(api)
    assert status == 200
    session = data["result"]
    api.stop()
    # 服务停止后票据随网关销毁；新握手被拒绝（连接失败或立即关闭）
    with pytest.raises(Exception):
        with connect(session["url"], subprotocols=["whale", session["ticket"]],
                     proxy=None, open_timeout=2) as ws:
            ws.recv()
