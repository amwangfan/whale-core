"""api/server.py 管理 API 测试：真实回环 HTTP（127.0.0.1 + 临时端口），不触外网。"""

from __future__ import annotations

import json
from http.client import HTTPConnection

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

TOKEN = "test-token-123"


class FakeBrain:
    def reply(self, text, *, persona="", memories=(), history=()):
        return f"[fake] {text}"


@pytest.fixture()
def api():
    store = InMemoryStore()
    registry = PluginRegistry()
    manager = PluginManager(registry, store)
    manager.load_manifest(PluginManifest(
        plugin_id="web_search", name="搜索", version="0.1.0",
        actions=("search",), action_risk={"search": "read_only"},
    ))
    curator = MemoryCurator(store)
    companion = WhaleCompanion(store, FakeBrain(), curator=curator)
    orch = ConversationOrchestrator(store, companion)
    ctx = ApiContext(store=store, manager=manager, curator=curator,
                     orchestrator=orch, profile="demo")
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server
    server.stop()


def _req(server, method, path, *, token=TOKEN, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def _json(raw):
    return json.loads(raw.decode("utf-8"))


# ---------- 鉴权 ----------

def test_unauthorized_without_token(api):
    status, raw = _req(api, "GET", "/api/v1/status", token="")
    assert status == 401
    assert _json(raw)["error"]["code"] == "unauthorized"


def test_wrong_token_rejected(api):
    status, _ = _req(api, "GET", "/api/v1/status", token="wrong")
    assert status == 401


def test_non_api_path_404(api):
    status, _ = _req(api, "GET", "/nope")
    assert status == 404


def test_token_not_reflected(api):
    status, raw = _req(api, "GET", "/api/v1/status", token="")
    assert TOKEN not in raw.decode()


# ---------- status / health ----------

def test_status(api):
    status, raw = _req(api, "GET", "/api/v1/status")
    assert status == 200
    result = _json(raw)["result"]
    assert result["ok"] and result["profile"] == "demo"
    assert result["plugins"]["loaded"] == 1
    assert "uptime_s" in result


def test_health(api):
    status, raw = _req(api, "GET", "/api/v1/health")
    assert status == 200 and _json(raw)["result"]["ok"] is True


# ---------- plugins ----------

def test_plugins_list_and_info(api):
    status, raw = _req(api, "GET", "/api/v1/plugins")
    assert status == 200
    plugins = _json(raw)["result"]["plugins"]
    assert plugins[0]["plugin_id"] == "web_search"
    status, raw = _req(api, "GET", "/api/v1/plugins/web_search")
    assert status == 200
    assert "config" in _json(raw)["result"]


def test_plugin_unknown_404(api):
    status, _ = _req(api, "GET", "/api/v1/plugins/nope")
    assert status == 404
    status, _ = _req(api, "POST", "/api/v1/plugins/nope/enable")
    assert status == 404


def test_plugin_enable_disable_flow(api):
    status, raw = _req(api, "POST", "/api/v1/plugins/web_search/enable")
    assert status == 200
    status, raw = _req(api, "GET", "/api/v1/plugins/web_search/health")
    assert status == 200
    assert _json(raw)["result"]["status"] in ("healthy", "unknown")
    status, raw = _req(api, "POST", "/api/v1/plugins/web_search/disable")
    assert status == 200
    status, raw = _req(api, "GET", "/api/v1/plugins/web_search/health")
    assert _json(raw)["result"]["status"] == "disabled"


# ---------- memory ----------

def test_memory_search_get_delete(api):
    from urllib.parse import quote
    store = api.ctx.store
    api.ctx.curator.remember("用户对香菜过敏", source="t", evidence_id="e1",
                             stable_fact=True, consent=True)
    status, raw = _req(api, "GET", f"/api/v1/memory/search?q={quote('香菜')}")
    assert status == 200
    hits = _json(raw)["result"]["hits"]
    assert hits and "香菜" in hits[0]["summary"]
    key = hits[0]["key"]
    status, raw = _req(api, "GET", f"/api/v1/memory/{key}")
    assert status == 200
    status, raw = _req(api, "DELETE", f"/api/v1/memory/{key}")
    assert _json(raw)["result"]["deleted"] is True
    status, _ = _req(api, "GET", f"/api/v1/memory/{key}")
    assert status == 404


def test_memory_export_ndjson(api):
    api.ctx.curator.remember("测试事实", source="t", evidence_id="e", consent=True)
    status, raw = _req(api, "POST", "/api/v1/memory/export")
    assert status == 200
    lines = [l for l in raw.decode().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["content"]["summary"] == "测试事实"


# ---------- chat ----------

def test_chat_roundtrip(api):
    status, raw = _req(api, "POST", "/api/v1/chat",
                       body={"conversation_id": "c1", "text": "你好"})
    assert status == 200
    result = _json(raw)["result"]
    assert result["reply"] == "[fake] 你好"
    assert result["status"] == "answered"


def test_chat_bad_body(api):
    status, _ = _req(api, "POST", "/api/v1/chat", body={"text": "  "})
    assert status == 400
    status, _ = _req(api, "POST", "/api/v1/chat", body=["not-a-dict"])
    assert status == 400


# ---------- approvals ----------

def test_approvals_list_and_decision(api):
    from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox
    outbox = ProactiveOutbox(api.ctx.store)
    outbox.propose(ProactiveDraft("d1", "草稿文本", "r", ("e",), channel="direct_message"))
    status, raw = _req(api, "GET", "/api/v1/approvals")
    assert status == 200
    approvals = _json(raw)["result"]["approvals"]
    assert any(a["draft_id"] == "d1" for a in approvals)
    status, raw = _req(api, "POST", "/api/v1/approvals/d1/decision",
                       body={"decision": "approve"})
    assert status == 200
    status, _ = _req(api, "POST", "/api/v1/approvals/d1/decision",
                     body={"decision": "maybe"})
    assert status == 400


# ---------- events SSE ----------

def test_events_sse_replay(api):
    api.ctx.log_event("test", "hello")
    conn = HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.request("GET", "/api/v1/events", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = conn.getresponse()
    assert resp.status == 200
    assert "text/event-stream" in resp.getheader("Content-Type")
    chunk = resp.read1(1024) if hasattr(resp, "read1") else resp.read(1024)
    conn.close()
    assert b"test" in chunk


def test_server_requires_token():
    with pytest.raises(ValueError):
        WhaleApiServer(ApiContext(store=InMemoryStore()), port=0, token="")


# ---------- 静态控制台（SPA） ----------

@pytest.fixture()
def api_static(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>console</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    ctx = ApiContext(store=InMemoryStore())
    server = WhaleApiServer(ctx, port=0, token=TOKEN, static_dir=str(dist))
    server.start()
    yield server
    server.stop()


def test_static_index_and_spa_fallback(api_static):
    status, raw = _req(api_static, "GET", "/", token="")  # 静态资源不鉴权
    assert status == 200 and b"console" in raw
    status, raw = _req(api_static, "GET", "/plugins/whatever", token="")
    assert status == 200 and b"console" in raw  # SPA 回落


def test_static_traversal_blocked(api_static):
    status, _ = _req(api_static, "GET", "/../secrets", token="")
    assert status in (403, 404)  # 穿越被拒（http.client 可能先归一化路径）
    status, raw = _req(api_static, "GET", "/assets/app.js", token="")
    assert status == 200


def test_static_absent_dir_404(api):
    status, raw = _req(api, "GET", "/", token="")
    assert status == 404


# ---------- tasks 轨迹（PR-13 API 侧） ----------

def test_tasks_endpoint_lists_recorded_tasks(api):
    # 走一次 task 型对话（无意图模型不会产任务——直接测 store 形状 + 端点）
    store = api.ctx.store
    store.put("task:t1", {"task_id": "t1", "at": 1.0, "goal": "g",
                          "status": "answered", "capabilities": ["web.search"],
                          "receipts": ["tool:search_web:ok:x"]})
    store.put("tasks:recent", ["t1"])
    status, raw = _req(api, "GET", "/api/v1/tasks")
    assert status == 200
    tasks = _json(raw)["result"]["tasks"]
    assert tasks[0]["task_id"] == "t1"
    status, raw = _req(api, "GET", "/api/v1/tasks/t1")
    assert status == 200
    status, _ = _req(api, "GET", "/api/v1/tasks/nope")
    assert status == 404


def test_plugin_config_set_and_masked_view(api):
    mgr = api.ctx.manager
    status, raw = _req(api, "POST", "/api/v1/plugins/web_search/config",
                       body={"config": {"api_key": "sekrit-123", "region": "us"}})
    assert status == 200
    # info 里的 config 是脱敏视图：键名含 key 的值被打码
    status, raw = _req(api, "GET", "/api/v1/plugins/web_search")
    config = _json(raw)["result"]["config"]
    assert config.get("region") == "us"
    assert "sekrit-123" not in raw.decode()
    status, _ = _req(api, "POST", "/api/v1/plugins/nope/config",
                     body={"config": {}})
    assert status == 404
