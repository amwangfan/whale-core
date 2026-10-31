"""plugins/loader.py 拆分包加载器测试（全离线）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whale_core.plugins.loader import (
    PluginLoadError,
    load_plugin_dir,
    load_plugin_module,
    read_plugin_manifest,
)
from whale_core.plugins.registry import PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SEARCH_DIR = REPO_ROOT / "plugins" / "search_web"
WEQ_DIR = REPO_ROOT / "plugins" / "weq_channel"


# ---------- manifest 读取 ----------

def test_read_manifest_search_web():
    manifest = read_plugin_manifest(SEARCH_DIR)
    assert manifest.plugin_id == "search_web"
    assert manifest.resolved_kind.value == "output"
    assert manifest.risk_of("search").value == "read_only"


def test_read_manifest_weq_channel():
    manifest = read_plugin_manifest(WEQ_DIR)
    assert manifest.plugin_id == "qq_weq"
    assert manifest.resolved_kind.value == "channel"
    assert "qq.message" in manifest.inputs


def test_manifest_missing_file(tmp_path):
    with pytest.raises(PluginLoadError):
        read_plugin_manifest(tmp_path)


def test_manifest_bad_json(tmp_path):
    (tmp_path / "plugin.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(PluginLoadError):
        read_plugin_manifest(tmp_path)


def test_manifest_incompatible_api_version(tmp_path):
    (tmp_path / "plugin.json").write_text(json.dumps({
        "plugin_id": "x", "api_version": "99", "actions": ["a"],
        "action_risk": {"a": "read_only"}}), encoding="utf-8")
    with pytest.raises(PluginLoadError):
        read_plugin_manifest(tmp_path)


# ---------- 模块加载 ----------

def test_load_package_module():
    module = load_plugin_module(SEARCH_DIR)  # 含 __init__.py → 按包加载
    assert callable(module.register)


def test_load_plain_file_module(tmp_path):
    (tmp_path / "main.py").write_text("VALUE = 42\n", encoding="utf-8")
    module = load_plugin_module(tmp_path)
    assert module.VALUE == 42


def test_load_missing_entrypoint(tmp_path):
    with pytest.raises(PluginLoadError):
        load_plugin_module(tmp_path)


# ---------- 目录加载 + 注册 ----------

def test_load_search_web_registers_manifest(tmp_path):
    """真实拆分包经加载器注册：内含的 web_search/web_fetch 两个插件进注册表
    （包 plugin.json 是外层描述；实际能力是内层注册项）。搜索 provider 走
    env 回退，不触网。"""
    registry = PluginRegistry()
    manifest = load_plugin_dir(registry, SEARCH_DIR, env={})
    assert manifest.plugin_id == "search_web"
    assert registry.manifest("web_search") is not None
    assert registry.manifest("web_fetch") is not None
    # 无 key 时 provider 是 DDG 回退档（诚实标注，非 mock）
    assert registry.executor("web_search") is not None


def test_load_dir_register_failure_raises(tmp_path):
    (tmp_path / "plugin.json").write_text(json.dumps({
        "plugin_id": "bad", "actions": ["a"], "action_risk": {"a": "read_only"}}),
        encoding="utf-8")
    (tmp_path / "main.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    with pytest.raises(PluginLoadError):
        load_plugin_dir(PluginRegistry(), tmp_path)


def test_load_dir_no_register_function(tmp_path):
    (tmp_path / "plugin.json").write_text(json.dumps({
        "plugin_id": "noimpl", "actions": ["a"], "action_risk": {"a": "read_only"}}),
        encoding="utf-8")
    (tmp_path / "main.py").write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(PluginLoadError):
        load_plugin_dir(PluginRegistry(), tmp_path)
