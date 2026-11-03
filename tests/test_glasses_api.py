"""眼镜/手机轻量 API 测试（handoff-whale-core-glasses-chat.md 验收 1–5）。

全部离线：真 HTTP 回环（127.0.0.1 随机端口），脑与发送口用假件；
sqlite 用临时目录真实库（验证记忆共享语义）。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from whale_core.api.glasses import build_delegates, make_handler
from whale_core.memory.sqlite_store import SqliteMemoryStore


class _FakeBrain:
    def __init__(self):
        self.calls = []

    def reply(self, text, *, persona="", memories=(), history=(), images=()):
        self.calls.append({"text": text, "persona": persona,
                           "history": list(history)})
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

