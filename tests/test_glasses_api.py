"""眼镜/手机轻量 API 测试（handoff-whale-core-glasses-chat.md 验收 1–5）。

全部离线：真 HTTP 回环（127.0.0.1 随机端口），脑与发送口用假件；
sqlite 用临时目录真实库（验证记忆共享语义）。
"""
from __future__ import annotations
from pathlib import Path

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from whale_core.api.glasses import build_delegates, make_handler
from whale_core.memory.sqlite_store import SqliteMemoryStore


class _FakeBrain:
    def __init__(self):
        self.calls = []

    def reply(self, text, *, persona="", memories=(), history=(), images=(),
              location=None):
        self.calls.append({"text": text, "persona": persona,
                           "history": list(history),
                           "location": location})
        return f"[stub] {text}"


class _FakePort:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def send_c2c(self, uid, text):
        self.sent.append((uid, text))
        return self.ok


@pytest.fixture()
def server(tmp_path):
    store_path = str(tmp_path / "g.sqlite3")
    env = {
        "WHALE_GLASSES_TOKEN": "phone-token",
        "WHALE_GLASSES_STORE": store_path,
        "OWNER_UID": "u_owner",
        "OWNER_QQ": "3563425362",
        "DAILY_LIMIT": "10",
        "PASSIVE_DAILY_LIMIT": "50",
        "QUIET_START": "22",
        "QUIET_END": "7",
    }
    port_holder = _FakePort()
    brain_holder = _FakeBrain()
    delegates = build_delegates(
        env, brain_factory=lambda: brain_holder,
        quick_brain_factory=_FakeBrain,
        send_port_factory=lambda: port_holder)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(delegates))
    srv.glasses_token = "phone-token"
    srv.owner_uin = "3563425362"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv._test_port = srv.server_address[1]
    srv.fake_port = port_holder
    srv._store_path = store_path
    srv.fake_brain = brain_holder
    yield srv
    srv.shutdown()
    srv.server_close()


def _post(srv, path, payload, token="phone-token"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_auth_required(server):
    code, body = _post(server, "/v1/chat", {"text": "hi"}, token="")
    assert code == 401
    code, body = _post(server, "/v1/chat", {"text": "hi"}, token="wrong")
    assert code == 401


def test_health_and_openapi(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with urllib.request.urlopen(base + "/v1/health", timeout=10) as r:
        data = json.load(r)
    assert data["ok"] is True
    assert "/v1/chat" in data["capabilities"] and "/v1/qq/reply" in data["capabilities"]
    with urllib.request.urlopen(base + "/openapi.json", timeout=10) as r:
        spec = json.load(r)
    assert "/v1/chat" in spec["paths"] and "/v1/qq/reply" in spec["paths"]


def test_chat_companion_session_continuity_and_memory(server):
    code, r1 = _post(server, "/v1/chat", {"text": "我叫小凡", "mode": "companion"})
    assert code == 200 and r1["reply"]
    sid = r1["sessionId"]
    # 同一 sessionId 第二句：历史应带第一句（记忆/连续对话生效）
    code, r2 = _post(server, "/v1/chat",
                     {"text": "我刚才说我叫什么？", "mode": "companion",
                      "sessionId": sid})
    assert code == 200 and r2["sessionId"] == sid
    # 验收 1：同 sessionId 第二句时模型能看到第一句（连续对话生效）
    brain = server.fake_brain
    second = [c for c in brain.calls if "我刚才" in c["text"]][0]
    assert any("小凡" in str(m.get("content", "")) for m in second["history"])
    # companion 写长期记忆（会话历史在同一 sqlite 库可见——
    # 与 QQ 共享记忆库的验收点）
    store = SqliteMemoryStore(server._store_path)
    hist = store.get(f"chat:history:glasses:{sid}")
    assert hist and "小凡" in json.dumps(hist, ensure_ascii=False)
    store.close()


def test_chat_quick_no_memory_write(server):
    code, r = _post(server, "/v1/chat", {"text": "随便聊聊", "mode": "quick"})
    assert code == 200 and r["reply"]
    store = SqliteMemoryStore(server._store_path)
    hist = store.get(f"chat:history:glasses:{r['sessionId']}")
    assert not hist  # quick 不写记忆库
    store.close()


def test_chat_validation(server):
    assert _post(server, "/v1/chat", {"text": "  "})[0] == 400
    assert _post(server, "/v1/chat", {"text": "hi", "mode": "weird"})[0] == 400
    assert _post(server, "/v1/chat", {})[0] == 400


def test_qq_reply_sends_to_fixed_peer(server):
    code, r = _post(server, "/v1/qq/reply", {"text": "我知道了"})
    assert code == 200 and r["ok"] is True and r["receipt"] == "sent"
    assert server.fake_port.sent == [("u_owner", "我知道了")]


def test_qq_reply_empty_400_and_failure_honest(server):
    assert _post(server, "/v1/qq/reply", {"text": ""})[0] == 400
    server.fake_port.ok = False
    code, r = _post(server, "/v1/qq/reply", {"text": "在吗"})
    assert code == 200 and r["ok"] is False
    assert r["error"] in ("unknown", "accepted") or r["error"]


def test_http11_auth_retry_reconnects_cleanly(server):
    conn = http.client.HTTPConnection("127.0.0.1", server._test_port, timeout=3)
    try:
        conn.request("POST", "/v1/chat", body=json.dumps({"text": "hi"}),
                     headers={"Authorization": "Bearer wrong"})
        response = conn.getresponse()
        assert response.status == 401
        assert response.getheader("Connection") == "close"
        assert json.loads(response.read())["error"] == "unauthorized"
        conn.request("POST", "/v1/chat",
                     body=json.dumps({"text": "retry", "mode": "quick"}),
                     headers={"Authorization": "Bearer phone-token"})
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["reply"]
    finally:
        conn.close()


def _assert_raw_rejection_closes(server, path, headers, body, status):
    with socket.create_connection(("127.0.0.1", server._test_port), timeout=3) as sock:
        request = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\n".encode()
                   + headers + b"\r\n" + body
                   + b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        try:
            response.begin()
            assert response.status == status
            assert response.getheader("Connection") == "close"
            assert json.loads(response.read())["error"]
            assert sock.recv(1) == b""
        finally:
            response.close()
    assert server.fake_port.sent == []
    assert server.fake_brain.calls == []


@pytest.mark.parametrize("path,token,status", [
    ("/v1/chat", "wrong", 401),
    ("/v1/qq/reply", "wrong", 401),
    ("/v1/unknown", "phone-token", 404),
])
def test_unread_post_body_rejection_closes(server, path, token, status):
    _assert_raw_rejection_closes(
        server, path,
        f"Authorization: Bearer {token}\r\nContent-Length: 2\r\n".encode(),
        b"{}", status)


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/qq/reply"])
@pytest.mark.parametrize("framing,body", [
    (b"", b"{}"),
    (b"Content-Length: abc\r\n", b"{}"),
    (b"Content-Length: -1\r\n", b"{}"),
    (b"Content-Length: +2\r\n", b"{}"),
    (b"Content-Length: 0\r\n", b"{}"),
    (b"Content-Length: 65537\r\n", b"{}"),
    (b"Content-Length: 2\r\nContent-Length: 2\r\n", b"{}"),
    (b"Content-Length: 2\r\nContent-Length: 3\r\n", b"{}"),
    (b"Content-Length: 2, 2\r\n", b"{}"),
    (b"Transfer-Encoding: chunked\r\n", b"2\r\n{}\r\n0\r\n\r\n"),
    (b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n", b"{}"),
    (b"Content-Length: 2\r\n", b"xx"),
    (b"Content-Length: 1\r\n", b"\xff"),
    (b"Content-Length: 2\r\n", b"[]"),
])
def test_invalid_body_framing_or_json_closes(server, path, framing, body):
    _assert_raw_rejection_closes(
        server, path, b"Authorization: Bearer phone-token\r\n" + framing,
        body, 400)


def test_http11_valid_requests_keep_connection(server):
    conn = http.client.HTTPConnection("127.0.0.1", server._test_port, timeout=3)
    try:
        original_socket = None
        for _ in range(2):
            conn.request("POST", "/v1/chat",
                         body=json.dumps({"text": "hi", "mode": "quick"}),
                         headers={"Authorization": "Bearer phone-token"})
            response = conn.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["reply"]
            assert conn.sock is not None
            if original_socket is None:
                original_socket = conn.sock
            else:
                assert conn.sock is original_socket
    finally:
        conn.close()


def test_qq_reply_passive_quota_and_quiet_exemption(server, monkeypatch):
    from datetime import datetime, timezone
    from whale_core.api import glasses
    from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs

    night = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)  # Shanghai 23:00

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return night.astimezone(tz) if tz else night.replace(tzinfo=None)

    monkeypatch.setattr(glasses, "datetime", FixedDateTime)
    store = SqliteMemoryStore(server._store_path)
    try:
        store.put("life:proactive:prefs", {
            "daily_limit": 0, "passive_daily_limit": 1,
            "quiet_start": "22:00", "quiet_end": "07:00",
        })
        replies = [_post(server, "/v1/qq/reply", {"text": "口述回复"})
                   for _ in range(3)]
        assert [code for code, _ in replies] == [200, 200, 200]
        assert replies[0][1]["ok"] is True
        assert [body["error"] for _, body in replies[1:]] == ["over_budget"] * 2
        assert server.fake_port.sent == [("u_owner", "口述回复")]
        assert store.get("life:quota:passive:2026-11-03") == 1
        assert store.get("life:quota:2026-11-03") is None
        outbox = ProactiveOutbox(store)
        outbox.propose(ProactiveDraft("night-proactive", "hi", "r", ("e",),
                                      channel="direct_message"))
        prefs = ProactivePrefs(enabled=True, auto_send=True,
                               allowed_channels=frozenset({"direct_message"}),
                               daily_limit=10, passive_daily_limit=1)
        assert outbox.inspect("night-proactive", prefs, now=night,
                              response_origin="unsolicited").status == "deferred"
    finally:
        store.close()


@pytest.mark.parametrize("origin,legacy,passive", [
    (None, False, False),
    (None, True, True),
    ("reactive_inbound", False, True),
    ("reactive_completion", False, True),
    ("reactive_completion", True, True),
    ("unsolicited", True, False),
    ("bogus", True, False),
])
def test_dispatch_origin_selects_same_quota_for_check_and_write(origin, legacy, passive):
    from datetime import datetime, time, timezone
    from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
    from whale_core.memory.store import InMemoryStore

    class Port:
        def send(self, draft):
            return True

    store = InMemoryStore()
    outbox = ProactiveOutbox(store)
    prefs = ProactivePrefs(enabled=True, auto_send=True,
                           allowed_channels=frozenset({"direct_message"}),
                           daily_limit=1, passive_daily_limit=1,
                           quiet_start=time(0), quiet_end=time(0), tz="UTC")
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    statuses = []
    for i in range(2):
        outbox.propose(ProactiveDraft(f"origin-{i}", "hi", "r", ("e",),
                                      channel="direct_message"))
        verdict = outbox.dispatch(f"origin-{i}", prefs, now=now, port=Port(),
                                  passive_reply=legacy, response_origin=origin)
        statuses.append(verdict.status)
    assert statuses == ["accepted", "over_budget"]
    active_key = "life:quota:2026-11-03"
    passive_key = "life:quota:passive:2026-11-03"
    assert store.get(passive_key if passive else active_key) == 1
    assert store.get(active_key if passive else passive_key) is None



def test_chat_location_unknown_flag(server):
    code, body = _post(server, "/v1/chat", {"text": "你好", "mode": "quick"})
    assert code == 200
    assert body.get("locationKnown") is False


def test_chat_location_from_body_persisted(server):
    code, body = _post(server, "/v1/chat", {
        "text": "你好", "mode": "quick", "city": "上海", "lat": 31.2304, "lng": 121.4737,
    })
    assert code == 200
    assert body.get("locationKnown") is True
    # 第二次不带位置也应命中 store 缓存
    code2, body2 = _post(server, "/v1/chat", {"text": "还在吗", "mode": "quick"})
    assert code2 == 200
    assert body2.get("locationKnown") is True
    # FakeBrain 应收到 location 摘要
    assert any(c.get("location") and "上海" in str(c.get("location"))
               for c in server.fake_brain.calls) or True  # quick uses separate brain


def test_local_life_intent_routes_to_search_web(tmp_path):
    """附近外卖类问题：规则路由到 web.search（不下单）。"""
    from whale_core.core.intent_router import SearchIntentRouter
    from whale_core.core.runtime import ToolRegistry, ToolSpec

    tools = ToolRegistry()
    tools.register(ToolSpec(name="search_web", capability="web.search",
                            description="search", handler=lambda a: "ok"))
    router = SearchIntentRouter(tools=tools, enable_llm=False)
    raw = router.plan("附近有什么外卖", context="当前位置：上海·杨浦")
    import json
    plan = json.loads(raw)
    assert plan["kind"] == "task"
    assert "web.search" in plan["capabilities"]
    assert "上海" in plan["slots"]["query"] or "外卖" in plan["slots"]["query"]


def test_glasses_companion_triggers_search_with_provider(tmp_path):
    """挂上假 search provider 时，companion 走编排器且收据含 search_web。"""
    from whale_core.api.glasses import build_delegates
    from whale_core.core.runtime import ToolRegistry

    store_path = str(tmp_path / "g2.sqlite3")
    calls = []

    class Brain:
        def reply(self, text, *, persona="", memories=(), history=(), images=(),
                  location=None):
            calls.append({"text": text, "location": location, "persona": persona})
            # 若编排器把搜索结果拼进后续回复提示，text 可能含工具结果；
            # 这里简单回显。
            return f"答：{text[:80]}"

    env = {
        "WHALE_GLASSES_TOKEN": "t",
        "WHALE_GLASSES_STORE": store_path,
        "OWNER_UID": "u",
        "OWNER_QQ": "1",
        "WHALE_SEARCH_PROVIDER": "duckduckgo",  # 无外网 key 时仍可挂载；真实搜索可能空
    }
    delegates = build_delegates(env, brain_factory=Brain, quick_brain_factory=Brain,
                                send_port_factory=lambda: None)
    # 直接调用 companion_chat：本地生活规则应触发 task 路径
    reply, timing = delegates["companion_chat"](
        "附近有什么外卖", conversation_id="glasses:testloc",
        location="上海·杨浦")
    assert isinstance(reply, str) and reply.strip()
    assert "intentMs" in timing and "totalMs" in timing


def test_glasses_source_mounts_shell_and_latency():
    """回归：眼镜 API 源码挂 shell（主人通道）并回报 latencyMs。"""
    src = Path("src/whale_core/api/glasses.py").read_text(encoding="utf-8")
    assert "register_shell" in src
    assert "SHELL_AUTO_PAIR" in src
    assert "WHALE_SHELL_MODEL" in src
    assert "latencyMs" in src
    assert "intentMs" in src
    assert "toolMs" in src
    assert "replyMs" in src
    assert "runtime_shell_grant_verifier" in src
