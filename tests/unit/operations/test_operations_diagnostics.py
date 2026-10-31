"""G4 测试：能力健康视图、运营指标、doctor --json/--deep、插件信任分级。

全部离线：deep 探测走注入的 fake opener/回环服务，不触外网。
"""

from __future__ import annotations

import json
import time

import pytest

from whale_core.core.diagnostics import (
    InstrumentedProvider,
    capability_health,
    provider_health_report,
)
from whale_core.core.runtime import ToolRegistry, ToolSpec
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.manifest import PluginManifest, RiskClass
from whale_core.plugins.registry import PluginRegistry


class _Provider:
    def __init__(self, healthy=True):
        self._healthy = healthy

    def healthcheck(self):
        return self._healthy

    def reply(self, text, *, persona="", memories=(), history=()):
        return "ok"


def _setup():
    store = InMemoryStore()
    registry = PluginRegistry()
    manager = PluginManager(registry, store)
    tools = ToolRegistry()
    return store, registry, manager, tools


def _register_plugin(registry, pid, trust="bundled", risk=RiskClass.READ_ONLY,
                     provider=None):
    if provider is not None:
        # provider 只允许 provider 类插件（processor/model/memory），声明 provides
        manifest = PluginManifest(
            plugin_id=pid, name=pid, version="1.0.0", kind="processor",
            provides=(f"test.{pid}",), trust=trust)
    else:
        # 动作类插件：actions 是名元组，风险级在 action_risk 映射
        manifest = PluginManifest(
            plugin_id=pid, name=pid, version="1.0.0", kind="output",
            actions=("go",), action_risk={"go": risk.value}, trust=trust)
    registry.register(manifest, provider=provider)


# ---------- 能力健康视图 ----------


def test_capability_health_five_layers():
    store, registry, manager, tools = _setup()
    _register_plugin(registry, "plug_a", provider=_Provider(healthy=True))
    tools.register(ToolSpec(name="tool_a", capability="test.plug_a",
                            plugin_id="plug_a", action="go"))
    # 纯本地 handler 工具（无插件映射）
    tools.register(ToolSpec(name="local_echo", capability="test.local",
                            handler=lambda s: "ok"))
    # 声明了工具但插件未注册的能力
    tools.register(ToolSpec(name="orphan", capability="test.orphan",
                            plugin_id="ghost_plugin", action="go"))
    view = capability_health(manager, tools, store)
    by_cap = {c["capability"]: c for c in view["capabilities"]}
    healthy = by_cap["test.plug_a"]
    assert healthy["declared"] and healthy["registered"] and healthy["enabled"] is False
    # manager.status() 未启用 → enabled False；reachable 来自 provider.healthcheck
    assert healthy["reachable"] is True
    assert healthy["verified"] == "unavailable"  # 无真实成功证据
    local = by_cap["test.local"]
    assert local["registered"] and local["enabled"] and local["reachable"] is True
    orphan = by_cap["test.orphan"]
    assert orphan["registered"] is False and orphan["enabled"] is False
    assert view["summary"]["total"] == 3


def test_capability_health_verified_by_task_receipts():
    store, registry, manager, tools = _setup()
    _register_plugin(registry, "plug_a", provider=_Provider())
    tools.register(ToolSpec(name="tool_a", capability="test.plug_a",
                            plugin_id="plug_a", action="go"))
    store.put("task:t1", {"task_id": "t1", "status": "answered",
                          "receipts": ["tool:tool_a:ok:done"]})
    store.put("tasks:recent", ["t1"])
    view = capability_health(manager, tools, store)
    entry = view["capabilities"][0]
    assert entry["verified"] is True


def test_capability_health_verified_by_provider_calls():
    store, registry, manager, tools = _setup()
    _register_plugin(registry, "llm_chat", provider=_Provider())
    tools.register(ToolSpec(name="t", capability="test.llm_chat",
                            plugin_id="llm_chat", action="go"))
    provider = InstrumentedProvider("llm_chat", _Provider(), store)
    provider.reply("hello")
    view = capability_health(manager, tools, store)
    assert view["capabilities"][0]["verified"] is True


# ---------- 运营指标（真实数据 / unavailable） ----------


def test_provider_metrics_24h_and_latency_percentiles():
    store = InMemoryStore()

    class Flaky:
        def reply(self, text, *, persona="", memories=(), history=()):
            if "bad" in text:
                raise TimeoutError("boom")
            return "ok"

    provider = InstrumentedProvider("test-p", Flaky(), store)
    provider.reply("fine")
    with pytest.raises(TimeoutError):
        provider.reply("bad")
    report = provider_health_report(store)
    view = report["providers"][0]
    assert view["calls"] == 2 and view["errors"] == 1
    # 24h 失败类别：真实事件窗口
    assert view["errors_24h"] == {"TimeoutError": 1}
    # 延迟分位来自真实样本
    assert view["latency"]["samples"] == 2
    assert view["latency"]["p50_ms"] >= 0 and view["latency"]["p95_ms"] >= 0


def test_provider_metrics_unavailable_without_samples():
    view = provider_health_report(InMemoryStore())
    assert view == {"providers": [], "totals": {"calls": 0, "errors": 0}}


def test_old_stats_without_new_fields_still_load():
    """存量统计键（无 error_events/latency_samples）读取不炸，分位标 unavailable。"""
    store = InMemoryStore()
    store.put("provider:health:legacy", {"calls": 3, "errors": 0,
                                         "last_latency_ms": 1.0,
                                         "total_latency_ms": 3.0,
                                         "chars_in": 9, "chars_out": 6,
                                         "updated_at": "x"})
    view = provider_health_report(store)["providers"][0]
    assert view["calls"] == 3
    assert view["latency"] == "unavailable"
    assert view["errors_24h"] == "unavailable"


# ---------- doctor --json / --deep ----------


def _doctor_args(tmp_path, **kw):
    from types import SimpleNamespace

    base = dict(config_dir=str(tmp_path), json=False, deep=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _make_config(tmp_path, profile="demo"):
    (tmp_path / "config.json").write_text(json.dumps({"profile": profile}),
                                          encoding="utf-8")
    (tmp_path / "manifests").mkdir(exist_ok=True)
    from whale_core.memory.sqlite_store import SqliteMemoryStore

    SqliteMemoryStore(str(tmp_path / "memory.sqlite3")).close()


def test_doctor_json_machine_readable(tmp_path, capsys):
    from whale_core.cli.main import _cmd_doctor

    _make_config(tmp_path)
    code = _cmd_doctor(_doctor_args(tmp_path, json=True))
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["deep"] is False
    assert isinstance(out["checks"], list) and out["checks"]
    assert all("status" in c and "detail" in c for c in out["checks"])
    # 默认档：不触网（无任何 live 检查）
    assert not any(c["live"] for c in out["checks"])
    assert code == 0


def test_doctor_deep_skips_without_key(tmp_path, capsys):
    from whale_core.cli.main import _cmd_doctor

    (tmp_path / "config.json").write_text(json.dumps(
        {"profile": "cloud", "model": {"base_url": "https://api.deepseek.com/v1"}}),
        encoding="utf-8")
    (tmp_path / "manifests").mkdir(exist_ok=True)
    from whale_core.memory.sqlite_store import SqliteMemoryStore

    SqliteMemoryStore(str(tmp_path / "memory.sqlite3")).close()
    import os
    os.environ.pop("DEEPSEEK_API_KEY", None)
    code = _cmd_doctor(_doctor_args(tmp_path, json=True, deep=True))
    out = json.loads(capsys.readouterr().out)
    deep_checks = [c for c in out["checks"] if "deep" in c["detail"]]
    assert deep_checks and all(not c["live"] for c in deep_checks)  # 无 key 不探测
    assert code == 1  # cloud 档缺 key 本身是 fail 检查项


def test_deep_probe_models_offline_fake():
    """真实连通逻辑：fake opener 断言 URL 形状与状态判定（不触网）。"""
    import urllib.error
    import whale_core.cli.main as cli

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class FakeOpener:
        def __init__(self):
            self.urls = []

        def open(self, req, timeout=0):
            self.urls.append(req.full_url)
            return FakeResp()

    import urllib.request
    original = urllib.request.build_opener
    fake = FakeOpener()
    urllib.request.build_opener = lambda *a, **k: fake
    try:
        ok, detail = cli._deep_probe_models("https://api.deepseek.com/v1", "sk-x")
    finally:
        urllib.request.build_opener = original
    assert ok and detail == "HTTP 200"
    assert fake.urls == ["https://api.deepseek.com/v1/models"]


# ---------- 插件信任分级 ----------


def test_manifest_trust_validation():
    PluginManifest(plugin_id="p1", name="p1", version="1.0.0", kind="output",
                   trust="external")
    with pytest.raises(ValueError):
        PluginManifest(plugin_id="p2", name="p2", version="1.0.0", kind="output",
                       trust="root")  # 非法信任级别


def test_external_plugin_non_readonly_requires_approval():
    from whale_core.plugins.policy import PluginActionPolicy

    registry = PluginRegistry()
    _register_plugin(registry, "extplug", trust="external",
                     risk=RiskClass.COMMUNICATE)
    # bundled 对照组：communicate + auto_approve 宿主放宽 → ready
    _register_plugin(registry, "bundledplug", trust="bundled",
                     risk=RiskClass.COMMUNICATE)
    policy = PluginActionPolicy(
        registry, auto_approve_risks=frozenset({"communicate"}))
    ext = policy.evaluate("extplug", "go", is_owner=True)
    assert ext.status == "needs_approval"  # external 不适用宿主自动放宽
    bundled = policy.evaluate("bundledplug", "go", is_owner=True)
    assert bundled.status == "ready"
    # external + 显式审批 → ready（审批通道不变）
    approved = policy.evaluate("extplug", "go", is_owner=True,
                               explicitly_approved=True)
    assert approved.status == "ready"
    # external 的 read_only 动作不受影响
    _register_plugin(registry, "extro", trust="external", risk=RiskClass.READ_ONLY)
    assert policy.evaluate("extro", "go", is_owner=True).status == "ready"
