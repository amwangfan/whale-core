"""PR#4 渠道桥接移植的离线测试。

覆盖 JsonProvider / HttpBridgeProvider / HttpPluginExecutor /
WhaleIOChannelProvider 的 URL 校验、无重定向、错误分类与幂等键透传。

全部离线：默认 monkeypatch ``provider_http.open_request``；仅重定向用例起
127.0.0.1 回环 http.server（不触外网），与 test_model_provider.py 同款。
"""
import io
import json
import threading
import urllib.error

import pytest

from whale_core.plugins import provider_http
from whale_core.plugins.bridge import HttpBridgeProvider, HttpPluginExecutor
from whale_core.plugins.provider_http import JsonProvider, ProviderError
from whale_core.plugins.whale_io import WhaleIOChannelProvider


def _provider(**kw):
    kw.setdefault("base_url", "http://localhost:9/v1")  # 回环 HTTP：免 api_key
    return JsonProvider(**kw)


def _transport(monkeypatch, handler):
    """fake open_request：记录请求，返回 handler(body) 的 JSON。全部离线。"""
    calls = []

    def fake(req, **kw):
        body = json.loads(req.data) if req.data else None
        calls.append({"url": req.full_url, "body": body, "req": req})
        return io.BytesIO(json.dumps(handler(body)).encode("utf-8"))

    monkeypatch.setattr(provider_http, "open_request", fake)
    return calls


def _http_error(code):
    def fake(req, **kw):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)
    return fake


# ── JsonProvider：URL / 超时校验 ─────────────────────────────


@pytest.mark.parametrize("url", [
    "https://api.example.com",
    "https://api.example.com/v1/",          # 尾斜杠会被剥掉
    "http://localhost:9000",
    "http://127.0.0.1:8080",
    "http://192.168.1.10:8000",
    "http://[::1]:9000",
])
def test_base_url_accepted(url):
    p = JsonProvider(base_url=url)
    assert p.base_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://api.example.com",               # 公网明文 HTTP 拒绝
    "http://127.evil.com",                  # 词法前缀不算回环
    "https://user:pass@api.example.com",    # URL 禁带凭据
    "https://api.example.com/v1?x=1",       # 禁 query
    "https://api.example.com/#frag",        # 禁 fragment
    "https://",                             # 无主机
    "ftp://example.com",                    # 非 http(s)
    "https://api.example.com:99999",        # 端口越界
])
def test_base_url_rejected(url):
    with pytest.raises(ValueError):
        JsonProvider(base_url=url)


@pytest.mark.parametrize("timeout", [0, -1, 121, float("nan"), float("inf")])
def test_timeout_bounds(timeout):
    with pytest.raises(ValueError):
        _provider(timeout=timeout)
    assert _provider(timeout=120).timeout == 120


# ── JsonProvider：healthcheck / 请求封装 ──────────────────────


def test_healthcheck():
    assert _provider(api_key="k").healthcheck() is True
    assert _provider().healthcheck() is True  # 回环 HTTP 免凭据
    assert JsonProvider(base_url="https://api.example.com").healthcheck() == \
        "missing credentials"


def test_missing_credentials_fail_closed():
    p = JsonProvider(base_url="https://api.example.com")
    with pytest.raises(ProviderError) as exc:
        p.request("/x", {})
    assert exc.value.kind == "config"


def test_request_posts_json_with_auth_and_bounds(monkeypatch):
    calls = _transport(monkeypatch, lambda body: {"ok": True})
    p = JsonProvider(base_url="https://bridge.example/api", api_key="secret")
    assert p.request("/messages", {"text": "你好"}) == {"ok": True}
    (call,) = calls
    assert call["url"] == "https://bridge.example/api/messages"
    assert call["body"] == {"text": "你好"}
    req = call["req"]
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Authorization") == "Bearer secret"


def test_request_extra_headers_and_empty_path(monkeypatch):
    calls = _transport(monkeypatch, lambda body: {"ok": True})
    p = _provider()
    p.request("", {"a": 1}, extra_headers={"X-Trace": "t1"})
    (call,) = calls
    assert call["url"] == "http://localhost:9/v1"  # 空 path 直打 base_url
    assert call["req"].get_header("X-trace") == "t1"  # urllib 规范化首字母大写


@pytest.mark.parametrize("path", ["messages", "https://evil.example/x", "x://y"])
def test_request_path_must_stay_on_origin(path):
    with pytest.raises(ValueError):
        _provider().request(path, {})


# ── JsonProvider：错误分类 ────────────────────────────────────


@pytest.mark.parametrize("code,kind", [
    (401, "auth"), (403, "auth"), (429, "quota"),
    (500, "server"), (503, "server"),
    (302, "http_302"), (404, "http_404"), (418, "http_418"),
])
def test_http_errors_classified(monkeypatch, code, kind):
    monkeypatch.setattr(provider_http, "open_request", _http_error(code))
    with pytest.raises(ProviderError) as exc:
        _provider().request("/x", {})
    assert exc.value.kind == kind


@pytest.mark.parametrize("boom", [
    urllib.error.URLError("refused"), TimeoutError("slow"), ConnectionResetError(),
])
def test_network_errors_classified(monkeypatch, boom):
    def fake(req, **kw):
        raise boom
    monkeypatch.setattr(provider_http, "open_request", fake)
    with pytest.raises(ProviderError) as exc:
        _provider().request("/x", {})
    assert exc.value.kind == "network"


@pytest.mark.parametrize("raw", [
    b"not json",              # 解析失败
    b"[1,2]",                 # 非 JSON object
    b'"text"',                # 标量
    b"x" * (4 * 1024 * 1024 + 1),  # 超 4 MiB 有界读取上限
])
def test_bad_responses_classified(monkeypatch, raw):
    monkeypatch.setattr(
        provider_http, "open_request", lambda *a, **k: io.BytesIO(raw))
    with pytest.raises(ProviderError) as exc:
        _provider().request("/x", {})
    assert exc.value.kind == "bad_response"


def test_credentials_never_follow_redirect_on_wire():
    """真实回环 wire 测试：302 不得带凭据跳转（不触外网）。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{destination.server_port}/stolen")
            self.end_headers()

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threads = [threading.Thread(target=s.serve_forever, daemon=True)
               for s in (destination, origin)]
    for thread in threads:
        thread.start()
    try:
        p = JsonProvider(base_url=f"http://127.0.0.1:{origin.server_port}",
                         api_key="secret-bridge-key", timeout=5)
        with pytest.raises(ProviderError) as exc:
            p.request("/sessions", {"x": 1})
        assert exc.value.kind == "http_302"
        assert seen == []  # 重定向目标从未收到请求，凭据未外流
    finally:
        origin.shutdown()
        destination.shutdown()
        origin.server_close()
        destination.server_close()


# ── HttpBridgeProvider：会话与消息桥 ──────────────────────────


def _bridge(monkeypatch, handler):
    calls = _transport(monkeypatch, handler)
    bridge = HttpBridgeProvider(base_url="https://bridge.example",
                                api_key="k", model="m1")
    return bridge, calls


def test_start_session_contract_and_persona_bound(monkeypatch):
    bridge, calls = _bridge(
        monkeypatch, lambda body: {"session_id": "srv-1", "url": "wss://bridge.example/rtc"})
    out = bridge.start_session(persona="x" * 9000, session_id="req-1")
    assert out == {"session_id": "srv-1", "url": "wss://bridge.example/rtc"}
    (call,) = calls
    assert call["url"] == "https://bridge.example/sessions"
    assert call["body"]["session_id"] == "req-1"
    assert call["body"]["model"] == "m1"
    assert len(call["body"]["instructions"]) == 4000  # 人格指令有界截断


def test_start_session_falls_back_to_requested_id(monkeypatch):
    bridge, _ = _bridge(
        monkeypatch, lambda body: {"url": "https://bridge.example/rtc"})
    out = bridge.start_session(session_id="req-9")
    assert out["session_id"] == "req-9"


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "ftp://x", 123, None])
def test_start_session_rejects_bad_connection_url(monkeypatch, url):
    bridge, _ = _bridge(monkeypatch, lambda body: {"url": url})
    with pytest.raises(ProviderError) as exc:
        bridge.start_session()
    assert exc.value.kind == "bad_response"


def test_send_passes_idempotency_key_through(monkeypatch):
    bridge, calls = _bridge(monkeypatch, lambda body: {"accepted": True})
    out = bridge.send("hud", "device-1", "经批准的文案", idempotency_key="draft-123")
    assert out == {"accepted": True}
    (call,) = calls
    assert call["url"] == "https://bridge.example/messages"
    assert call["body"] == {
        "channel": "hud",
        "target": "device-1",
        "text": "经批准的文案",
        "idempotency_key": "draft-123",  # 幂等键原样透传，桥侧按它去重
    }


@pytest.mark.parametrize("kwargs", [
    {"channel": "", "target": "t", "text": "x", "idempotency_key": "k"},
    {"channel": "hud", "target": "", "text": "x", "idempotency_key": "k"},
    {"channel": "hud", "target": "t", "text": "x" * 8001, "idempotency_key": "k"},
    {"channel": "hud", "target": "t", "text": "x", "idempotency_key": ""},
])
def test_send_validates_before_any_network(monkeypatch, kwargs):
    bridge, calls = _bridge(monkeypatch, lambda body: {})
    with pytest.raises(ValueError):
        bridge.send(kwargs["channel"], kwargs["target"], kwargs["text"],
                    idempotency_key=kwargs["idempotency_key"])
    assert calls == []


def test_bridge_error_classification(monkeypatch):
    monkeypatch.setattr(provider_http, "open_request", _http_error(429))
    bridge = HttpBridgeProvider(base_url="https://bridge.example", api_key="k")
    with pytest.raises(ProviderError) as exc:
        bridge.send("hud", "t", "x", idempotency_key="k")
    assert exc.value.kind == "quota"


# ── HttpPluginExecutor：外置 manifest 插件传输 ────────────────


def test_executor_validates_plugin_id_and_config_keys():
    with pytest.raises(ValueError):
        HttpPluginExecutor("Bad ID!", {"base_url": "http://localhost:1"})
    with pytest.raises(ValueError):
        HttpPluginExecutor(None, {"base_url": "http://localhost:1"})
    with pytest.raises(ValueError):
        HttpPluginExecutor("my_notify", {"base_url": "http://localhost:1",
                                         "evil_key": 1})
    with pytest.raises(ValueError):  # config 透传 URL 校验
        HttpPluginExecutor("my_notify", {"base_url": "http://public.example"})


def test_executor_posts_execute_envelope(monkeypatch):
    calls = _transport(monkeypatch,
                       lambda body: {"ok": True, "receipt": "queued #7"})
    ex = HttpPluginExecutor("my_notify", {"base_url": "https://bridge.example",
                                          "api_key": "k"})
    assert ex.healthcheck() is True
    ok, receipt = ex.execute("notify", "owner", {"text": "你好"})
    assert (ok, receipt) == (True, "queued #7")
    (call,) = calls
    assert call["url"] == "https://bridge.example/execute"
    assert call["body"] == {"plugin_id": "my_notify", "action": "notify",
                            "target": "owner", "args": {"text": "你好"}}


def test_executor_receipt_bounded_and_ok_strict(monkeypatch):
    calls = _transport(monkeypatch, lambda body: {"ok": 1, "receipt": "r" * 1000})
    ex = HttpPluginExecutor("my_notify", {"base_url": "http://localhost:1"})
    ok, receipt = ex.execute("notify", "owner", {})
    assert ok is False  # 只认严格的 True
    assert len(receipt) == 500  # 回执有界截断
    monkeypatch.setattr(provider_http, "open_request",
                        lambda *a, **k: io.BytesIO(b'{"ok": true}'))
    ok, receipt = ex.execute("notify", "owner", {})
    assert ok is True and receipt == "no execution receipt"


def test_executor_error_classification(monkeypatch):
    monkeypatch.setattr(provider_http, "open_request", _http_error(401))
    ex = HttpPluginExecutor("my_notify", {"base_url": "https://bridge.example",
                                          "api_key": "bad"})
    with pytest.raises(ProviderError) as exc:
        ex.execute("notify", "owner", {})
    assert exc.value.kind == "auth"


# ── WhaleIOChannelProvider：原生适配器骨架（契约未联调，语义从严）────


def test_whale_io_unconfigured_fails_closed():
    provider = WhaleIOChannelProvider()
    assert provider.healthcheck() == \
        "configure a dashboard, ring hub or WeQ endpoint"
    with pytest.raises(ValueError):
        provider.send("hud", "t", "x", idempotency_key="k")


def test_whale_io_dashboard_card_contract(monkeypatch):
    calls = _transport(monkeypatch, lambda body: {"ok": True})
    provider = WhaleIOChannelProvider(dashboard_url="http://localhost:9/hud",
                                      dashboard_token="t")
    assert provider.healthcheck() is True
    out = provider.send("hud", "ignored", "x" * 900, idempotency_key="k")
    assert out == {"accepted": True}
    (call,) = calls
    assert call["url"] == "http://localhost:9/hud"  # 卡片直打配置地址
    assert call["body"]["layout_hint"] == "default"
    assert len(call["body"]["body"]) == 800  # 卡片正文有界


def test_whale_io_dashboard_explicit_reject_not_accepted(monkeypatch):
    _transport(monkeypatch, lambda body: {"ok": False})
    provider = WhaleIOChannelProvider(dashboard_url="http://localhost:9")
    assert provider.send("hud", "t", "x", idempotency_key="k") == \
        {"accepted": False}


def test_whale_io_ring_keeps_number_masked(monkeypatch):
    calls = _transport(monkeypatch,
                       lambda body: {"ok": True, "delivered": True})
    provider = WhaleIOChannelProvider(ring_url="http://localhost:9",
                                      ring_token="t")
    out = provider.send("call", "device-1", "回家提醒", idempotency_key="k")
    assert out == {"accepted": True, "delivered": True}
    (call,) = calls
    assert call["url"] == "http://localhost:9/v1/ring"
    assert call["body"]["device_id"] == "device-1"
    assert call["body"]["number"] == "***"  # 掩码占位：骨架不携带真实号码


def test_whale_io_ring_delivered_defaults_false(monkeypatch):
    _transport(monkeypatch, lambda body: {"ok": True})
    provider = WhaleIOChannelProvider(ring_url="http://localhost:9")
    assert provider.send("call", "d", "n", idempotency_key="k") == \
        {"accepted": True, "delivered": False}


def test_whale_io_weq_uses_injected_port_only():
    """WeQ 不自建 sender：宿主注入 OutboundPort，幂等键进草稿与证据链。"""
    sent = []

    class FakePort:
        def send(self, draft):
            sent.append(draft)
            return True

    provider = WhaleIOChannelProvider(weq_sender=FakePort())
    assert provider.healthcheck() is True
    out = provider.send("direct_message", "owner", "晚上好",
                        idempotency_key="draft-42")
    assert out == {"accepted": True}
    (draft,) = sent
    assert draft.draft_id == "draft-42"
    assert draft.channel == "direct_message"
    assert draft.evidence == ("api:draft-42",)
    with pytest.raises(ValueError):  # 未注入 sender 的渠道明确失败
        WhaleIOChannelProvider().send("direct_message", "owner", "x",
                                      idempotency_key="k")
