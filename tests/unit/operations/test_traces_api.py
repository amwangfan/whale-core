"""issue #39：/traces 诊断视图 + /activity 用户视图 API 回环测试。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest

from whale_core.api.routes.traces import _ledger
from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.life.trace import TraceLedger
from whale_core.memory.store import InMemoryStore

TOKEN = "trace-api-test-token"


@pytest.mark.parametrize("as_path", [False, True])
def test_default_ledger_stays_beside_memory_database(tmp_path, monkeypatch, as_path):
    monkeypatch.delenv("WHALE_TRACE_DB", raising=False)
    data_dir = tmp_path / "private-data"
    data_dir.mkdir()
    memory_path = data_dir / "memory.sqlite3"
    store = SimpleNamespace(path=memory_path if as_path else str(memory_path))
    handler = SimpleNamespace(ctx=SimpleNamespace(store=store))
    ledger = _ledger(handler)
    try:
        trace_id = ledger.begin("test")
        assert (data_dir / "trace.sqlite3").is_file()
        assert _ledger(handler) is ledger
        assert trace_id
    finally:
        ledger.close()


def test_explicit_ledger_path_wins(tmp_path, monkeypatch):
    explicit = tmp_path / "custom-trace.sqlite3"
    monkeypatch.setenv("WHALE_TRACE_DB", str(explicit))
    handler = SimpleNamespace(ctx=SimpleNamespace(store=SimpleNamespace(path="unused/memory.sqlite3")))
    ledger = _ledger(handler)
    try:
        assert explicit.is_file()
    finally:
        ledger.close()


@contextmanager
def _server():
    store = InMemoryStore()  # in-memory → ledger 也走 :memory:
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    try:
        yield server, ctx
    finally:
        server.stop()


def _req(server, method, path, *, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def _seed(led: TraceLedger):
    t1 = led.begin("qq_inbound", conversation_id="group:9",
                   principal_id="anon:qq:1", message_id="m1")
    led.span(t1, "decide", "observe", reason_code="cooldown")
    led.finish(t1, "silent")
    t2 = led.begin("qq_inbound", conversation_id="private:2",
                   principal_id="owner", message_id="m2")
    led.span(t2, "decide", "ok", reason_code="private")
    led.span(t2, "send", "executed")
    led.finish(t2, "replied")
    return t1, t2


class TestTracesApi:
    def test_list_and_detail(self):
        with _server() as (server, ctx):
            ctx.trace_ledger = TraceLedger(":memory:")
            t1, t2 = _seed(ctx.trace_ledger)
            status, body = _req(server, "GET", "/api/v1/traces")
            assert status == 200
            r = body["result"]
            assert r["total"] == 2
            assert "spans" not in r["traces"][0]
            status, body = _req(server, "GET", f"/api/v1/traces/{t1}")
            assert status == 200
            trace = body["result"]["trace"]
            assert trace["status"] == "silent"
            assert trace["spans"][0]["reason_code"] == "cooldown"
            status, _ = _req(server, "GET", "/api/v1/traces/nope")
            assert status == 404

    def test_filters(self):
        with _server() as (server, ctx):
            ctx.trace_ledger = TraceLedger(":memory:")
            _seed(ctx.trace_ledger)
            status, body = _req(server, "GET",
                                "/api/v1/traces?status=silent")
            assert body["result"]["total"] == 1
            status, body = _req(server, "GET",
                                "/api/v1/traces?conversation_id=private:2")
            assert body["result"]["total"] == 1

    def test_activity_user_view(self):
        with _server() as (server, ctx):
            ctx.trace_ledger = TraceLedger(":memory:")
            _seed(ctx.trace_ledger)
            status, body = _req(server, "GET", "/api/v1/activity")
            assert status == 200
            items = body["result"]["items"]
            assert len(items) == 2
            silent = [i for i in items if i["status"] == "silent"][0]
            assert any("冷却" in ln for ln in silent["lines"])
            replied = [i for i in items if i["status"] == "replied"][0]
            assert any("已回复" in ln for ln in replied["lines"])

    def test_prune_endpoint(self):
        with _server() as (server, ctx):
            ctx.trace_ledger = TraceLedger(":memory:", max_age_days=0)
            _seed(ctx.trace_ledger)
            status, body = _req(server, "POST", "/api/v1/traces/prune")
            assert status == 200
            assert body["result"]["pruned"] >= 0
