"""whale-io PCM 帧协议 + 握手状态机 + 票据中继测试。

全部在 127.0.0.1 回环上以真实 socket / WebSocket 跑通，不触外网。
线协议夹具 ``tests/fixtures/whale_io_protocol.py`` **直接复制** whale-io
``server.py`` 的帧函数与握手分支（镜像基线 whale-io@48f5993），不简化、
不跳过 AUTH_OK——这是与真实桥联调前的协议镜像层验证。
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from fixtures.whale_io_protocol import WhaleIoBridgeFixture
from whale_core.plugins.pcm_voice import (
    MAX_FRAME,
    PCMHandshakeError,
    PCMVoiceProvider,
    pack_frame,
    read_frame,
    register_pcm_voice,
)


# ---------- 帧协议（whale-io 线格式：长度不含类型字节） ----------


def _socketpair():
    return socket.socketpair()


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


def test_frame_wire_format_matches_whale_io_server():
    """逐字节对照 whale-io server.py write_frame：struct.pack('!IB', len(payload), typ)。"""
    payload = b"HELLO box mode=echo sample=16000"
    assert pack_frame(1, payload) == struct.pack("!IB", len(payload), 1) + payload
    pcm = b"\x00\x01" * 160
    assert pack_frame(0, pcm) == struct.pack("!IB", len(pcm), 0) + pcm


def test_frame_roundtrip_exact_boundary_size():
    a, b = _socketpair()
    try:
        payload = b"\x00" * MAX_FRAME
        # 512KiB 超过回环缓冲：发送放后台线程，接收端边收边排水
        sender = threading.Thread(target=a.sendall, args=(pack_frame(0, payload),))
        sender.start()
        assert read_frame(b) == (0, payload)
        sender.join(timeout=5)
    finally:
        a.close()
        b.close()


def test_frame_zero_length_payload_legal():
    """whale-io read_frame 允许 0 长度 payload；客户端必须同样容忍。"""
    a, b = _socketpair()
    try:
        a.sendall(pack_frame(1, b""))
        assert read_frame(b) == (1, b"")
    finally:
        a.close()
        b.close()


def test_pack_frame_rejects_bad_kind_and_oversize():
    with pytest.raises(ValueError):
        pack_frame(2, b"x")
    with pytest.raises(ValueError):
        pack_frame(0, b"x" * (MAX_FRAME + 1))


def test_read_frame_rejects_bad_length_and_type():
    a, b = _socketpair()
    try:
        a.sendall(struct.pack("!IB", MAX_FRAME + 1, 0))
        with pytest.raises(ValueError):
            read_frame(b)
    finally:
        a.close()
        b.close()
    a, b = _socketpair()
    try:
        a.sendall(struct.pack("!IB", 1, 9) + b"x")  # 未知类型 9
        with pytest.raises(ValueError):
            read_frame(b)
    finally:
        a.close()
        b.close()


def test_read_frame_eof_on_disconnect():
    a, b = _socketpair()
    try:
        a.sendall(struct.pack("!IB", 10, 0) + b"\x00\x01")  # 声明 10 字节只给 2
        a.close()
        with pytest.raises(EOFError):
            read_frame(b)
    finally:
        b.close()


# ---------- PCMVoiceProvider 校验 ----------


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


# ---------- 握手状态机（whale-io 真实帧序夹具） ----------


def test_handshake_with_token_full_sequence():
    """token 匹配：AUTH → AUTH_OK → HELLO，connect 返回即可读音频流。"""
    fixture = WhaleIoBridgeFixture(token="bridge-secret", mode="echo")
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, bridge_token="bridge-secret"
        )
        conn = provider.connect()
        assert provider.last_hello["mode"] == "echo"
        assert provider.last_hello["sample"] == "16000"
        # 握手完成后 PCM 回环可用
        conn.sendall(pack_frame(0, b"\x01\x00\xff\x7f"))
        assert read_frame(conn) == (0, b"\x01\x00\xff\x7f")
        conn.close()
        deadline = time.monotonic() + 3
        while not fixture.received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fixture.received[0] == (1, b"AUTH bridge-secret")
    finally:
        fixture.close()


def test_handshake_without_token_direct_hello():
    """无 token：server 直接 HELLO；客户端不得发送 AUTH 帧。"""
    fixture = WhaleIoBridgeFixture(mode="omni")
    try:
        provider = PCMVoiceProvider(host="127.0.0.1", port=fixture.port)
        conn = provider.connect()
        assert provider.last_hello["mode"] == "omni"
        conn.close()
        deadline = time.monotonic() + 3
        while not fixture.received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(kind != 1 or not p.startswith(b"AUTH ") for kind, p in fixture.received)
    finally:
        fixture.close()


def test_handshake_wrong_token_server_closes():
    """真实 handle_client：鉴权失败不发 AUTH_FAIL，直接断开 → 必须拒绝且释放。"""
    fixture = WhaleIoBridgeFixture(token="right-token")
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, bridge_token="wrong-token", timeout=5
        )
        with pytest.raises(PCMHandshakeError):
            provider.connect()
        assert fixture.client_closed.wait(3)
    finally:
        fixture.close()


def test_handshake_auth_fail_frame_rejected():
    """mode handler 分支会发 AUTH_FAIL：也必须明确拒绝。"""
    fixture = WhaleIoBridgeFixture(token="right-token", script="auth_fail_frame")
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, bridge_token="wrong-token"
        )
        with pytest.raises(PCMHandshakeError, match="rejected"):
            provider.connect()
    finally:
        fixture.close()


def test_handshake_hello_before_auth_ok_rejected():
    """乱序：期望 AUTH_OK 时收到 HELLO → 拒绝。"""
    fixture = WhaleIoBridgeFixture(token="t", script="hello_first")
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, bridge_token="t"
        )
        with pytest.raises(PCMHandshakeError):
            provider.connect()
    finally:
        fixture.close()


def test_handshake_pcm_before_greeting_rejected():
    """乱序：握手阶段收到 PCM 帧 → 拒绝。"""
    fixture = WhaleIoBridgeFixture(token="t", script="pcm_first")
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, bridge_token="t"
        )
        with pytest.raises(PCMHandshakeError, match="帧序错乱"):
            provider.connect()
    finally:
        fixture.close()


def test_handshake_timeout_rejected():
    """server 在鉴权窗口内不响应 → 超时拒绝，socket 关闭。"""
    fixture = WhaleIoBridgeFixture(token="t", auth_delay=30.0)
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, bridge_token="t", timeout=1
        )
        started = time.monotonic()
        with pytest.raises(PCMHandshakeError, match="timeout"):
            provider.connect()
        assert time.monotonic() - started < 5
    finally:
        fixture.close()


def test_handshake_close_on_connect_rejected():
    fixture = WhaleIoBridgeFixture(script="close_on_connect")
    try:
        provider = PCMVoiceProvider(
            host="127.0.0.1", port=fixture.port, timeout=2
        )
        with pytest.raises(PCMHandshakeError):
            provider.connect()
    finally:
        fixture.close()


def test_handshake_no_token_but_server_requires_auth():
    """客户端无 token、server 要求鉴权：server 8s 后断开 → 客户端超时拒绝。"""
    fixture = WhaleIoBridgeFixture(token="server-side-secret")
    try:
        provider = PCMVoiceProvider(host="127.0.0.1", port=fixture.port, timeout=2)
        with pytest.raises(PCMHandshakeError):
            provider.connect()
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
    """全链路：浏览器 → WS 票据 → AUTH → AUTH_OK → HELLO → PCM 回环。

    桥侧是 whale-io 真实帧序夹具（含 BRIDGE_TOKEN 鉴权分支）。
    """
    fixture = WhaleIoBridgeFixture(token="fixture-bridge-secret", mode="echo")
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
        assert (1, b"AUTH fixture-bridge-secret") in fixture.received
        assert (0, sample) in fixture.received
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


def test_voice_session_relay_rejected_when_bridge_auth_fails(api):
    """桥鉴权失败（真实行为：直接断开）→ 浏览器拿到 1011，不得暴露 ready。"""
    fixture = WhaleIoBridgeFixture(token="real-secret", mode="echo")
    provider = PCMVoiceProvider(
        host="127.0.0.1", port=fixture.port, bridge_token="stale-secret", timeout=5
    )
    register_pcm_voice(api.ctx.manager.registry, provider)
    try:
        status, data = _post_session(api)
        assert status == 200, data
        session = data["result"]
        with connect(session["url"], subprotocols=["whale", session["ticket"]],
                     proxy=None) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                ws.recv()
            assert closed.value.rcvd.code == 1011
    finally:
        fixture.close()


def test_voice_session_relay_closes_when_plugin_disabled(api):
    fixture = WhaleIoBridgeFixture(mode="echo")
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
