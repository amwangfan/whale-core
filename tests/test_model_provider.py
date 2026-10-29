"""PR-4 模型 provider 化：DeepSeekCompanion 错误分类/重试/http 放宽 + 能力注册解析。

全部离线：HTTP 层用 monkeypatch 替换 urllib.request.urlopen。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from whale_core.core.deepseek import DeepSeekCompanion, ModelError
from whale_core.plugins.model_provider import (
    register_openai_compatible,
    require_chat_model,
    resolve_chat_model,
)
from whale_core.plugins.registry import PluginRegistry


# ---------- http/https 基址策略（R3 放宽：回环/私网 http 放行，公网 http 仍拒） ----------

@pytest.mark.parametrize("url,ok", [
    ("https://api.deepseek.com", True),
    ("https://platform.agentscope.io/v1", True),
    ("http://example.com", False),          # 公网 http 拒绝
    ("http://api.deepseek.com", False),
    ("http://127.0.0.1:11434", True),       # 回环本地模型
    ("http://localhost:8080", True),
    ("http://192.168.1.10:8000", True),     # 私网
    ("http://10.0.0.2", True),
    ("http://172.16.0.5", True),
    ("http://172.31.255.1", True),
    ("http://172.32.0.1", False),           # 172.32 已出私网段
    ("http://[::1]:11434", True),
    ("ftp://127.0.0.1", False),
    ("not-a-url", False),
])
def test_base_url_policy(url, ok):
    if ok:
        DeepSeekCompanion(api_key="k", base_url=url)
    else:
        with pytest.raises(ValueError):
            DeepSeekCompanion(api_key="k", base_url=url)


# ---------- 错误分类与重试 ----------

def _fake_urlopen(monkeypatch, script):
    """script: 列表，元素为 HTTPError/URLError 实例或 'ok'。返回调用次数。"""
    calls = {"n": 0, "timeouts": []}
    def fake(req, *, timeout=None):
        calls["n"] += 1
        calls["timeouts"].append(timeout)
        item = script[min(calls["n"] - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        payload = json.dumps({"choices": [{"message": {"content": "回了"}}]}).encode()
        return io.BytesIO(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "m", {}, None)


def test_auth_error_no_retry(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [_http_error(401)])
    d = DeepSeekCompanion(api_key="k", base_url="https://api.deepseek.com")
    with pytest.raises(ModelError) as ei:
        d.reply("hi")
    assert ei.value.kind == "auth" and not ei.value.retryable
    assert calls["n"] == 1


def test_rate_limit_retries_once_then_succeeds(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [_http_error(429), "ok"])
    monkeypatch.setattr("time.sleep", lambda s: None)
    d = DeepSeekCompanion(api_key="k", base_url="https://api.deepseek.com")
    assert d.reply("hi") == "回了"
    assert calls["n"] == 2


def test_server_error_retry_exhaustion_raises(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [_http_error(503), _http_error(503)])
    monkeypatch.setattr("time.sleep", lambda s: None)
    d = DeepSeekCompanion(api_key="k", base_url="https://api.deepseek.com")
    with pytest.raises(ModelError) as ei:
        d.reply("hi")
    assert ei.value.kind == "server"
    assert calls["n"] == 2


def test_network_error_retryable(monkeypatch):
    calls = _fake_urlopen(monkeypatch, [urllib.error.URLError("boom")])
    monkeypatch.setattr("time.sleep", lambda s: None)
    d = DeepSeekCompanion(api_key="k", base_url="https://api.deepseek.com")
    with pytest.raises(ModelError) as ei:
        d.reply("hi")
    assert ei.value.kind == "network" and ei.value.retryable
    assert calls["n"] == 2


def test_bad_response_shape(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, *, timeout=None: io.BytesIO(b'{"choices": []}'))
    d = DeepSeekCompanion(api_key="k", base_url="https://api.deepseek.com")
    with pytest.raises(ModelError) as ei:
        d.reply("hi")
    assert ei.value.kind == "bad_response"


def test_missing_key_is_config_error():
    d = DeepSeekCompanion(api_key="", base_url="https://api.deepseek.com")
    with pytest.raises(ModelError) as ei:
        d.reply("hi")
    assert ei.value.kind == "config"


def test_local_endpoint_allows_empty_key(monkeypatch):
    """local 配置档：回环 http 端点（如 Ollama）无 key 也可调用。"""
    calls = _fake_urlopen(monkeypatch, ["ok"])
    d = DeepSeekCompanion(api_key="", base_url="http://127.0.0.1:11434/v1")
    assert d.reply("hi") == "回了"
    assert calls["n"] == 1


def test_timeout_flows_through(monkeypatch):
    calls = _fake_urlopen(monkeypatch, ["ok"])
    d = DeepSeekCompanion(api_key="k", base_url="https://api.deepseek.com", timeout=7.5)
    d.reply("hi")
    assert calls["timeouts"] == [7.5]


def test_info_never_contains_key():
    d = DeepSeekCompanion(api_key="super-secret-key", base_url="https://api.deepseek.com")
    info = d.info()
    assert info["configured"] is True
    assert "super-secret-key" not in json.dumps(info)


# ---------- 能力注册与解析 ----------

def test_register_and_resolve_chat_model():
    registry = PluginRegistry()
    companion = register_openai_compatible(registry, api_key="k")
    assert resolve_chat_model(registry) is companion
    manifest = registry.manifest("deepseek")
    assert manifest is not None and manifest.resolved_kind.value == "model"
    assert "llm.chat" in manifest.provides


def test_resolve_empty_returns_none_and_require_raises():
    registry = PluginRegistry()
    assert resolve_chat_model(registry) is None
    with pytest.raises(LookupError):
        require_chat_model(registry)


def test_register_custom_endpoint():
    registry = PluginRegistry()
    companion = register_openai_compatible(
        registry, plugin_id="local-ollama", name="本地 Ollama",
        api_key="x", base_url="http://127.0.0.1:11434", model="qwen3.8",
    )
    assert companion.base_url == "http://127.0.0.1:11434"
    assert resolve_chat_model(registry) is companion
