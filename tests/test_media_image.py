"""PR-12 media.image.generate 能力契约：解析 / 校验 / 错误分类 / base_url 策略 /
Mock 确定性 / 注册形状。

全部离线：HTTP 层用 monkeypatch 替换 urllib.request.urlopen。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from whale_core.plugins.manifest import PluginKind
from whale_core.plugins.media_image import (
    IMAGE_GENERATE,
    PLUGIN_ID,
    ImageError,
    ImageTaskResult,
    OpenAIImagesProvider,
    register_image_gen,
)
from whale_core.plugins.registry import PluginRegistry
from _doubles import MockImageProvider


# ---------- 离线 HTTP 替身 ----------


def _fake_urlopen(monkeypatch, scripted):
    """scripted: dict（JSON 响应）或 Exception 实例。返回调用记录。"""
    calls = {"n": 0, "requests": []}

    def fake(req, *, timeout=None):
        calls["n"] += 1
        calls["requests"].append(req)
        if isinstance(scripted, Exception):
            raise scripted
        return io.BytesIO(json.dumps(scripted).encode())

    monkeypatch.setattr("whale_core.plugins.media_image.open_provider_request", fake)
    return calls


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "m", {}, None)


def _provider():
    return OpenAIImagesProvider(api_key="sk-secret-TESTKEY")


# ---------- 响应解析 ----------


def test_parses_url_response(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {"data": [{"url": "https://img.example/x.png"}]})
    result = _provider().generate("画一只鲸")
    assert calls["n"] == 1
    assert result.status == "succeeded"
    assert result.images == ("https://img.example/x.png",)
    assert result.error == "" and result.task_id


def test_b64_json_becomes_data_uri(monkeypatch):
    _fake_urlopen(monkeypatch, {"data": [{"b64_json": "QUJD"}]})
    result = _provider().generate("画一只鲸")
    assert result.images == ("data:image/png;base64,QUJD",)


def test_missing_url_and_b64_raises_bad_response(monkeypatch):
    _fake_urlopen(monkeypatch, {"data": [{}]})
    with pytest.raises(ImageError) as ei:
        _provider().generate("画一只鲸")
    assert ei.value.kind == "bad_response"


def test_unexpected_shape_raises_bad_response(monkeypatch):
    _fake_urlopen(monkeypatch, {"unexpected": True})
    with pytest.raises(ImageError) as ei:
        _provider().generate("画一只鲸")
    assert ei.value.kind == "bad_response"


# ---------- 请求校验（prompt / size / n） ----------


@pytest.mark.parametrize("provider_factory", [_provider, MockImageProvider])
@pytest.mark.parametrize("prompt", ["", "   "])
def test_empty_prompt_rejected(provider_factory, prompt):
    with pytest.raises(ValueError):
        provider_factory().generate(prompt)


def test_prompt_truncated_to_4000(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {"data": [{"url": "https://img.example/x.png"}]})
    _provider().generate("鲸" * 5000)
    sent = json.loads(calls["requests"][0].data.decode())
    assert sent["prompt"] == "鲸" * 4000
    assert len(sent["prompt"]) == 4000


@pytest.mark.parametrize("size", ["512x512", "2048x2048", "1024", "large", ""])
def test_size_whitelist_rejects_unknown(size):
    with pytest.raises(ValueError):
        _provider().generate("画一只鲸", size=size)
    with pytest.raises(ValueError):
        MockImageProvider().generate("画一只鲸", size=size)


@pytest.mark.parametrize("size", ["1024x1024", "1792x1024", "1024x1792"])
def test_size_whitelist_accepts_known(size):
    result = MockImageProvider().generate("画一只鲸", size=size)
    assert result.status == "succeeded" and len(result.images) == 1


@pytest.mark.parametrize("n,expected", [(0, 1), (-3, 1), (1, 1), (4, 4), (99, 4)])
def test_n_clamped_to_1_4(monkeypatch, n, expected):
    calls = _fake_urlopen(
        monkeypatch,
        {"data": [{"url": f"https://img.example/{i}.png"} for i in range(4)]},
    )
    _provider().generate("画一只鲸", n=n)
    sent = json.loads(calls["requests"][0].data.decode())
    assert sent["n"] == expected
    assert len(MockImageProvider().generate("画一只鲸", n=n).images) == expected


# ---------- 错误分类（401/429/5xx/网络）与脱敏 ----------


def test_401_classified_auth(monkeypatch):
    _fake_urlopen(monkeypatch, _http_error(401))
    with pytest.raises(ImageError) as ei:
        _provider().generate("画一只鲸")
    assert ei.value.kind == "auth" and ei.value.status == 401


def test_429_classified_quota(monkeypatch):
    _fake_urlopen(monkeypatch, _http_error(429))
    with pytest.raises(ImageError) as ei:
        _provider().generate("画一只鲸")
    assert ei.value.kind == "quota" and ei.value.status == 429


@pytest.mark.parametrize("code", [500, 502, 503])
def test_5xx_classified_server(monkeypatch, code):
    _fake_urlopen(monkeypatch, _http_error(code))
    with pytest.raises(ImageError) as ei:
        _provider().generate("画一只鲸")
    assert ei.value.kind == "server" and ei.value.status == code


def test_network_error_classified(monkeypatch):
    _fake_urlopen(monkeypatch, urllib.error.URLError("boom"))
    with pytest.raises(ImageError) as ei:
        _provider().generate("画一只鲸")
    assert ei.value.kind == "network"


def test_missing_key_is_config_error():
    provider = OpenAIImagesProvider(api_key="")
    with pytest.raises(ImageError) as ei:
        provider.generate("画一只鲸")
    assert ei.value.kind == "config"


@pytest.mark.parametrize(
    "scripted",
    [
        _http_error(401),
        _http_error(429),
        _http_error(503),
        urllib.error.URLError("boom"),
    ],
)
def test_error_message_never_contains_key(monkeypatch, scripted):
    _fake_urlopen(monkeypatch, scripted)
    with pytest.raises(ImageError) as ei:
        _provider().generate("画一只鲸")
    assert "sk-secret-TESTKEY" not in str(ei.value)
    assert "sk-secret-TESTKEY" not in repr(ei.value)


# ---------- base_url 策略（公网 https；回环/私网 http 放行） ----------


@pytest.mark.parametrize(
    "url", ["http://example.com", "http://api.openai.com", "ftp://x"]
)
def test_base_url_public_http_rejected(url):
    with pytest.raises(ValueError):
        OpenAIImagesProvider(api_key="k", base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://127.0.0.1:8080",
        "http://localhost:9000",
        "http://192.168.1.10:8000",
    ],
)
def test_base_url_https_or_private_http_allowed(url):
    provider = OpenAIImagesProvider(api_key="k", base_url=url)
    assert provider.base_url == url


# ---------- Mock：确定性 + 标注 ----------


def test_mock_deterministic_and_canned_svg():
    a = MockImageProvider().generate("画一只鲸", size="1024x1792", n=2)
    b = MockImageProvider().generate("画一只鲸", size="1024x1792", n=2)
    assert a == b
    assert isinstance(a, ImageTaskResult)
    assert all(img.startswith("data:image/svg+xml,") for img in a.images)


def test_mock_is_labelled():
    assert "mock" in MockImageProvider.__doc__.lower()


# ---------- 注册形状 + providers_for 解析 ----------


def test_register_requires_provider():
    """mock 默认已移除（owner 决策）：缺 provider 诚实 ValueError。"""
    registry = PluginRegistry()
    with pytest.raises(ValueError):
        register_image_gen(registry)
    assert registry.manifest(PLUGIN_ID) is None


def test_register_shape_and_providers_for_resolution():
    registry = PluginRegistry()
    custom = OpenAIImagesProvider(api_key="k")
    returned = register_image_gen(registry, provider=custom)
    assert returned is custom
    found = registry.providers_for(IMAGE_GENERATE, kind=PluginKind.MODEL)
    assert found == [(PLUGIN_ID, custom)]
    assert registry.providers_for(IMAGE_GENERATE) == [(PLUGIN_ID, custom)]
    assert registry.providers_for("llm.chat") == []


def test_current_image_default_and_standard_sizes(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {"data": [{"b64_json": "QUJD"}]})
    provider = OpenAIImagesProvider(api_key="fixture")
    provider.generate("sea", size="1536x1024")
    payload = json.loads(calls["requests"][0].data)
    assert payload["model"] == "gpt-image-2"
    assert payload["size"] == "1536x1024"
