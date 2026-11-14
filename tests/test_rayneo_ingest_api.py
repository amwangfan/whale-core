"""/v1/rayneo/* HTTP 接口离线测试。"""
from __future__ import annotations

import json
from http.client import HTTPConnection
import threading
import urllib.error
import urllib.request

import pytest

from whale_core.api.glasses import build_delegates, make_handler
from whale_core.memory.continuity import remember_episode
from whale_core.memory.rayneo_context import _sha256_text
from whale_core.memory.spaces import OWNER_WORKING_SPACE_ID, ensure_owner_working_copy
from whale_core.memory.sqlite_store import SqliteMemoryStore


class _FakeBrain:
    def reply(self, text, *, persona="", memories=(), history=(), images=(),
              location=None):
        return f"[stub] {text}"


class _FakePort:
    def send_c2c(self, uid, text):
        return True


@pytest.fixture()
def server(tmp_path):
    env = {
        "WHALE_GLASSES_TOKEN": "phone-token",
        "WHALE_GLASSES_STORE": str(tmp_path / "g.sqlite3"),
        "OWNER_UID": "u_owner",
        "OWNER_QQ": "3563425362",
        "DAILY_LIMIT": "10",
        "PASSIVE_DAILY_LIMIT": "50",
        "QUIET_START": "22",
        "QUIET_END": "7",
    }
    delegates = build_delegates(
        env, brain_factory=_FakeBrain, quick_brain_factory=_FakeBrain,
        send_port_factory=_FakePort)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(delegates))
    srv.glasses_token = "phone-token"
    srv.owner_uin = "3563425362"
    srv.glasses_env = env
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv._delegates = delegates
    yield srv
    srv.shutdown()
    srv.server_close()
    rs = delegates.get("rayneo_store")
    if rs is not None:
        rs.close()


def _post(srv, path, payload, token="phone-token", raw=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.server_address[1]}{path}",
        data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _item(seq, text, **kw):
    return {
        "client_seq": seq,
        "dedup_key": kw.get("dedup_key", f"rayneo:dev:{seq}"),
        "kind": kw.get("kind", "transcript"),
        "origin": kw.get("origin", "live"),
        "revision": kw.get("revision", 1),
        "segment_id": kw.get("segment_id", f"s-{seq}"),
        "text": text,
        "content_hash": _sha256_text(text),
        "received_at": kw.get("received_at", "2026-11-10T02:15:00.000Z"),
        "source_time": kw.get("source_time", "2026-11-10T02:15:00.000Z"),
    }


def _batch(*items):
    return {"contract_version": "rayneo-context/v1", "source": "rayneo",
            "source_instance": "rayneo-android-3f2cxxxx",
            "batch_id": "b-1", "items": list(items)}


def test_auth_and_framing(server):
    code, body = _post(server, "/v1/rayneo/ingest", _batch(_item(1, "hi")), token="")
    assert code == 401 and body["error"] == "unauthorized"
    code, body = _post(server, "/v1/rayneo/ingest", _batch(_item(1, "hi")), token="wrong")
    assert code == 401
    # 缺 Content-Length：urllib 会自动加；改用空 items
    code, body = _post(server, "/v1/rayneo/ingest",
                       {"source": "rayneo", "items": []})
    assert code == 400
    code, body = _post(server, "/v1/rayneo/ingest",
                       {"source": "other", "items": [_item(1, "x")]})
    assert code == 400
    # >320 KiB
    huge = _batch(_item(1, "hi"))
    raw = json.dumps(huge).encode() + b" " * (320 * 1024 + 10)
    # Check rejection before uploading the oversized body. Eager upload can race
    # server close and produce TCP reset instead of delivering the 413 on Windows.
    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.putrequest("POST", "/v1/rayneo/ingest")
        conn.putheader("Authorization", "Bearer phone-token")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(raw)))
        conn.endheaders()
        response = conn.getresponse()
        code, body = response.status, json.loads(response.read())
    finally:
        conn.close()
    assert code == 413 and "too large" in body["error"]


def test_ingest_replay_and_query_envelope(server):
    batch = _batch(_item(1, "今天下午和同事讨论接口方案"))
    code, body = _post(server, "/v1/rayneo/ingest", batch)
    assert code == 200 and body["ok"] is True
    assert body["results"][0]["effect"] == "ingested"
    code, body2 = _post(server, "/v1/rayneo/ingest", batch)
    assert body2["results"][0]["effect"] == "replay_idempotent"
    code, q = _post(server, "/v1/rayneo/query",
                    {"source": "rayneo", "limit": 10, "include_derived": True})
    assert code == 200
    assert q["ok"] is True and q["source"] == "rayneo"
    assert q["instruction_eligible"] is False and q["query_disabled"] is False
    assert any("讨论接口" in (it.get("text") or "") for it in q["items"])


def test_delete_range_removes_derived(server):
    mem = server._delegates["store"]
    ensure_owner_working_copy(mem)
    remember_episode(
        mem, summary="派生总结", key="ep:rayneo:1:2",
        tags=("rayneo", "ambient", "summary"),
        extra={"derived": "derived-from-transcript", "segment_ids": ["s-1"]},
        space_id=OWNER_WORKING_SPACE_ID)
    _post(server, "/v1/rayneo/ingest", _batch(_item(1, "将被删除的原文")))
    code, body = _post(server, "/v1/rayneo/control", {
        "source": "rayneo", "action": "delete_range",
        "from": "2026-11-10T00:00:00Z", "to": "2026-11-10T23:59:59Z",
    })
    assert code == 200 and body["ok"] is True and body["deleted_segments"] == 1
    assert body["deleted_episodes"] >= 1
    code, q = _post(server, "/v1/rayneo/query", {"source": "rayneo", "limit": 20,
                                                "include_derived": True})
    blob = json.dumps(q, ensure_ascii=False)
    assert "将被删除的原文" not in blob
    assert "派生总结" not in blob
    # 回灌被拒
    code, again = _post(server, "/v1/rayneo/ingest", _batch(_item(1, "将被删除的原文")))
    assert "tombstoned" in again["results"][0]["effect"]


def test_stop_ingest_and_query_disabled(tmp_path):
    env = {
        "WHALE_GLASSES_TOKEN": "phone-token",
        "WHALE_GLASSES_STORE": str(tmp_path / "g.sqlite3"),
        "OWNER_UID": "u", "OWNER_QQ": "1",
        "DAILY_LIMIT": "10", "PASSIVE_DAILY_LIMIT": "50",
        "QUIET_START": "22", "QUIET_END": "7",
        "WHALE_RAYNEO_QUERY": "0",
    }
    delegates = build_delegates(
        env, brain_factory=_FakeBrain, quick_brain_factory=_FakeBrain,
        send_port_factory=_FakePort)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(delegates))
    srv.glasses_token = "phone-token"
    srv.owner_uin = "1"
    srv.glasses_env = env
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, body = _post(srv, "/v1/rayneo/control",
                           {"source": "rayneo", "action": "stop_ingest"})
        assert code == 200 and body["ingest_enabled"] is False
        code, body = _post(srv, "/v1/rayneo/ingest", _batch(_item(1, "x")))
        assert code == 503 and body["error"] == "ingest_disabled"
        code, q = _post(srv, "/v1/rayneo/query", {"source": "rayneo", "limit": 5})
        assert code == 200 and q["query_disabled"] is True and q["items"] == []
        _post(srv, "/v1/rayneo/control", {"source": "rayneo", "action": "resume_ingest"})
    finally:
        srv.shutdown(); srv.server_close()
        if delegates.get("rayneo_store"):
            delegates["rayneo_store"].close()


def test_health_lists_rayneo(server):
    import urllib.request
    with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/v1/health", timeout=10) as r:
        data = json.load(r)
    assert "/v1/rayneo/ingest" in data["capabilities"]
