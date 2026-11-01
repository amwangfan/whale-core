"""FullModeWindow / WindowedSendPort 离线测试：注入 fake opener / stub window，不触网。

覆盖任务书 §3 矩阵：开窗序列、401 重登、超时、幂等关窗、touch/续期、send 透传
（True/False）、开窗失败不阻断、closer 假时钟/跨进程 state_file、env 门禁、
token 卫生（日志/异常/回执/state_file 一律不含 token）。
"""
import json
import sys
import time
from pathlib import Path

import pytest

from weq_channel.full_mode_window import (
    FullModeWindow,
    FullModeWindowError,
    WindowedSendPort,
)
from whale_core.life.initiative import ProactiveDraft

BASE = "http://127.0.0.1:7690"
TOKEN = "synthetic-window-token"


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
    """按队列回放响应；记录请求供断言。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append({"url": request.full_url, "body": request.data,
                              "headers": dict(request.header_items()), "method": request.method})
        item = self.responses.pop(0) if self.responses else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


def login_ok():
    return FakeResponse(200, raw=b"ok", set_cookie="weq_session=wxyz; Path=/; HttpOnly")


def state(allowed, ready=None):
    """getFullModeState 假响应；ready=None 时响应里不含 ready 键（旧 weq-web 兼容形态）。"""
    payload = {"allowed": allowed}
    if ready is not None:
        payload["ready"] = ready
    return FakeResponse(200, {"result": {"data": {"json": payload}}})


def set_ok():
    return FakeResponse(200, {"result": {"data": {"json": True}}})


def make_window(opener, state_file, **kw):
    kw.setdefault("ready_timeout_s", 5.0)
    kw.setdefault("grace_s", 0.0)
    kw.setdefault("poll_interval_s", 0.01)
    return FullModeWindow(BASE, TOKEN, state_file=state_file, opener=opener, **kw)


def draft():
    return ProactiveDraft("d1", "测试回复", "QQ 回复", ("qq:m1",), channel="direct_message")


class StubInner:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def send(self, d):
        self.calls.append(d)
        return self.result


class StubWindow:
    """端口级测试用：无 HTTP 的内存窗口。"""

    def __init__(self, state_file, *, allowed=False, open_raises=None):
        self.state_file = Path(state_file)
        self._allowed = allowed
        self.open_calls = 0
        self.close_calls = 0
        self.touch_calls = 0
        self.open_raises = open_raises

    def is_allowed(self):
        return self._allowed

    def is_ready(self):
        return self._allowed

    def open(self):
        self.open_calls += 1
        if self.open_raises:
            raise self.open_raises
        self._allowed = True
        self.touch()

    def close(self):
        self.close_calls += 1
        self._allowed = False
        self.state_file.unlink(missing_ok=True)

    def touch(self):
        self.touch_calls += 1
        self.state_file.write_text(str(int(time.time())))

    def last_send_epoch(self):
        try:
            return float(self.state_file.read_text().strip())
        except (OSError, ValueError):
            return 0.0


# ── FullModeWindow（fake opener）─────────────────────────────────

def test_open_sequence_login_set_poll_touch(tmp_path):
    sf = tmp_path / "w.ts"
    opener = FakeOpener([login_ok(), set_ok(), state(False), state(True)])
    w = make_window(opener, sf)
    w.open()
    urls = [r["url"] for r in opener.requests]
    assert urls == [BASE + "/_auth/login",
                    BASE + "/trpc/bootstrap.setAutoAttachQq",
                    BASE + "/trpc/bootstrap.getFullModeState",
                    BASE + "/trpc/bootstrap.getFullModeState"]
    assert json.loads(opener.requests[1]["body"]) == {"json": {"enabled": True}}
    assert opener.requests[2]["method"] == "GET" and opener.requests[2]["body"] is None
    assert opener.requests[1]["headers"]["Cookie"] == "weq_session=wxyz"
    assert sf.exists() and sf.read_text().strip().isdigit()
    assert oct(sf.stat().st_mode & 0o777) == "0o600"  # state_file 0600


def test_open_relogin_once_on_401(tmp_path):
    sf = tmp_path / "w.ts"
    opener = FakeOpener([
        login_ok(), FakeResponse(401, "unauthorized"),  # set → 401
        login_ok(), set_ok(), state(True),              # 重登后成功
    ])
    make_window(opener, sf).open()
    logins = [r for r in opener.requests if r["url"].endswith("/_auth/login")]
    assert len(logins) == 2
    assert sf.exists()


def test_open_timeout_raises_and_writes_nothing(tmp_path):
    sf = tmp_path / "w.ts"
    opener = FakeOpener([login_ok(), set_ok()] + [state(False)] * 50)
    w = make_window(opener, sf, ready_timeout_s=0.05)
    with pytest.raises(FullModeWindowError):
        w.open()
    assert not sf.exists()


def test_open_waits_for_ready_not_just_allowed(tmp_path):
    """allowed=true 但 ready=false（SSO 未登记）时 open() 不通过——钉死 2026-11-01 丢发事故形态。"""
    sf = tmp_path / "w.ts"
    opener = FakeOpener([login_ok(), set_ok()] + [state(True, ready=False)] * 200)
    w = make_window(opener, sf, ready_timeout_s=0.05)
    with pytest.raises(FullModeWindowError):
        w.open()
    assert not sf.exists()


def test_open_compat_when_ready_field_missing(tmp_path):
    """旧 weq-web 响应没有 ready 键：回落 allowed 语义，照常开窗。"""
    sf = tmp_path / "w.ts"
    opener = FakeOpener([login_ok(), set_ok(), state(True)])
    make_window(opener, sf).open()
    assert sf.exists()


def test_close_disables_and_clears_state_idempotent(tmp_path):
    sf = tmp_path / "w.ts"
    sf.write_text("123")
    opener = FakeOpener([login_ok(), set_ok(), set_ok()])
    w = make_window(opener, sf)
    w.close()
    assert json.loads(opener.requests[1]["body"]) == {"json": {"enabled": False}}
    assert not sf.exists()
    w.close()  # 幂等：重复调用不抛


def test_touch_and_last_send_epoch(tmp_path):
    sf = tmp_path / "w.ts"
    opener = FakeOpener([])
    w = make_window(opener, sf)
    assert w.last_send_epoch() == 0.0          # 不存在 → 0
    w.touch()
    assert w.last_send_epoch() > 0
    sf.write_text("not-a-number")
    assert w.last_send_epoch() == 0.0          # 损坏 → 0 不抛
    assert not opener.requests                 # touch/读档全程零 HTTP


def test_unreachable_raises_window_error_without_token(tmp_path):
    import urllib.error
    opener = FakeOpener([urllib.error.URLError("connection refused")])
    w = make_window(opener, tmp_path / "w.ts")
    with pytest.raises(FullModeWindowError) as ei:
        w.open()
    assert TOKEN not in str(ei.value)


def test_login_failure_text_has_no_token(tmp_path):
    opener = FakeOpener([FakeResponse(500, {"error": "bad credentials echo " + TOKEN})])
    w = make_window(opener, tmp_path / "w.ts")
    with pytest.raises(FullModeWindowError) as ei:
        w.open()
    assert TOKEN not in str(ei.value)


# ── WindowedSendPort（stub window + stub inner）─────────────────

def test_send_closed_opens_then_delegates_and_touches(tmp_path):
    win = StubWindow(tmp_path / "w.ts")
    inner = StubInner(True)
    port = WindowedSendPort(inner, win)
    assert port.send(draft()) is True
    assert win.open_calls == 1 and len(inner.calls) == 1
    assert win.touch_calls >= 2            # open 落档 + send 后续期
    assert port.opens == 1 and port.sends == 1


def test_send_when_open_does_not_reopen(tmp_path):
    win = StubWindow(tmp_path / "w.ts", allowed=True)
    inner = StubInner()
    port = WindowedSendPort(inner, win)
    port.send(draft())
    assert win.open_calls == 0 and port.opens == 0
    assert win.touch_calls == 1            # 只续期


def test_send_passthrough_false_still_touches(tmp_path):
    win = StubWindow(tmp_path / "w.ts")
    inner = StubInner(False)
    port = WindowedSendPort(inner, win)
    assert port.send(draft()) is False     # 内层 False 原样透传，不重试
    assert len(inner.calls) == 1
    assert win.touch_calls >= 2            # 失败的发送也算窗口活跃


def test_open_failure_does_not_block_send(tmp_path):
    win = StubWindow(tmp_path / "w.ts",
                     open_raises=FullModeWindowError("weq web unreachable"))
    inner = StubInner(False)               # 窗口没开，内层如实 False
    port = WindowedSendPort(inner, win)
    assert port.send(draft()) is False
    assert len(inner.calls) == 1           # 仍委托内层（不阻断）
    assert port.open_errors == 1
    assert win.state_file.exists()         # touch 仍落档（closer 超时纠正半开态）


def test_closer_closes_after_idle_and_clears_state(tmp_path):
    sf = tmp_path / "w.ts"
    win = StubWindow(sf, allowed=True)
    port = WindowedSendPort(StubInner(), win, idle_min=10)
    sf.write_text(str(int(time.time() - 700)))   # 10min 前（>idle）
    assert port._closer_step(time.time()) is True
    assert win.close_calls == 1 and not sf.exists()
    assert port.closes == 1


def test_closer_keeps_fresh_window(tmp_path):
    sf = tmp_path / "w.ts"
    win = StubWindow(sf, allowed=True)
    port = WindowedSendPort(StubInner(), win, idle_min=10)
    win.touch()                                 # 刚刚活跃
    assert port._closer_step(time.time()) is False
    assert win.close_calls == 0 and sf.exists()


def test_closer_cross_process_touch_keeps_window(tmp_path):
    """approve 子进程 touch 的新时间戳，runner 的 closer 必须看得见。"""
    sf = tmp_path / "w.ts"
    win = StubWindow(sf, allowed=True)
    port = WindowedSendPort(StubInner(), win, idle_min=10)
    now = time.time()
    sf.write_text(str(int(now - 700)))                       # 看似过期
    assert now - port.window.last_send_epoch() > 600
    sf.write_text(str(int(now)))                             # 外部进程刚 touch
    assert port._closer_step(now + 1) is False               # 不关
    assert win.close_calls == 0 and sf.exists()


def test_daily_open_warn_threshold(capsys, tmp_path):
    win = StubWindow(tmp_path / "w.ts")
    port = WindowedSendPort(StubInner(), win)
    for _ in range(41):
        port._open_window()
    out = capsys.readouterr().out
    assert "今日第41次" in out
    assert "超过 40 次" in out and "QQ_FULLMODE_IDLE_MIN" in out


# ── runner 接线（env 门禁）───────────────────────────────────────

def _load_example(monkeypatch):
    """按路径导入 examples/qq_live_workflow.py（import 不触发 main）。"""
    examples = str(Path(__file__).resolve().parents[1] / "examples")
    if examples not in sys.path:
        sys.path.insert(0, examples)
    import qq_live_workflow
    return qq_live_workflow


def test_build_send_port_bare_when_ondemand_off(monkeypatch):
    mod = _load_example(monkeypatch)
    monkeypatch.setenv("WEQ_WEB_URL", BASE)
    monkeypatch.setenv("WEQ_WEB_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_UID", "u_X5hSynthetic")
    monkeypatch.delenv("QQ_FULLMODE_ONDEMAND", raising=False)
    port = mod.build_send_port(live_ok=True)
    assert not isinstance(port, WindowedSendPort)


def test_build_send_port_wrapped_when_ondemand_on(monkeypatch, tmp_path):
    mod = _load_example(monkeypatch)
    monkeypatch.setenv("WEQ_WEB_URL", BASE)
    monkeypatch.setenv("WEQ_WEB_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_UID", "u_X5hSynthetic")
    monkeypatch.setenv("QQ_FULLMODE_ONDEMAND", "1")
    monkeypatch.setenv("QQ_FULLMODE_IDLE_MIN", "10")
    port = mod.build_send_port(live_ok=True)
    assert isinstance(port, WindowedSendPort)
    assert port.idle_min == 10.0
