"""refresh_media_rkeys 离线测试：fake opener，不触网、不依赖真实 WeQ/QQ。

覆盖：登录调用与 expected account 绑定字段的组装、result.data.json 布尔
解包（true/false/形状不符）、401 会话过期重登一次、各类失败返回 False
且不污染 last_receipt。

绑定组装规则对应服务端真实 schema（weq-src account.ts:7333，
``z.object({expectedAccountUin?, expectedAccountConfigId?}).strict().optional()``）：
- 两字段各自 optional、服务端逐字段独立校验 → 配了哪个发哪个（半绑定合法）；
- ``object.optional()`` 只认 undefined 不认 null → 无绑定发 ``{"json": {}}``
  （空对象过 schema 且跳过全部账号校验），**绝不发 null**；
- strict() → input 对象不得带任何额外键。

注意：fake opener 只能断言发出的请求形状，不能证明服务端接受；形状正确性
由这些用例对照真实 schema 约束逐条断言，真实联通验证由宿主对本地 WeQ 做。
"""
import json

from weq_channel.weq_send import WeQWebSendPort

BASE = "http://127.0.0.1:7690"
TOKEN = "synthetic-web-token"
UID = "u_X5hSynthetic"
UIN = "123456789"
CONFIG_ID = "cfg-synthetic-1"


class FakeResponse:
    def __init__(self, status=200, body=None, set_cookie=None, raw=None):
        self.status = status
        self._raw = raw if raw is not None else json.dumps(
            body if body is not None else {}).encode()
        self.headers = {"Set-Cookie": set_cookie} if set_cookie else {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """按序回放响应；记录请求供断言。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append({"url": request.full_url, "body": request.data,
                              "headers": dict(request.header_items()),
                              "timeout": timeout})
        item = self.responses.pop(0) if self.responses else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


def login_ok():
    return FakeResponse(200, raw=b"ok", set_cookie="weq_session=abc123; Path=/; HttpOnly")


def refresh_result(value):
    return FakeResponse(200, {"result": {"data": {"json": value}}})


def make_port(opener, *, with_binding=True):
    kwargs = {}
    if with_binding:
        kwargs = {"expected_account_uin": UIN,
                  "expected_account_config_id": CONFIG_ID}
    return WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener, **kwargs)


def test_refresh_success_with_expected_binding():
    opener = FakeOpener([login_ok(), refresh_result(True)])
    port = make_port(opener)
    assert port.refresh_media_rkeys() is True
    assert len(opener.requests) == 2
    login_req, refresh_req = opener.requests
    assert login_req["url"] == BASE + "/_auth/login"
    assert json.loads(login_req["body"]) == {"token": TOKEN}
    assert refresh_req["url"] == BASE + "/trpc/account.refreshRkeys"
    assert refresh_req["headers"]["Cookie"] == "weq_session=abc123"
    inp = json.loads(refresh_req["body"])["json"]
    assert inp == {"expectedAccountUin": UIN,
                   "expectedAccountConfigId": CONFIG_ID}
    assert TOKEN not in json.dumps(refresh_req["headers"])


def test_refresh_without_binding_sends_empty_object_not_null():
    # z.object(...).strict().optional() 只认 undefined 不认 null；
    # {"json": {}} 过 schema 且 requireExpectedAccount(undefined, undefined)
    # 跳过全部校验，与无 input 语义等价。
    opener = FakeOpener([login_ok(), refresh_result(True)])
    port = make_port(opener, with_binding=False)
    assert port.refresh_media_rkeys() is True
    body = json.loads(opener.requests[1]["body"])
    assert body == {"json": {}}
    assert body["json"] is not None


def test_refresh_with_uin_only_binding_sends_uin_only():
    # runner 只配 expected_account_uin：字段各自 optional，必须照发
    # （静默丢弃会让服务端漏掉 uin 校验），不得补 null/空串。
    opener = FakeOpener([login_ok(), refresh_result(True)])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener,
                          expected_account_uin=UIN)
    assert port.refresh_media_rkeys() is True
    assert json.loads(opener.requests[1]["body"]) == {
        "json": {"expectedAccountUin": UIN}}


def test_refresh_with_config_id_only_binding_sends_config_id_only():
    opener = FakeOpener([login_ok(), refresh_result(True)])
    port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener,
                          expected_account_config_id=CONFIG_ID)
    assert port.refresh_media_rkeys() is True
    assert json.loads(opener.requests[1]["body"]) == {
        "json": {"expectedAccountConfigId": CONFIG_ID}}


def test_refresh_input_matches_strict_schema_shape():
    # 对照真实 schema 约束逐条断言（fake 只能断请求形状，服务接受与否
    # 由宿主真实联通验证）：
    # - strict()：input 只允许 expectedAccountUin/expectedAccountConfigId 两键
    # - min(1)：出现的绑定值必须是非空字符串
    # - 顶层 tRPC 信封只有 "json" 一键（不夹带 meta 等多余结构）
    allowed = {"expectedAccountUin", "expectedAccountConfigId"}
    for kwargs in ({}, {"expected_account_uin": UIN},
                   {"expected_account_config_id": CONFIG_ID},
                   {"expected_account_uin": UIN,
                    "expected_account_config_id": CONFIG_ID}):
        opener = FakeOpener([login_ok(), refresh_result(True)])
        port = WeQWebSendPort(BASE, TOKEN, target_uid=UID, opener=opener, **kwargs)
        assert port.refresh_media_rkeys() is True
        body = json.loads(opener.requests[1]["body"])
        assert set(body) == {"json"}
        inp = body["json"]
        assert isinstance(inp, dict)  # 绝不发 null
        assert set(inp) <= allowed
        for value in inp.values():
            assert isinstance(value, str) and len(value) >= 1


def test_refresh_false_result_returns_false():
    opener = FakeOpener([login_ok(), refresh_result(False)])
    port = make_port(opener)
    assert port.refresh_media_rkeys() is False


def test_refresh_non_boolean_result_returns_false():
    for value in ({"ok": True}, "true", 1, None):
        opener = FakeOpener([login_ok(), refresh_result(value)])
        port = make_port(opener)
        assert port.refresh_media_rkeys() is False


def test_refresh_401_relogins_once():
    expired = FakeResponse(401, raw=b"unauthorized")
    opener = FakeOpener([login_ok(), expired, login_ok(), refresh_result(True)])
    port = make_port(opener)
    assert port.refresh_media_rkeys() is True
    assert len(opener.requests) == 4  # login, 401, relogin, retry


def test_refresh_401_after_relogin_returns_false():
    expired = FakeResponse(401, raw=b"unauthorized")
    opener = FakeOpener([login_ok(), expired, login_ok(), expired])
    port = make_port(opener)
    assert port.refresh_media_rkeys() is False


def test_refresh_login_failure_returns_false():
    opener = FakeOpener([FakeResponse(401, raw=b"unauthorized")])
    port = make_port(opener)
    assert port.refresh_media_rkeys() is False


def test_refresh_unreachable_returns_false():
    import urllib.error
    opener = FakeOpener([urllib.error.URLError("refused")])
    port = make_port(opener)
    assert port.refresh_media_rkeys() is False


def test_refresh_http_500_returns_false():
    opener = FakeOpener([login_ok(), FakeResponse(500, raw=b"boom")])
    port = make_port(opener)
    assert port.refresh_media_rkeys() is False


def test_refresh_failure_does_not_touch_last_receipt():
    # 先有一次成功发送的回执，再刷新失败：last_receipt 必须原样保留
    send_body = {"result": {"data": {"json": {
        "ok": True, "peerType": "c2c", "targetId": UID, "privateSequence": 7}}}}
    opener = FakeOpener([login_ok(), FakeResponse(200, send_body),
                         FakeResponse(500, raw=b"boom")])
    port = make_port(opener)
    assert port.send_image(b"\x89PNG fake") is True
    receipt_before = dict(port.last_receipt)
    assert port.refresh_media_rkeys() is False
    assert port.last_receipt == receipt_before


def test_refresh_success_does_not_touch_last_receipt():
    opener = FakeOpener([login_ok(), refresh_result(True)])
    port = make_port(opener)
    assert port.last_receipt is None
    assert port.refresh_media_rkeys() is True
    assert port.last_receipt is None


# ── WindowedSendPort.refresh_media_rkeys（stub window，无 HTTP）────────
# 2026-11-11 父实测：关窗态 refreshRkeys HTTP 200 但回 False（rkey 收割要
# attach 在线），故包装层须经既有 _through_window 临时开窗刷新，不开新
# fullmode 策略、不发消息。以下全程 mock，不开真实窗口、不调真实刷新。

import time
from pathlib import Path

from weq_channel.full_mode_window import FullModeWindowError, WindowedSendPort


class StubWindow:
    """无 HTTP 的内存窗口（形态同 test_full_mode_window 的 StubWindow）。"""

    def __init__(self, state_file, *, allowed=False, open_raises=None):
        self.state_file = Path(state_file)
        self._allowed = allowed
        self.open_calls = 0
        self.touch_calls = 0
        self.open_raises = open_raises

    def is_ready(self):
        return self._allowed

    def open(self):
        self.open_calls += 1
        if self.open_raises:
            raise self.open_raises
        self._allowed = True
        self.touch()

    def touch(self):
        self.touch_calls += 1
        self.state_file.write_text(str(int(time.time())))


class RefreshInner:
    """带 refresh_media_rkeys 的内层端口 stub；send 被调用即炸（纪律断言）。"""

    def __init__(self, result=True):
        self.result = result
        self.refresh_calls = 0

    def refresh_media_rkeys(self):
        self.refresh_calls += 1
        return self.result

    def send(self, draft):  # pragma: no cover - 被调即失败
        raise AssertionError("refresh 路径不得触发任何发送")


def test_windowed_refresh_no_inner_method_false_no_window(tmp_path):
    win = StubWindow(tmp_path / "w.ts")

    class BareInner:
        pass

    port = WindowedSendPort(BareInner(), win)
    assert port.refresh_media_rkeys() is False
    assert win.open_calls == 0 and win.touch_calls == 0


def test_windowed_refresh_opens_window_and_touches(tmp_path):
    win = StubWindow(tmp_path / "w.ts")          # 初始关窗
    inner = RefreshInner(True)
    port = WindowedSendPort(inner, win)
    assert port.refresh_media_rkeys() is True
    assert win.open_calls == 1                   # 关窗态先开窗
    assert inner.refresh_calls == 1
    assert win.touch_calls >= 2                  # open 落档 + 刷新后续期
    assert port.opens == 1


def test_windowed_refresh_when_open_does_not_reopen(tmp_path):
    win = StubWindow(tmp_path / "w.ts", allowed=True)
    inner = RefreshInner(True)
    port = WindowedSendPort(inner, win)
    assert port.refresh_media_rkeys() is True
    assert win.open_calls == 0 and port.opens == 0
    assert win.touch_calls == 1                  # 只续期


def test_windowed_refresh_inner_false_still_touches(tmp_path):
    win = StubWindow(tmp_path / "w.ts")
    inner = RefreshInner(False)
    port = WindowedSendPort(inner, win)
    assert port.refresh_media_rkeys() is False   # 内层 False 原样透传
    assert inner.refresh_calls == 1
    assert win.touch_calls >= 2                  # 失败的刷新也算窗口活跃


def test_windowed_refresh_open_failure_still_attempts(tmp_path):
    # 开窗失败不阻断（红线 1 同发送语义）：内层照常尝试，如实返回，仍 touch
    win = StubWindow(tmp_path / "w.ts",
                     open_raises=FullModeWindowError("weq web unreachable"))
    inner = RefreshInner(False)
    port = WindowedSendPort(inner, win)
    assert port.refresh_media_rkeys() is False
    assert inner.refresh_calls == 1
    assert port.open_errors == 1
    assert win.state_file.exists()               # touch 落档供 closer 纠正半开态
