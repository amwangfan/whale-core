"""profiles.py 三档装配测试（全离线）。"""

from __future__ import annotations

import pytest

from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.profiles import assemble_runtime


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        assemble_runtime("yolo")


def test_demo_profile_removed():
    """mock/demo 档已移除（owner 决策 2026-10-30）：不再存在。"""
    with pytest.raises(ValueError):
        assemble_runtime("demo")
    assert "demo" not in __import__("whale_core.profiles", fromlist=["x"]).PROFILE_NAMES


def test_local_memory_command_works():
    bundle = assemble_runtime("local", env={})
    result = bundle.orchestrator.handle_message("c", "记住：我怕辣")
    assert result.reply.startswith("好，我记住啦")


def test_cloud_profile_registers_model():
    bundle = assemble_runtime("cloud", env={"DEEPSEEK_API_KEY": "k-test"})
    from whale_core.plugins.model_provider import resolve_chat_model
    assert resolve_chat_model(bundle.registry) is not None
    assert any("llm.chat" in n or "已注册" in n for n in bundle.notes)


def test_cloud_without_key_notes_warning():
    bundle = assemble_runtime("cloud", env={})
    assert any("DEEPSEEK_API_KEY 未配置" in n for n in bundle.notes)


def test_cloud_with_search_key_mounts_tools():
    bundle = assemble_runtime("cloud", env={"DEEPSEEK_API_KEY": "k",
                                          "TAVILY_API_KEY": "t"})
    assert bundle.tools.get("search_web") is not None
    assert bundle.tools.get("fetch_url") is not None
    assert bundle.registry.manifest("web_search") is not None


def test_local_profile_loopback_no_key():
    bundle = assemble_runtime("local", env={})
    from whale_core.plugins.model_provider import resolve_chat_model
    model = resolve_chat_model(bundle.registry)
    assert model is not None
    assert model.base_url.startswith("http://127.0.0.1")
    assert model.api_key == ""


def test_local_profile_custom_endpoint():
    bundle = assemble_runtime("local", env={
        "LOCAL_MODEL_BASE_URL": "http://192.168.1.5:8000/v1",
        "LOCAL_MODEL_NAME": "qwen3.8",
    })
    from whale_core.plugins.model_provider import resolve_chat_model
    model = resolve_chat_model(bundle.registry)
    assert model.base_url == "http://192.168.1.5:8000/v1"
    assert model.model == "qwen3.8"


def test_config_dir_persists_store(tmp_path):
    bundle = assemble_runtime("local", env={}, config_dir=str(tmp_path))
    assert isinstance(bundle.store, SqliteMemoryStore)
    assert (tmp_path / "memory.sqlite3").exists()
