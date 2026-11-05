"""2026-11-05 参考图生图（kimi 判定 → 搜参考图 → 带图生成）单元测试。"""
from __future__ import annotations

import base64

from whale_core.plugins.minimax_media import MiniMaxImageProvider
from whale_core.plugins.dashscope_media import DashScopeImageProvider

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


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


def test_minimax_plain_when_no_reference():
    p = _FakeMiniMax()
    p.generate("一只鲸鱼", size="1024x1024")
    assert "subject_reference" not in p.last_body


def test_minimax_subject_reference_with_reference():
    p = _FakeMiniMax()
    p.generate("娜娜奇", size="1024x1024", reference=PNG_1PX,
               reference_mime="image/png")
    ref = p.last_body["subject_reference"][0]
    assert ref["type"] == "character"
    assert ref["image_file"].startswith("data:image/png;base64,")
    assert base64.b64decode(ref["image_file"].split(",", 1)[1]) == PNG_1PX


def test_dashscope_image_part_prepended_with_reference():
    p = _FakeDash()
    p.generate("米蒂", reference=PNG_1PX, reference_mime="image/png")
    content = p.last_body["input"]["messages"][0]["content"]
    assert content[0]["image"].startswith("data:image/png;base64,")
    assert content[1] == {"text": "米蒂"}
    # 无参考图：纯文本件
    p.generate("米蒂")
    content = p.last_body["input"]["messages"][0]["content"]
    assert content == [{"text": "米蒂"}]


def test_capability_setup_reference_flow(monkeypatch):
    """reference_query 有图 → 带图生成；无图 → 纯文本（有图/没图两态）。

    走真实装配 + 真实 DashScopeImageProvider，只截网络层（_post 抓请求体）。
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
    registry = PluginRegistry()
    tools = ToolRegistry()
    cs.configure_optional_capabilities(registry, tools, InMemoryStore(), env={})
    gen = tools.get("generate_image").handler

    # 有参考图：content 首件是 image
    monkeypatch.setattr(cs, "_find_reference_image",
                        lambda q, **kw: (PNG_1PX, "image/png"))
    out = json.loads(gen({"prompt": "娜娜奇", "reference_query": "来自深渊 娜娜奇"}))
    assert out["reference_used"] is True
    content = bodies[-1]["input"]["messages"][0]["content"]
    assert content[0]["image"].startswith("data:image/png;base64,")
    assert content[1]["text"].startswith("娜娜奇")

    # 没参考图：纯文本件 + 如实标注
    monkeypatch.setattr(cs, "_find_reference_image", lambda q, **kw: None)
    out = json.loads(gen({"prompt": "娜娜奇", "reference_query": "来自深渊"}))
    assert out["reference_used"] is False
    assert "没搜到可用参考图" in out["note"]
    content = bodies[-1]["input"]["messages"][0]["content"]
    assert all("image" not in part for part in content)
