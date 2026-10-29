"""QQ/OneBot 适配器离线测试：解析、鉴权、配对、出站受理、HTTP 传输构造。

全部合成数据：不起真实协议端、不访问外网；监听器冒烟仅回环地址，绑定不可用时跳过。
"""
import json
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from whale_core.life.channels import OwnerChannelRegistry
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
from whale_core.life.qq_onebot import (
    MAX_TEXT_CHARS,
    HttpOneBotTransport,
    NullOneBotTransport,
    OneBotHttpListener,
    OneBotInboundBridge,
    OneBotOutboundPort,
    OneBotTransportError,
    bearer_authenticated,
    parse_onebot_message,
)
from whale_core.memory.store import InMemoryStore

TOKEN = "synthetic-token"


def private_event(uid=10001, mid=7, text="你好", **extra):
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": uid,
        "self_id": 20002,
        "message_id": mid,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    event.update(extra)
    return event


def group_event(uid=10001, gid=30003, mid=9, text="群聊消息"):
    return {
        "post_type": "message",
        "message_type": "group",
        "user_id": uid,
        "group_id": gid,
        "self_id": 20002,
        "message_id": mid,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def make_bridge(*, accept_group=False, bindings=None):
    registry = OwnerChannelRegistry(
        owner_id="u1",
        bindings={("qq", "10001"): "u1"} if bindings is None else bindings,
    )
    return OneBotInboundBridge(registry, accept_group=accept_group)


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = {"retcode": 0, "status": "ok", "data": {"message_id": 1}} \
            if response is None else response
        self.error = error

    def call(self, action, params):
        self.calls.append((action, params))
        if self.error:
            raise self.error
        return self.response


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_opener(payload=b'{"retcode":0}', error=None):
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        if error:
            raise error
        return FakeResponse(payload)

    return opener, captured


# ── 事件解析 ──────────────────────────────────────────────────

def test_parse_private_message_fields():
    incoming = parse_onebot_message(private_event(), authenticated=True)
    assert incoming is not None
    assert (incoming.channel, incoming.account, incoming.conversation) == ("qq", "10001", "private:10001")
    assert incoming.message_id == "7"
    assert incoming.text == "你好"
    assert incoming.transport_authenticated is True
    # 鉴权标志如实透传：未鉴权事件仍然可解析，由 registry 拒绝
    assert parse_onebot_message(private_event(), authenticated=False).transport_authenticated is False


def test_parse_extracts_only_text_segments_and_string_form():
    event = private_event(message=[
        {"type": "text", "data": {"text": "前半"}},
        {"type": "image", "data": {"file": "x.png"}},
        {"type": "at", "data": {"qq": "20002"}},
        {"type": "text", "data": {"text": "后半"}},
    ])
    assert parse_onebot_message(event, authenticated=True).text == "前半后半"
    assert parse_onebot_message(private_event(message="字符串形态"), authenticated=True).text == "字符串形态"
    assert parse_onebot_message(private_event(message=None), authenticated=True).text == ""


def test_parse_rejects_unusable_events():
    assert parse_onebot_message({"post_type": "meta_event"}, authenticated=True) is None
    assert parse_onebot_message({"post_type": "notice"}, authenticated=True) is None
    assert parse_onebot_message(private_event(message_id=None), authenticated=True) is None
    assert parse_onebot_message(private_event(user_id=None), authenticated=True) is None
    assert parse_onebot_message("not-a-dict", authenticated=True) is None
    # bot 自己发的消息不处理
    assert parse_onebot_message(private_event(uid=20002), authenticated=True) is None


def test_group_rejected_by_default_and_opt_in():
    assert parse_onebot_message(group_event(), authenticated=True) is None
    incoming = parse_onebot_message(group_event(), authenticated=True, accept_group=True)
    assert incoming.conversation == "group:30003"
    assert parse_onebot_message(group_event(gid=None), authenticated=True, accept_group=True) is None


def test_overlong_text_truncated():
    incoming = parse_onebot_message(private_event(text="长" * (MAX_TEXT_CHARS + 500)), authenticated=True)
    assert len(incoming.text) == MAX_TEXT_CHARS


# ── 鉴权与桥接 ────────────────────────────────────────────────

def test_bearer_authenticated_fail_closed():
    assert bearer_authenticated(f"Bearer {TOKEN}", TOKEN) is True
    assert bearer_authenticated(f"bearer {TOKEN}", TOKEN) is True
    assert bearer_authenticated("Bearer wrong", TOKEN) is False
    assert bearer_authenticated(None, TOKEN) is False
    assert bearer_authenticated(TOKEN, TOKEN) is False  # 缺 scheme
    assert bearer_authenticated(f"Bearer {TOKEN}", "") is False  # 未配置 token 一律拒绝


def test_bridge_only_paired_authenticated_owner():
    bridge = make_bridge()
    event = bridge.handle_event(private_event(), authenticated=True)
    assert event is not None and event.owner_id == "u1" and event.text == "你好"
    assert bridge.handle_event(private_event(), authenticated=False) is None
    assert bridge.handle_event(private_event(uid=99999), authenticated=True) is None
    assert bridge.handle_event(group_event(), authenticated=True) is None
    assert bridge.handle_event({"post_type": "meta_event"}, authenticated=True) is None


def test_http_request_status_codes():
    bridge = make_bridge()
    body = json.dumps(private_event()).encode()
    ok_headers = {"Authorization": f"Bearer {TOKEN}"}
    assert bridge.handle_http_request(ok_headers, body, expected_token=TOKEN) == (
        204, bridge.handle_event(private_event(), authenticated=True))
    assert bridge.handle_http_request({}, body, expected_token=TOKEN)[0] == 401
    assert bridge.handle_http_request({"Authorization": "Bearer bad"}, body, expected_token=TOKEN)[0] == 401
    assert bridge.handle_http_request(ok_headers, body, expected_token="")[0] == 401
    assert bridge.handle_http_request(ok_headers, None, expected_token=TOKEN)[0] == 413
    assert bridge.handle_http_request(ok_headers, b"x" * 1_000_001, expected_token=TOKEN)[0] == 413
    assert bridge.handle_http_request(ok_headers, b"{broken", expected_token=TOKEN)[0] == 400
    assert bridge.handle_http_request(ok_headers, b"[1,2]", expected_token=TOKEN)[0] == 400
    # 鉴权通过但事件无用（meta_event）：204 且无 owner 事件
    code, event = bridge.handle_http_request(ok_headers, json.dumps({"post_type": "meta_event"}).encode(),
                                             expected_token=TOKEN)
    assert (code, event) == (204, None)


# ── 出站端口 ──────────────────────────────────────────────────

def make_draft(channel="direct_message", text="提醒：明天有实验课"):
    return ProactiveDraft("qq-1", text, "日程提醒", ("event-1",), channel=channel)


def test_outbound_disabled_by_default_and_channel_scoped():
    transport = FakeTransport()
    port = OneBotOutboundPort(transport, user_id="10001")
    assert port.send(make_draft()) is False
    assert transport.calls == []
    port.enabled = True
    assert port.send(make_draft(channel="hud")) is False
    assert transport.calls == []


def test_outbound_sends_only_bound_private_target():
    transport = FakeTransport()
    port = OneBotOutboundPort(transport, user_id="10001", enabled=True)
    assert port.send(make_draft()) is True
    action, params = transport.calls[0]
    assert action == "send_private_msg"
    assert params == {"user_id": 10001,
                      "message": [{"type": "text", "data": {"text": "提醒：明天有实验课"}}]}
    assert port.last_receipt == {"retcode": 0, "status": "ok", "data": {"message_id": 1}}


def test_outbound_failures_report_false_not_raise():
    ok = OneBotOutboundPort(FakeTransport(response={"retcode": 0}), user_id="1", enabled=True)
    assert ok.send(make_draft()) is True
    bad = OneBotOutboundPort(FakeTransport(response={"retcode": 1404, "status": "failed"}),
                             user_id="1", enabled=True)
    assert bad.send(make_draft()) is False
    broken = OneBotOutboundPort(FakeTransport(error=OneBotTransportError("down")),
                                user_id="1", enabled=True)
    assert broken.send(make_draft()) is False
    stub = OneBotOutboundPort(NullOneBotTransport(), user_id="1", enabled=True)
    assert stub.send(make_draft()) is False  # 默认 stub 永不触网
    long_text = OneBotOutboundPort(FakeTransport(), user_id="1", enabled=True)
    assert long_text.send(make_draft(text="长" * (MAX_TEXT_CHARS + 1))) is False


def test_outbound_rejects_invalid_user_id():
    with pytest.raises(ValueError):
        OneBotOutboundPort(FakeTransport(), user_id="not-a-number")


def test_outbox_integration_review_gate_before_transport():
    outbox = ProactiveOutbox(InMemoryStore())
    outbox.propose(make_draft())
    prefs = ProactivePrefs(enabled=True, auto_send=True,
                           allowed_channels=frozenset({"direct_message"}))
    transport = FakeTransport()
    port = OneBotOutboundPort(transport, user_id="10001", enabled=True)
    now = datetime(2026, 10, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    # direct_message 强制人工批准：未批准时传输层零调用
    verdict = outbox.dispatch("qq-1", prefs, now=now, port=port)
    assert verdict.status == "needs_approval"
    assert transport.calls == []
    # 批准后受理；accepted 仅是渠道受理
    verdict = outbox.dispatch("qq-1", prefs, now=now, port=port, explicitly_approved=True)
    assert verdict.status == "accepted"
    assert len(transport.calls) == 1
    assert outbox.ledger("qq-1")["status"] == "accepted"
    # 不自动重发
    assert outbox.dispatch("qq-1", prefs, now=now, port=port,
                           explicitly_approved=True).status == "already_attempted"


# ── HTTP 传输构造（注入 opener，不触网）────────────────────────

def test_http_transport_builds_authorized_request():
    opener, captured = fake_opener(payload=b'{"retcode":0,"status":"ok","data":{"message_id":5}}')
    transport = HttpOneBotTransport("http://127.0.0.1:3000/", token=TOKEN, timeout=3.0, opener=opener)
    result = transport.call("send_private_msg", {"user_id": 1})
    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:3000/send_private_msg"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode()) == {"user_id": 1}
    assert captured["timeout"] == 3.0
    assert result["data"]["message_id"] == 5
    # 无 token 时不带 Authorization 头
    opener2, captured2 = fake_opener()
    HttpOneBotTransport("http://127.0.0.1:3000", opener=opener2).call("get_status", {})
    assert captured2["request"].get_header("Authorization") is None


def test_http_transport_errors_do_not_leak_token():
    http_error = urllib.error.HTTPError("http://x/send_private_msg", 500, "ISE", None, None)
    for error in (http_error, urllib.error.URLError("refused"), OSError("boom")):
        opener, _ = fake_opener(error=error)
        transport = HttpOneBotTransport("http://127.0.0.1:3000", token=TOKEN, opener=opener)
        with pytest.raises(OneBotTransportError) as excinfo:
            transport.call("send_private_msg", {})
        assert TOKEN not in str(excinfo.value)
    opener, _ = fake_opener(payload=b"not-json")
    with pytest.raises(OneBotTransportError):
        HttpOneBotTransport("http://127.0.0.1:3000", opener=opener).call("x", {})
    opener, _ = fake_opener(payload=b"[1]")
    with pytest.raises(OneBotTransportError):
        HttpOneBotTransport("http://127.0.0.1:3000", opener=opener).call("x", {})
    with pytest.raises(ValueError):
        HttpOneBotTransport("  ")


# ── 监听器（仅回环；绑定不可用则跳过）─────────────────────────

class RecordingBridge(OneBotInboundBridge):
    def __init__(self, registry):
        super().__init__(registry)
        self.events = []

    def handle_event(self, event, *, authenticated):
        result = super().handle_event(event, authenticated=authenticated)
        self.events.append((event, authenticated, result))
        return result


def test_listener_requires_token():
    with pytest.raises(ValueError):
        OneBotHttpListener(make_bridge(), expected_token="")


def test_listener_loopback_smoke():
    bridge = RecordingBridge(OwnerChannelRegistry(owner_id="u1", bindings={("qq", "10001"): "u1"}))
    listener = OneBotHttpListener(bridge, expected_token=TOKEN, port=0)
    try:
        listener.start()
    except OSError:
        pytest.skip("loopback bind unavailable in this environment")
    try:
        url = f"http://127.0.0.1:{listener.port}/onebot"
        body = json.dumps(private_event()).encode()
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 204
        assert len(bridge.events) == 1
        event, authenticated, owner_event = bridge.events[0]
        assert authenticated is True and owner_event is not None and owner_event.owner_id == "u1"
        # 错误 token → 401，且不产生事件
        bad = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(bad, timeout=5)
        assert excinfo.value.code == 401
        assert len(bridge.events) == 1
        # GET 探活 → 204
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 204
    finally:
        listener.stop()
