"""WeQWebSendPort 离线测试：登录、发送、会话过期重登、失败如实报 False。

注入 fake opener，不触网、不依赖真实 WeQ。
"""
import json

import pytest

from whale_core.life.initiative import ProactiveDraft
from weq_channel.weq_send import MAX_TEXT_CHARS, WeQSendError, WeQWebSendPort

BASE = "http://127.0.0.1:7690"
TOKEN = "synthetic-web-token"
UID = "u_X5hSynthetic"


class FakeResponse:
    def __init__(self, status=200, body=None, set_cookie=None, raw=None):
        self.status = status
        self._raw = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        self.headers = {"Set-Cookie": set_cookie} if set_cookie else {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """按 (method, path) 队列回放响应；记录请求供断言。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append({"url": request.full_url, "body": request.data,
                              "headers": dict(request.header_items()), "timeout": timeout})
        item = self.responses.pop(0) if self.responses else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


def draft(text="测试回复", channel="direct_message"):
    return ProactiveDraft("d1", text, "QQ 回复", ("qq:m1",), channel=channel)


def login_ok():
    return FakeResponse(200, raw=b"ok", set_cookie="weq_session=abc123; Path=/; HttpOnly")


def send_result(ok=True, extra=None):
    outcome = {"ok": ok, "peerType": "c2c", "targetId": UID, **(extra or {})}
    return FakeResponse(200, {"result": {"data": {"json": outcome}}})


def test_login_and_send_success():
    opener = FakeOpener([login_ok(), send_result(True)])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    assert port.send(draft()) is True
    assert port.last_receipt["ok"] is True
    login_req, send_req = opener.requests
    assert login_req["url"] == BASE + "/_auth/login"
    assert json.loads(login_req["body"]) == {"token": TOKEN}
    assert send_req["url"] == BASE + "/trpc/account.sendElements"
    assert send_req["headers"]["Cookie"] == "weq_session=abc123"
    payload = json.loads(send_req["body"])["json"]
    assert payload["peerType"] == "c2c"
    assert payload["targetId"] == UID
    assert payload["elements"] == [{"kind": "text", "textContent": "测试回复"}]
    assert TOKEN not in json.dumps(send_req["headers"])  # token 不随发送请求外泄


def test_send_rejected_by_server_is_false():
    opener = FakeOpener([login_ok(), send_result(False, {"errMsg": "完整离线模式"})])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    assert port.send(draft()) is False
    assert port.last_receipt["errMsg"] == "完整离线模式"


def test_session_expiry_relogins_once():
    expired = FakeResponse(401, raw=b"unauthorized")
    opener = FakeOpener([login_ok(), expired, login_ok(), send_result(True)])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    assert port.send(draft()) is True
    assert len(opener.requests) == 4  # login, 401, relogin, resend


def test_login_failure_raises_and_send_reports_false():
    opener = FakeOpener([FakeResponse(401, raw=b"unauthorized")])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    with pytest.raises(WeQSendError):
        port.login()
    opener2 = FakeOpener([FakeResponse(401, raw=b"unauthorized")])
    port2 = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener2)
    assert port2.send(draft()) is False  # send 内登录失败 → False 不抛


def test_unreachable_and_http_errors_are_false():
    import urllib.error
    opener = FakeOpener([urllib.error.URLError("refused")])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    assert port.send(draft()) is False


def test_scoping_and_limits():
    from types import SimpleNamespace
    opener = FakeOpener([login_ok(), send_result(True)])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    assert port.send(draft(channel="hud")) is False       # 非 direct_message 不发
    # 空文本/超长：ProactiveDraft 构造期已拦空文本，这里用鸭子对象测端口自身的防御层
    assert port.send(SimpleNamespace(channel="direct_message", text="  ")) is False
    assert port.send(SimpleNamespace(channel="direct_message",
                                     text="长" * (MAX_TEXT_CHARS + 1))) is False
    assert opener.requests == []                           # 以上都没触网


def test_expected_account_fields_passed_when_configured():
    opener = FakeOpener([login_ok(), send_result(True)])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, expected_account_uin="123",
                          expected_account_config_id="123_ab", opener=opener)
    assert port.send(draft()) is True
    payload = json.loads(opener.requests[1]["body"])["json"]
    assert payload["expectedAccountUin"] == "123"
    assert payload["expectedAccountConfigId"] == "123_ab"


def test_constructor_validation():
    with pytest.raises(ValueError):
        WeQWebSendPort("", TOKEN, target_uid=UID)
    with pytest.raises(ValueError):
        WeQWebSendPort(BASE, "", target_uid=UID)
    with pytest.raises(ValueError):
        WeQWebSendPort(BASE, TOKEN, target_uid=" ")


# ── issue #15：地址与目标校验（凭据发出之前 fail closed） ──────────────


def test_loopback_http_allowed():
    for url in ("http://127.0.0.1:7690", "http://localhost:7690", "http://127.0.0.5:7690"):
        assert WeQWebSendPort(url, TOKEN, target_uid=UID).base_url == url


def test_remote_requires_https():
    with pytest.raises(ValueError, match="HTTPS"):
        WeQWebSendPort("http://weq.example.com", TOKEN, target_uid=UID)
    port = WeQWebSendPort("https://weq.example.com", TOKEN, target_uid=UID)
    assert port.base_url == "https://weq.example.com"



def test_fake_loopback_prefix_is_not_a_loopback_address():
    for url in ("http://127.attacker.example", "http://127.0.0.1.evil.example"):
        with pytest.raises(ValueError, match="HTTPS"):
            WeQWebSendPort(url, TOKEN, target_uid=UID)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:7690?token=secret-value",
    "http://127.0.0.1:7690#secret-value",
])
def test_query_and_fragment_are_rejected_without_echoing_values(url):
    with pytest.raises(ValueError) as exc:
        WeQWebSendPort(url, TOKEN, target_uid=UID)
    assert "secret-value" not in str(exc.value)


def test_bad_scheme_and_embedded_credentials_refused():
    with pytest.raises(ValueError, match="scheme"):
        WeQWebSendPort("ftp://127.0.0.1:7690", TOKEN, target_uid=UID)
    with pytest.raises(ValueError, match="credentials"):
        WeQWebSendPort("http://user:pass@127.0.0.1:7690", TOKEN, target_uid=UID)


def test_allowed_hosts_enforced():
    with pytest.raises(ValueError, match="allowed_hosts"):
        WeQWebSendPort("https://weq.example.com", TOKEN, target_uid=UID,
                       allowed_hosts=("127.0.0.1",))
    assert WeQWebSendPort("https://weq.example.com", TOKEN, target_uid=UID,
                          allowed_hosts=("weq.example.com",))


@pytest.mark.parametrize("bad_uid", ["12345", "u_", "u_" + "x" * 70, "  ", "u_汉字", "U_ABCDEF"])
def test_invalid_target_uid_refused(bad_uid):
    with pytest.raises(ValueError, match="target_uid"):
        WeQWebSendPort(BASE, TOKEN, target_uid=bad_uid)


def test_unsafe_config_rejected_before_any_request():
    opener = FakeOpener([])
    with pytest.raises(ValueError):
        WeQWebSendPort("http://evil.example", TOKEN, target_uid=UID, opener=opener)
    assert opener.requests == []  # 校验先于任何凭据使用


def test_redirects_never_followed():
    from weq_channel.weq_send import _NoRedirect
    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {},
                                    "http://evil.example") is None
    # 3xx 经 opener → login 直接失败，不发生第二个请求（凭据不跟跳转走）
    opener = FakeOpener([FakeResponse(302, raw=b"", set_cookie="")])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    assert port.send(draft()) is False
    assert len(opener.requests) == 1


def test_error_messages_never_contain_token():
    opener = FakeOpener([FakeResponse(500, raw=b"internal error")])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener)
    with pytest.raises(WeQSendError) as exc:
        port.login()
    assert TOKEN not in str(exc.value)
    for bad in ("http://evil.example", "ftp://127.0.0.1"):
        with pytest.raises(ValueError) as exc2:
            WeQWebSendPort(bad, TOKEN, target_uid=UID)
        assert TOKEN not in str(exc2.value)


def test_split_image_parts_markdown():
    from weq_channel.weq_send import _split_image_parts
    parts = _split_image_parts("画好啦 ![图](https://cdn.example.com/a.png) 看看")
    assert parts == [("text", "画好啦"), ("image", "https://cdn.example.com/a.png"),
                     ("text", "看看")]


def test_split_image_parts_bare_url():
    from weq_channel.weq_send import _split_image_parts
    parts = _split_image_parts("图在这 https://cdn.example.com/b.jpg?x=1 完事")
    assert ("image", "https://cdn.example.com/b.png") not in parts
    assert any(k == "image" and "b.jpg" in v for k, v in parts)


def test_split_image_parts_no_image():
    from weq_channel.weq_send import _split_image_parts
    parts = _split_image_parts("纯文本没有图")
    assert parts == [("text", "纯文本没有图")]


def test_mixed_send_sends_image_then_text(monkeypatch):
    """图文混排：图片走 send_image，文字走 _send_text_payload，按序。"""
    from weq_channel.weq_send import WeQWebSendPort
    calls = []
    port = WeQWebSendPort.__new__(WeQWebSendPort)
    monkeypatch.setattr(WeQWebSendPort, "send_image",
                        lambda self, b, **kw: (calls.append(("img", len(b))), True)[1])
    monkeypatch.setattr(WeQWebSendPort, "_send_text_payload",
                        lambda self, t: (calls.append(("text", t)), True)[1])
    monkeypatch.setattr("weq_channel.weq_send._download_image",
                        lambda url: b"png-bytes")
    from whale_core.life.initiative import ProactiveDraft
    draft = ProactiveDraft(draft_id="d1", text="前缀 ![图](https://cdn.example.com/a.png) 后缀",
                           reason="t", evidence=("t",), channel="direct_message")
    assert port.send(draft)
    assert calls == [("text", "前缀"), ("img", 9), ("text", "后缀")]


def test_mixed_send_image_failure_falls_back_to_link(monkeypatch):
    """图片下载失败 → 兜底发文字链接，不丢内容。"""
    from weq_channel.weq_send import WeQWebSendPort
    calls = []
    port = WeQWebSendPort.__new__(WeQWebSendPort)
    monkeypatch.setattr(WeQWebSendPort, "send_image", lambda self, b, **kw: False)
    monkeypatch.setattr(WeQWebSendPort, "_send_text_payload",
                        lambda self, t: (calls.append(t), True)[1])
    monkeypatch.setattr("weq_channel.weq_send._download_image", lambda url: None)
    from whale_core.life.initiative import ProactiveDraft
    draft = ProactiveDraft(draft_id="d2", text="图 ![图](https://cdn.example.com/a.png)",
                           reason="t", evidence=("t",), channel="direct_message")
    assert port.send(draft)
    assert "https://cdn.example.com/a.png" in calls  # 兜底发了链接
