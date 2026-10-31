"""PluginManager 测试：清单目录加载、生命周期、健康检查、配置管理（全离线）。

覆盖：manifest api_version 校验、load_dir 错误收集、启停状态持久化回放、
工厂接线与失败兜底、healthcheck 五态、config_schema 校验与脱敏视图。
"""

from __future__ import annotations

import json

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.registry import NullPluginExecutor, PluginRegistry

# ---------------------------------------------------------------------------
# 示例清单 / 测试替身
# ---------------------------------------------------------------------------

SHOP = {
    "plugin_id": "shop",
    "name": "购物插件（示例）",
    "version": "0.1.0",
    "actions": ["purchase"],
    "action_risk": {"purchase": "transaction"},
}
MINUTES = {
    "plugin_id": "rayneo_minutes",
    "name": "雷鸟纪要（示例）",
    "version": "0.1.0",
    "inputs": ["minutes.segment"],
}
SUMMARIZER = {
    "plugin_id": "summarizer",
    "name": "摘要器（示例）",
    "version": "0.2.0",
    "kind": "processor",
    "provides": ["summarize"],
}
CONFIG_SCHEMA = {
    "required": ["api_token"],
    "properties": {
        "api_token": {"type": "string", "format": "secret"},
        "webhook_secret": {"type": "string", "format": "secret"},
        "endpoint": {"type": "string"},
        "retries": {"type": "number"},
        "debug": {"type": "boolean"},
    },
}


def _write_plugin(directory, data, filename: str | None = None):
    pid = data.get("plugin_id", "plugin")
    file = directory / (filename or f"{pid}.json")
    file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return file


def _make_manager(store=None, factories=None):
    store = store or InMemoryStore()
    registry = PluginRegistry()
    return PluginManager(registry, store, factories=factories), registry, store


class SpyExecutor:
    """带健康检查开关的执行器替身。"""

    def __init__(self, health=True):
        self.calls = []
        self._health = health

    def execute(self, action, target, args):
        self.calls.append((action, target, dict(args)))
        return True, "ok"

    def healthcheck(self):
        if isinstance(self._health, Exception):
            raise self._health
        return self._health


# ---------------------------------------------------------------------------
# manifest api_version 增量
# ---------------------------------------------------------------------------


def test_manifest_api_version_defaults_to_1():
    m = PluginManifest(plugin_id="shop", name="购物", version="0.1.0",
                       actions=("purchase",), action_risk={"purchase": "transaction"})
    assert m.api_version == "1"


def test_manifest_api_version_accepts_minor():
    m = PluginManifest(plugin_id="shop", name="购物", version="0.1.0",
                       actions=("purchase",), action_risk={"purchase": "transaction"},
                       api_version="1.2")
    assert m.api_version == "1.2"


@pytest.mark.parametrize("bad", ["", "abc", "x.1", ".1", " 1"])
def test_manifest_api_version_rejects_invalid(bad):
    with pytest.raises(ValueError):
        PluginManifest(plugin_id="shop", name="购物", version="0.1.0",
                       actions=("purchase",), action_risk={"purchase": "transaction"},
                       api_version=bad)


def test_manifest_api_version_rejects_non_string():
    with pytest.raises(ValueError):
        PluginManifest(plugin_id="shop", name="购物", version="0.1.0",
                       actions=("purchase",), action_risk={"purchase": "transaction"},
                       api_version=1)


# ---------------------------------------------------------------------------
# load_dir
# ---------------------------------------------------------------------------


def test_load_dir_loads_json_files(tmp_path):
    _write_plugin(tmp_path, SHOP)
    _write_plugin(tmp_path, MINUTES)
    manager, _, _ = _make_manager()
    loaded = manager.load_dir(tmp_path)
    assert loaded == ["rayneo_minutes", "shop"]  # 按文件名排序
    rows = {row["plugin_id"]: row for row in manager.status()}
    assert rows["shop"]["status"] == "loaded"
    assert rows["shop"]["enabled"] is False
    assert rows["shop"]["kind"] == "output"
    assert rows["shop"]["api_version"] == "1"
    assert rows["shop"]["actions"] == ["purchase"]
    assert rows["rayneo_minutes"]["kind"] == "input"
    assert manager.errors == []


def test_load_dir_records_bad_json_and_continues(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    _write_plugin(tmp_path, SHOP)
    manager, _, _ = _make_manager()
    loaded = manager.load_dir(tmp_path)
    assert loaded == ["shop"]
    assert len(manager.errors) == 1
    assert manager.errors[0]["path"].endswith("broken.json")
    assert manager.errors[0]["error"]


def test_load_dir_records_invalid_manifest(tmp_path):
    _write_plugin(tmp_path, {"plugin_id": "noname", "version": "0.1.0",
                             "actions": ["purchase"],
                             "action_risk": {"purchase": "read_only"}})
    manager, _, _ = _make_manager()
    assert manager.load_dir(tmp_path) == []
    assert len(manager.errors) == 1
    assert "name" in manager.errors[0]["error"]


def test_load_dir_rejects_incompatible_api_version(tmp_path):
    _write_plugin(tmp_path, dict(SHOP, api_version="2"))
    manager, _, _ = _make_manager()
    assert manager.load_dir(tmp_path) == []
    assert len(manager.errors) == 1
    assert "api_version" in manager.errors[0]["error"]
    assert manager.status() == []


def test_load_dir_missing_directory(tmp_path):
    manager, _, _ = _make_manager()
    assert manager.load_dir(tmp_path / "nope") == []
    assert len(manager.errors) == 1
    assert "not found" in manager.errors[0]["error"]


def test_load_dir_rejects_overwrite_of_enabled_plugin(tmp_path):
    _write_plugin(tmp_path, SHOP)
    manager, registry, _ = _make_manager()
    manager.load_dir(tmp_path)
    manager.enable("shop")
    assert registry.manifest("shop") is not None

    _write_plugin(tmp_path, dict(SHOP, version="9.9.9"))  # 同 id 新版本
    assert manager.load_dir(tmp_path) == []
    assert any("refusing to overwrite" in e["error"] for e in manager.errors)
    row = manager.status()[0]
    assert row["status"] == "enabled"
    assert row["version"] == "0.1.0"  # 旧清单未被覆盖


# ---------------------------------------------------------------------------
# 生命周期：enable / disable / 持久化
# ---------------------------------------------------------------------------


def test_enable_then_disable_lifecycle(tmp_path):
    _write_plugin(tmp_path, SHOP)
    manager, registry, _ = _make_manager()
    manager.load_dir(tmp_path)
    manager.enable("shop")
    assert registry.manifest("shop") is not None
    assert manager.status()[0]["status"] == "enabled"

    manager.disable("shop")
    assert registry.manifest("shop") is None
    row = manager.status()[0]
    assert row["status"] == "disabled"
    assert row["enabled"] is False


def test_disable_is_idempotent_for_unknown_plugin():
    manager, _, _ = _make_manager()
    manager.disable("ghost")  # 未加载：静默返回，不抛


def test_enabled_state_persists_across_manager_instances(tmp_path):
    _write_plugin(tmp_path, SHOP)
    store = InMemoryStore()
    manager1, registry1, _ = _make_manager(store)
    manager1.load_dir(tmp_path)
    manager1.enable("shop")

    # 新 manager + 新 registry、同一 store：重新 load_dir 后回放为 enabled
    manager2, registry2, _ = _make_manager(store)
    assert manager2.load_dir(tmp_path) == ["shop"]
    row = manager2.status()[0]
    assert row["status"] == "enabled"
    assert row["enabled"] is True
    assert registry2.manifest("shop") is not None  # 回放时重新注册
    assert registry2.executor("shop") is not None


def test_disabled_state_persists_across_manager_instances(tmp_path):
    _write_plugin(tmp_path, SHOP)
    store = InMemoryStore()
    manager1, _, _ = _make_manager(store)
    manager1.load_dir(tmp_path)
    manager1.enable("shop")
    manager1.disable("shop")

    manager2, registry2, _ = _make_manager(store)
    manager2.load_dir(tmp_path)
    row = manager2.status()[0]
    assert row["status"] == "disabled"
    assert registry2.manifest("shop") is None


# ---------------------------------------------------------------------------
# 工厂接线
# ---------------------------------------------------------------------------


def test_enable_calls_factory_and_wires_executor(tmp_path):
    _write_plugin(tmp_path, SHOP)
    spy = SpyExecutor()
    calls = []

    def factory():
        calls.append("shop")
        return {"executor": spy}

    manager, registry, _ = _make_manager(factories={"shop": factory})
    manager.load_dir(tmp_path)
    manager.enable("shop")
    assert calls == ["shop"]
    assert registry.executor("shop") is spy
    assert spy.execute("purchase", "order-1", {}) == (True, "ok")


def test_factory_exception_marks_error_without_raising(tmp_path):
    _write_plugin(tmp_path, SHOP)

    def bad_factory():
        raise RuntimeError("connect to secret-host:1234 failed")

    manager, registry, _ = _make_manager(factories={"shop": bad_factory})
    manager.load_dir(tmp_path)
    manager.enable("shop")  # 不抛
    state_rows = manager.status()
    assert state_rows[0]["status"] == "error"
    assert registry.manifest("shop") is None  # 未注册
    state = manager._states["shop"]
    assert state.error == "RuntimeError"  # 只记异常类型名
    assert "secret-host" not in state.error  # 不含敏感参数


def test_enable_without_factory_uses_null_executor(tmp_path):
    _write_plugin(tmp_path, SHOP)
    manager, registry, _ = _make_manager()
    manager.load_dir(tmp_path)
    manager.enable("shop")
    executor = registry.executor("shop")
    assert isinstance(executor, NullPluginExecutor)
    ok, receipt = executor.execute("purchase", "order-1", {})
    assert ok is False
    assert receipt == "plugin not connected"


# ---------------------------------------------------------------------------
# healthcheck 五态
# ---------------------------------------------------------------------------


def _enabled_manager(tmp_path, executor):
    _write_plugin(tmp_path, SHOP)
    factories = {"shop": lambda: {"executor": executor}} if executor is not None else None
    manager, registry, _ = _make_manager(factories=factories)
    manager.load_dir(tmp_path)
    manager.enable("shop")
    return manager


def test_healthcheck_healthy(tmp_path):
    manager = _enabled_manager(tmp_path, SpyExecutor(health=True))
    result = manager.healthcheck("shop")
    assert result == {"plugin_id": "shop", "status": "healthy", "detail": "ok"}


def test_healthcheck_false_is_unhealthy(tmp_path):
    manager = _enabled_manager(tmp_path, SpyExecutor(health=False))
    assert manager.healthcheck("shop")["status"] == "unhealthy"


def test_healthcheck_exception_is_unhealthy(tmp_path):
    manager = _enabled_manager(tmp_path, SpyExecutor(health=RuntimeError("boom")))
    result = manager.healthcheck("shop")
    assert result["status"] == "unhealthy"
    assert result["detail"] == "RuntimeError"


def test_healthcheck_unknown_without_method(tmp_path):
    manager = _enabled_manager(tmp_path, None)  # NullPluginExecutor 无 healthcheck
    assert manager.healthcheck("shop")["status"] == "unknown"


def test_healthcheck_disabled_and_missing(tmp_path):
    _write_plugin(tmp_path, SHOP)
    manager, _, _ = _make_manager()
    manager.load_dir(tmp_path)
    assert manager.healthcheck("shop")["status"] == "disabled"
    manager.enable("shop")
    manager.disable("shop")
    assert manager.healthcheck("shop")["status"] == "disabled"
    assert manager.healthcheck("ghost")["status"] == "missing"


# ---------------------------------------------------------------------------
# 配置管理：schema 校验 + 脱敏视图
# ---------------------------------------------------------------------------


def _manager_with_schema(tmp_path):
    _write_plugin(tmp_path, dict(SHOP, config_schema=CONFIG_SCHEMA))
    manager, registry, store = _make_manager()
    manager.load_dir(tmp_path)
    return manager, store


def test_set_config_rejects_missing_required(tmp_path):
    manager, _ = _manager_with_schema(tmp_path)
    with pytest.raises(ValueError, match="api_token"):
        manager.set_config("shop", {"endpoint": "https://example.invalid"})


def test_set_config_rejects_type_mismatch(tmp_path):
    manager, _ = _manager_with_schema(tmp_path)
    with pytest.raises(ValueError, match="retries"):
        manager.set_config("shop", {"api_token": "tok", "retries": "three"})
    with pytest.raises(ValueError, match="debug"):
        manager.set_config("shop", {"api_token": "tok", "debug": 1})


def test_set_config_number_rejects_bool(tmp_path):
    manager, _ = _manager_with_schema(tmp_path)
    with pytest.raises(ValueError, match="retries"):
        manager.set_config("shop", {"api_token": "tok", "retries": True})


def test_set_config_without_schema_stores(tmp_path):
    _write_plugin(tmp_path, MINUTES)  # 无 config_schema
    manager, _, store = _make_manager()
    manager.load_dir(tmp_path)
    manager.set_config("rayneo_minutes", {"anything": 1})
    assert store.get("plugin:config:rayneo_minutes") == {"anything": 1}


def test_config_view_masks_secrets(tmp_path):
    manager, _ = _manager_with_schema(tmp_path)
    manager.set_config(
        "shop",
        {
            "api_token": "tok-123",          # format: secret → 打码
            "endpoint": "https://example.invalid",  # 普通字段原样
            "retries": 3,
            "refresh_key": "rk-456",         # 键名含 key → 打码
        },
    )
    view = manager.config_view("shop")
    assert view["api_token"] == "***"
    assert view["refresh_key"] == "***"
    assert view["endpoint"] == "https://example.invalid"
    assert view["retries"] == 3
    assert view["webhook_secret"] == ""  # schema secret 未配置 → ""
    assert "tok-123" not in repr(view) and "rk-456" not in repr(view)


def test_config_view_masks_by_key_name_without_schema(tmp_path):
    _write_plugin(tmp_path, MINUTES)
    manager, _, _ = _make_manager()
    manager.load_dir(tmp_path)
    manager.set_config("rayneo_minutes",
                       {"Password": "pw", "note": "plain", "AUTH_TOKEN": "t"})
    view = manager.config_view("rayneo_minutes")
    assert view["Password"] == "***"
    assert view["AUTH_TOKEN"] == "***"
    assert view["note"] == "plain"


# ---------------------------------------------------------------------------
# 禁用后能力下线
# ---------------------------------------------------------------------------


def test_disable_removes_provider_capabilities(tmp_path):
    _write_plugin(tmp_path, SUMMARIZER)
    provider = object()
    manager, registry, _ = _make_manager(
        factories={"summarizer": lambda: {"provider": provider}}
    )
    manager.load_dir(tmp_path)
    manager.enable("summarizer")
    assert registry.providers_for("summarize") == [("summarizer", provider)]

    manager.disable("summarizer")
    assert registry.providers_for("summarize") == []
    assert registry.executor("summarizer") is None
    assert registry.manifest("summarizer") is None


# ---------- 安全审查回归：递归脱敏 + 扩展 hints ----------

def test_config_view_masks_nested_and_extended_hints(tmp_path):
    from whale_core.memory.store import InMemoryStore
    from whale_core.plugins.manager import PluginManager
    from whale_core.plugins.manifest import PluginManifest
    from whale_core.plugins.registry import PluginRegistry

    store = InMemoryStore()
    manager = PluginManager(PluginRegistry(), store)
    manager.load_manifest(PluginManifest(
        plugin_id="nest", name="n", version="1.0.0",
        actions=("ping",), action_risk={"ping": "read_only"},
    ))
    manager.set_config("nest", {
        "region": "us",
        "nested": {"api_key": "sk-NESTED"},
        "credentials": "sk-CREDS",
        "passwd": "hunter2",
        "auth_header": "Bearer x",
        "listy": [{"session_token": "abc"}, "plain"],
    })
    view = manager.config_view("nest")
    assert view["region"] == "us"
    assert view["nested"]["api_key"] == "***"
    assert view["credentials"] == "***"
    assert view["passwd"] == "***"
    assert view["auth_header"] == "***"
    assert view["listy"][0]["session_token"] == "***"
    assert view["listy"][1] == "plain"
    import json as _json
    assert "sk-NESTED" not in _json.dumps(view)
    assert "hunter2" not in _json.dumps(view)
