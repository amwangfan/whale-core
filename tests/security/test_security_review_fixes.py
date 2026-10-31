"""Regression tests for post-review integration and security fixes (offline).

(Moved from tests/test_review_fixes.py; issue #29 domain-based layout.)
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, time, timezone
from http.client import HTTPConnection

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.cli.main import main
from whale_core.core.deepseek import DeepSeekCompanion
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry
from whale_core.profiles import assemble_runtime


def _manager(store, secret_dir):
    manager = PluginManager(PluginRegistry(), store, secret_dir=secret_dir)
    manager.load_manifest(PluginManifest(plugin_id="demo_plugin",
                        name="Demo", version="1.0.0", kind="output"))
    return manager


def test_secrets_never_stored_in_regular_kv_and_reload(tmp_path):
    store = InMemoryStore()
    private = tmp_path / "secrets"
    manager = _manager(store, private)
    manager.set_config("demo_plugin", {
        "api_token": "sk-PRIVATE", "region": "us",
        "nested": {"auth_header": "Bearer PRIV"}, "listy": [{"session_key": "S"}, "plain"],
    })
    serialized = json.dumps(store.get("plugin:config:demo_plugin"))
    assert "sk-PRIVATE" not in serialized
    assert "Bearer PRIV" not in serialized
    assert '"api_token": "***"' in serialized
    assert manager.config_view("demo_plugin")["api_token"] == "***"
    assert private.exists()
    assert private.joinpath("demo_plugin.json").stat().st_mode & 0o077 == 0
    reloaded = _manager(store, private)
    assert reloaded.runtime_config("demo_plugin")["api_token"] == "sk-PRIVATE"
    reloaded.patch_config("demo_plugin", {"region": "cn", "api_token": "***"})
    again = _manager(store, private)
    assert again.runtime_config("demo_plugin")["api_token"] == "sk-PRIVATE"
    assert again.runtime_config("demo_plugin")["region"] == "cn"
    again.patch_config("demo_plugin", {"api_token": None})
    assert again.runtime_config("demo_plugin").get("api_token") is None


def test_legacy_plugin_secret_migrates_on_manifest_load(tmp_path):
    store = InMemoryStore()
    store.put("plugin:config:demo_plugin", {"api_key": "LEGACY-SECRET", "region": "us"})
    manager = _manager(store, tmp_path / "secrets")
    assert manager.runtime_config("demo_plugin")["api_key"] == "LEGACY-SECRET"
    assert "LEGACY-SECRET" not in repr(store.get("plugin:config:demo_plugin"))


def test_intent_router_search_and_normal_chat(monkeypatch):
    """local 档（demo 档已移除）：规则路由搜索进 web.search，闲聊走 chat。"""
    monkeypatch.setattr(DeepSeekCompanion, "reply",
                        lambda self, text, **kwargs: "REAL-MODEL-FAKE-TRANSPORT")
    from search_web.web_search import TavilyProvider, SearchResult
    monkeypatch.setattr(TavilyProvider, "search",
                        lambda self, query, **kwargs: [
                            SearchResult(title="Evidence", url="https://example.com",
                                         snippet=query, source="tavily")
                        ])
    bundle = assemble_runtime("local", env={"TAVILY_API_KEY": "test"})
    searched = bundle.orchestrator.handle_message("c", "帮我搜索：鲸鱼")
    assert searched.plan is not None
    assert searched.plan.capabilities == ("web.search",)
    assert "tool:search_web:ok:" in " ".join(searched.receipts)
    assert searched.reply == "REAL-MODEL-FAKE-TRANSPORT"
    casual = bundle.orchestrator.handle_message("c", "你好呀")
    assert casual.status == "answered"
    assert casual.plan is not None and casual.plan.kind == "chat"


def test_cloud_profile_uses_passed_env_and_search_tool(monkeypatch):
    monkeypatch.setattr(DeepSeekCompanion, "reply",
                        lambda self, text, **kwargs: "REAL-MODEL-FAKE-TRANSPORT")
    from search_web.web_search import TavilyProvider, SearchResult
    monkeypatch.setattr(TavilyProvider, "search",
                        lambda self, query, **kwargs: [
                            SearchResult(title="Evidence", url="https://example.com",
                                         snippet=query, source="tavily")
                        ])
    bundle = assemble_runtime("cloud", env={"DEEPSEEK_API_KEY": "test",
                                            "TAVILY_API_KEY": "test"})
    result = bundle.orchestrator.handle_message("c", "搜索：测试新闻")
    assert result.status == "answered"
    assert "tool:search_web:ok:" in " ".join(result.receipts)
    assert bundle.companion.model.api_key == "test"


def test_cli_run_uses_cloud_profile_not_mock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(DeepSeekCompanion, "reply",
                        lambda self, text, **kwargs: "REAL-MODEL-FAKE-TRANSPORT")
    assert main(["setup", "--profile", "cloud", "--config-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO("你好\n/quit\n"))
    assert main(["run", "--config-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "whale cloud REPL" in out
    assert "whale> REAL-MODEL-FAKE-TRANSPORT" in out
    assert "whale> [mock]" not in out


def test_web_decision_records_authorization_but_not_delivery():
    store = InMemoryStore()
    outbox = ProactiveOutbox(store)
    outbox.propose(ProactiveDraft("id1", "test", "needed", ("e",), channel="direct_message"))
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, host="127.0.0.1", port=0, token="secret")
    server.start()
    try:
        def request(decision):
            conn = HTTPConnection("127.0.0.1", server.port, timeout=4)
            conn.request("POST", "/api/v1/approvals/id1/decision",
                         body=json.dumps({"decision": decision}),
                         headers={"Authorization": "Bearer secret",
                                  "Content-Type": "application/json"})
            response = conn.getresponse()
            value = response.status, json.loads(response.read().decode())
            conn.close()
            return value
        status, body = request("approve")
        assert status == 200
        assert body["result"]["execution_status"] == "not_dispatched"
        assert outbox.ledger("id1").get("attempts", 0) == 0
        assert request("reject")[0] == 409
    finally:
        server.stop()
    prefs = ProactivePrefs(enabled=True,
                           allowed_channels=frozenset({"direct_message"}),
                           daily_limit=10, quiet_start=time(23), quiet_end=time(6))
    class Port:
        called = 0
        def send(self, draft):
            self.called += 1
            return True
    port = Port()
    verdict = outbox.dispatch("id1", prefs, now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
                              port=port)
    assert verdict.status == "accepted" and port.called == 1


def test_web_rejection_blocks_dispatch():
    store = InMemoryStore()
    outbox = ProactiveOutbox(store)
    outbox.propose(ProactiveDraft("id2", "test", "needed", ("e",), channel="direct_message"))
    store.put("life:decision:id2", {"decision": "reject"})
    prefs = ProactivePrefs(enabled=True,
                           allowed_channels=frozenset({"direct_message"}),
                           daily_limit=10)
    assert outbox.inspect("id2", prefs, now=datetime.now(timezone.utc),
                          explicitly_approved=True).status == "rejected"


def test_sse_pushes_new_events_after_connection():
    store = InMemoryStore()
    ctx = ApiContext(store=store)
    server = WhaleApiServer(ctx, port=0, token="secret")
    server.start()
    conn = HTTPConnection("127.0.0.1", server.port, timeout=4)
    try:
        conn.request("GET", "/api/v1/events", headers={"Authorization": "Bearer secret"})
        response = conn.getresponse()
        assert response.status == 200
        ctx.log_event("new-event", "later")
        data = b""
        for _ in range(3):
            data += response.read1(1024)
            if b"new-event" in data:
                break
        assert b"new-event" in data
        assert b"later" in data
    finally:
        conn.close()
        server.stop()
