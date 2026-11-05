"""2026-11-05 参考图生图 + 生图网关（多参考图、默认 qwen、模型可选）单元测试。"""
from __future__ import annotations

import base64

from whale_core.plugins.media_image import ImageTaskResult, ReferenceImage
from whale_core.plugins.minimax_media import MiniMaxImageProvider
from whale_core.plugins.dashscope_media import DashScopeImageProvider
from whale_core.plugins.image_gen import (
    ImageGenGateway, fetch_reference_image, find_reference_images,
    sniff_image_mime, MAX_REFERENCES)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
JPG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////"
    "////////////////////////////2wBDAf//////////////////////////"
    "////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEA"
    "AAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQAC"
    "EQMRAD8AnwB//9k=")


def _ref(data=PNG_1PX, mime="image/png"):
    return ReferenceImage(data=data, mime=mime)


class _FakeMiniMax(MiniMaxImageProvider):
    def __init__(self):
        super().__init__(api_key="k", model="image-01")
        self.last_body = None

    def _request(self, path, body, **kw):
        self.last_body = body
        return {"data": {"image_urls": ["https://example.com/x.png"]},
                "base_resp": {"status_code": 0}}


class _FakeDash(DashScopeImageProvider):
    def __init__(self):
        super().__init__(api_key="k", model="qwen-image-3.0")
        self.last_body = None

    def _post(self, path, body, **kw):
        self.last_body = body
        return {"output": {"choices": [{"message": {"content": [
            {"image": "https://example.com/y.png"}]}}]}}


# ---- provider 层：references 入参 ----

def test_minimax_plain_when_no_reference():
    p = _FakeMiniMax()
    p.generate("一只鲸鱼", size="1024x1024")
    assert "subject_reference" not in p.last_body


def test_minimax_subject_reference_uses_first_of_many():
    """MiniMax subject_reference 只认一张：多参考图取首张。"""
    p = _FakeMiniMax()
    p.generate("娜娜奇", size="1024x1024",
               references=[_ref(PNG_1PX, "image/png"), _ref(JPG_1PX, "image/jpeg")])
    refs = p.last_body["subject_reference"]
    assert len(refs) == 1
    assert refs[0]["type"] == "character"
    assert refs[0]["image_file"].startswith("data:image/png;base64,")
    assert base64.b64decode(refs[0]["image_file"].split(",", 1)[1]) == PNG_1PX


def test_dashscope_all_references_prepended():
    """qwen-image-3.0：多张参考图全部作为 image 件前置。"""
    p = _FakeDash()
    p.generate("米蒂", references=[_ref(PNG_1PX, "image/png"),
                                   _ref(JPG_1PX, "image/jpeg")])
    content = p.last_body["input"]["messages"][0]["content"]
    assert content[0]["image"].startswith("data:image/png;base64,")
    assert content[1]["image"].startswith("data:image/jpeg;base64,")
    assert content[2] == {"text": "米蒂"}
    # 无参考图：纯文本件
    p.generate("米蒂")
    content = p.last_body["input"]["messages"][0]["content"]
    assert content == [{"text": "米蒂"}]


def test_providers_declare_supports_references():
    assert DashScopeImageProvider.supports_references is True
    assert MiniMaxImageProvider.supports_references is True


# ---- 采集层：魔数嗅探 + 搜索凑多张 ----

def test_sniff_image_mime():
    assert sniff_image_mime(PNG_1PX) == "image/png"
    assert sniff_image_mime(JPG_1PX) == "image/jpeg"
    assert sniff_image_mime(b"GIF89a....") == "image/gif"
    assert sniff_image_mime(b"RIFFxxxxWEBPvp8") == "image/webp"
    assert sniff_image_mime(b"<html>nope</html>") == ""


def test_find_reference_images_collects_up_to_limit():
    ok = _ref()
    calls = []

    def fetcher(url):
        calls.append(url)
        return ok if "good" in url else None

    urls = ["https://a/bad1", "https://a/good1", "https://a/bad2",
            "https://a/good2", "https://a/good3", "https://a/good4"]
    refs = find_reference_images("q", search_images=lambda q, limit: urls,
                                 fetcher=fetcher, limit=3)
    assert len(refs) == 3
    # 凑够 3 张即停，不再下载后面的
    assert calls == urls[:5]


def test_find_reference_images_search_failure_returns_empty():
    def boom(q, limit):
        raise RuntimeError("search down")
    assert find_reference_images("q", search_images=boom) == []


# ---- 网关层：模型分派 + 参考图语义 ----

class _StubProvider:
    """不声明 supports_references 的 provider（OpenAI 档行为）。"""
    def __init__(self):
        self.calls = []

    def generate(self, prompt, *, size="1024x1024", n=1):
        self.calls.append((prompt, size, n))
        return ImageTaskResult(task_id="t", status="succeeded",
                               images=("https://example.com/z.png",))


class _RefStubProvider(_StubProvider):
    supports_references = True

    def generate(self, prompt, *, size="1328*1328", n=1, references=()):
        self.calls.append((prompt, size, n, list(references)))
        return ImageTaskResult(task_id="t", status="succeeded",
                               images=("https://example.com/z.png",))


def _gateway(**kw):
    providers = {"qwen-image-3.0": _RefStubProvider(), "dall-e-3": _StubProvider()}
    defaults = dict(default_model="qwen-image-3.0",
                    image_search=lambda q, limit: ["https://a/good"],
                    fetcher=lambda url, **k: _ref())
    defaults.update(kw)
    return ImageGenGateway(providers, **defaults), providers


def test_gateway_default_model_and_plain_generation():
    gw, providers = _gateway()
    out = gw.generate("一只猫")
    assert out.model == "qwen-image-3.0"
    assert out.reference_used is False and out.reference_count == 0
    assert out.note == ""
    assert providers["qwen-image-3.0"].calls[0][3] == []


def test_gateway_unknown_model_lists_available():
    gw, _ = _gateway()
    try:
        gw.generate("x", model="nope")
    except ValueError as exc:
        assert "nope" in str(exc) and "qwen-image-3.0" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_gateway_reference_query_collects_multiple():
    gw, providers = _gateway(
        image_search=lambda q, limit: ["https://a/1", "https://a/2", "https://a/3",
                                       "https://a/4", "https://a/5"])
    out = gw.generate("娜娜奇同人", reference_query="来自深渊 娜娜奇 官方图")
    assert out.reference_used is True
    assert out.reference_count == MAX_REFERENCES  # 最多 3 张
    assert len(providers["qwen-image-3.0"].calls[0][3]) == MAX_REFERENCES


def test_gateway_reference_urls_direct_and_search_topup():
    """reference_urls 直传先用上；不足 3 张时 reference_query 搜索补齐。"""
    seen = []

    def fetcher(url, **kw):
        seen.append(url)
        return _ref() if "good" in url else None

    gw, providers = _gateway(
        fetcher=fetcher,
        image_search=lambda q, limit: ["https://s/good1", "https://s/good2"])
    out = gw.generate("米蒂", reference_query="来自深渊 米蒂",
                      reference_urls=["https://x/good0", "https://x/bad"])
    assert out.reference_used is True and out.reference_count == 3
    assert seen[0] == "https://x/good0"  # 直传优先
    assert "https://s/good1" in seen and "https://s/good2" in seen


def test_gateway_no_reference_found_falls_back_plain():
    gw, providers = _gateway(fetcher=lambda url, **kw: None)
    out = gw.generate("娜娜奇", reference_query="来自深渊 娜娜奇")
    assert out.reference_used is False
    assert "没搜到可用参考图" in out.note
    assert providers["qwen-image-3.0"].calls[0][3] == []


def test_gateway_provider_without_reference_support_degrades():
    gw, providers = _gateway()
    out = gw.generate("画", model="dall-e-3", reference_query="某角色")
    assert out.reference_used is False
    assert "不支持参考图" in out.note


# ---- 装配层：端到端（截网络层）----

def test_capability_setup_reference_flow(monkeypatch):
    """reference_query 有图 → 带图生成；无图 → 纯文本（有图/没图两态）。

    走真实装配 + 真实 DashScopeImageProvider + 真实网关，只截最外层网络。
    """
    import json
    from whale_core.plugins import capability_setup as cs
    from whale_core.plugins.registry import PluginRegistry
    from whale_core.core.runtime import ToolRegistry
    from whale_core.memory.store import InMemoryStore
    from whale_core.plugins.dashscope_media import DashScopeImageProvider

    bodies = []

    def fake_post(self, path, body, **kw):
        bodies.append(body)
        return {"output": {"choices": [{"message": {"content": [
            {"image": "https://example.com/a.png"}]}}]}}

    monkeypatch.setattr(DashScopeImageProvider, "_post", fake_post)
    # 强制 dashscope 档（不论环境缺什么 key）
    monkeypatch.setattr(cs, "_resolve_capability_env",
                        lambda e, cap: (("https://example.invalid/v1", "k", "dashscope")
                                        if cap == "IMAGE" else ("", "", "")))
    monkeypatch.setattr(cs, "_tavily_image_search",
                        lambda env: (lambda q, limit: ["https://img/ref1",
                                                       "https://img/ref2"]))
    # 截参考图下载：第一张是 PNG、第二张坏图（凑不满也只带好的）
    import whale_core.plugins.image_gen as ig
    def fake_fetch(url, **kw):
        return _ref() if url.endswith("ref1") else None
    monkeypatch.setattr(ig, "fetch_reference_image", fake_fetch)

    registry = PluginRegistry()
    tools = ToolRegistry()
    cs.configure_optional_capabilities(registry, tools, InMemoryStore(), env={})
    gen = tools.get("generate_image").handler

    # 有参考图：content 首件是 image，默认模型 qwen
    out = json.loads(gen({"prompt": "娜娜奇", "reference_query": "来自深渊 娜娜奇"}))
    assert out["reference_used"] is True
    assert out["model"] == "qwen-image-3.0"
    assert out["reference_count"] == 1
    content = bodies[-1]["input"]["messages"][0]["content"]
    assert content[0]["image"].startswith("data:image/png;base64,")
    assert content[1]["text"].startswith("娜娜奇")

    # 没参考图：纯文本件 + 如实标注
    monkeypatch.setattr(cs, "_tavily_image_search", lambda env: None)
    registry2, tools2 = PluginRegistry(), ToolRegistry()
    cs.configure_optional_capabilities(registry2, tools2, InMemoryStore(), env={})
    gen2 = tools2.get("generate_image").handler
    out = json.loads(gen2({"prompt": "娜娜奇", "reference_query": "来自深渊"}))
    assert out["reference_used"] is False
    assert "没搜到可用参考图" in out["note"]
    content = bodies[-1]["input"]["messages"][0]["content"]
    assert all("image" not in part for part in content)
