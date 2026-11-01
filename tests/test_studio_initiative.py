"""UX-07（issue #42）：统一主动行为 API 回环测试。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.life.initiative import PREFS_KEY, ProactivePrefs, load_prefs
from whale_core.life.trace import TraceLedger
from whale_core.memory.store import InMemoryStore

TOKEN = "initiative-test-token"


@contextmanager
def _server():
    store = InMemoryStore()
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    try:
        yield server, store, ctx
    finally:
        server.stop()


def _req(server, method, path, *, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


class TestInitiativeApi:
    def test_get_default_view(self):
        with _server() as (server, _, _2):
            status, body = _req(server, "GET", "/api/v1/studio/initiative")
            assert status == 200
            r = body["result"]
            assert r["global"]["fields"]["daily_limit"]["source"] == "default"
            assert "静默" in r["global"]["quiet_hours_rule"]
            assert "主动" in r["global"]["quiet_hours_rule"]  # 用户主动发消息照常回应
            assert "enabled" in r["global"]["locked_by_deploy"]
            assert r["rhythm"]["activity_modes"] == ["quiet", "balanced", "lively"]
            assert r["recent_decisions"] == []

    def test_put_global_then_kv_effective(self):
        with _server() as (server, store, _2):
            status, body = _req(server, "PUT",
                                "/api/v1/studio/initiative/global",
                                body={"daily_limit": 6, "quiet_start": "21:30"})
            assert status == 200
            fields = body["result"]["global"]["fields"]
            assert fields["daily_limit"]["value"] == 6
            assert fields["daily_limit"]["source"] == "console"
            assert fields["quiet_start"]["value"] == "21:30"
            # 单一配置来源：runner 的 load_prefs 看到同一 kv
            prefs = load_prefs(store, ProactivePrefs())
            assert prefs.daily_limit == 6
            assert prefs.quiet_start.hour == 21 and prefs.quiet_start.minute == 30

    def test_put_global_validation(self):
        with _server() as (server, _, _2):
            status, _ = _req(server, "PUT", "/api/v1/studio/initiative/global",
                             body={"daily_limit": -1})
            assert status == 422
            status, _ = _req(server, "PUT", "/api/v1/studio/initiative/global",
                             body={"quiet_start": "25:00"})
            assert status == 422
            status, _ = _req(server, "PUT", "/api/v1/studio/initiative/global",
                             body={"enabled": True})  # 部署侧字段不热改
            assert status == 422

    def test_reset_restores_default(self):
        with _server() as (server, store, _2):
            _req(server, "PUT", "/api/v1/studio/initiative/global",
                 body={"daily_limit": 6})
            status, body = _req(server, "PUT",
                                "/api/v1/studio/initiative/global",
                                body={"reset": True})
            assert status == 200
            assert store.get(PREFS_KEY) is None
            assert body["result"]["global"]["fields"]["daily_limit"]["source"] \
                == "default"

    def test_channel_default_set_and_clear(self):
        with _server() as (server, store, _2):
            status, body = _req(server, "PUT",
                                "/api/v1/studio/initiative/channel",
                                body={"channel": "qq", "activity": "lively",
                                      "max_segments": 2})
            assert status == 200
            ch = body["result"]["rhythm"]["channels"]["qq"]
            assert ch["activity"] == "lively"
            # 清空
            _, body = _req(server, "PUT", "/api/v1/studio/initiative/channel",
                           body={"channel": "qq", "activity": ""})
            assert "qq" not in body["result"]["rhythm"]["channels"]
            # 非法档位
            status, _ = _req(server, "PUT", "/api/v1/studio/initiative/channel",
                             body={"channel": "qq", "activity": "wild"})
            assert status == 422

    def test_recent_decisions_from_ledger(self):
        with _server() as (server, _, ctx):
            ctx.trace_ledger = TraceLedger(":memory:")
            tid = ctx.trace_ledger.begin("qq_inbound",
                                         conversation_id="group:9")
            ctx.trace_ledger.span(tid, "decide", "observe",
                                  reason_code="cooldown")
            ctx.trace_ledger.finish(tid, "silent")
            status, body = _req(server, "GET", "/api/v1/studio/initiative")
            decisions = body["result"]["recent_decisions"]
            assert len(decisions) == 1
            assert decisions[0]["reason_code"] == "cooldown"
            assert "冷却" in decisions[0]["reason_text"]
