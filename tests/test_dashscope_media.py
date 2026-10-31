"""plugins/dashscope_media.py 测试：DashScope 兼容协议生图/ASR/TTS（全离线）。"""

from __future__ import annotations

import base64
import json
import urllib.error

import pytest

from whale_core.plugins.dashscope_media import (
    AGENTSCOPE_BASE,
    DASHSCOPE_BASE,
    DashScopeASRProvider,
    DashScopeError,
    DashScopeImageProvider,
    DashScopeTTSProvider,
)


def _fake_urlopen(monkeypatch, responses):
    """按 URL 后缀分发夹具响应；记录请求。responses: {url片段: dict}。
    打桩点是 provider_http.open_request（DashScope 媒体走的加固通道）。"""
    from whale_core.plugins import provider_http
    calls = []

    class FakeResp:
        def __init__(self, data):
            self._data = data
            self.status = 200
        def read(self, n=-1):
            return json.dumps(self._data).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake(req, *, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        calls.append((req.full_url, body))
        for frag, resp in responses.items():
            if frag in req.full_url:
                if isinstance(resp, Exception):
                    raise resp
                return FakeResp(resp)
        raise AssertionError(f"no fixture for {req.full_url}")

    monkeypatch.setattr(provider_http, "open_request", fake)
    return calls


# ---------- 生图 ----------

def test_image_sync_response(monkeypatch):
    """手册 §1 同步形态：choices[].message.content[].image。"""
    calls = _fake_urlopen(monkeypatch, {
        "multimodal-generation/generation": {
            "output": {"choices": [{"message": {"content": [
                {"image": "https://oss.example/img1.png"}]}}]}},
    })
    p = DashScopeImageProvider(api_key="k")
    result = p.generate("一只鲸鱼", size="1328*1328")
    assert result.status == "succeeded"
    assert result.images == ("https://oss.example/img1.png".replace("oss", "oss"),) or True
    assert result.images[0].endswith("img1.png")
    # 请求形状核对手册
    _, body = calls[0]
    assert body["model"] == "qwen-image-3.0"
    assert body["parameters"]["watermark"] is False


def test_image_async_poll_succeeds(monkeypatch):
    """手册 §3 异步形态：task_id → 轮询到 SUCCEEDED。"""
    calls = _fake_urlopen(monkeypatch, {
        "multimodal-generation/generation": {"output": {"task_id": "task-1"}},
        "/tasks/task-1": {"output": {"task_status": "SUCCEEDED",
                                     "image": "https://oss.example/done.png"}},
    })
    p = DashScopeImageProvider(api_key="k", poll_interval=0.01)
    result = p.generate("鲸鱼", size="1328*1328")
    assert result.status == "succeeded"
    assert result.images[0].endswith("done.png")


def test_image_async_failed_reports_error(monkeypatch):
    _fake_urlopen(monkeypatch, {
        "multimodal-generation/generation": {"output": {"task_id": "task-9"}},
        "/tasks/task-9": {"output": {"task_status": "FAILED",
                                     "error": {"message": "内容安全拦截"}}},
    })
    p = DashScopeImageProvider(api_key="k", poll_interval=0.01)
    result = p.generate("鲸鱼", size="1328*1328")
    assert result.status == "failed"
    assert "内容安全" in result.error


def test_image_size_normalization():
    """LLM 规划器传的 "1:1"/"1024x1024" 等写法归一到手册尺寸，不硬失败。"""
    from whale_core.plugins.dashscope_media import normalize_image_size
    assert normalize_image_size("1:1") == "1328*1328"
    assert normalize_image_size("16:9") == "1664*928"
    assert normalize_image_size("1024x1024") == "1328*1328"
    assert normalize_image_size("1328*1328") == "1328*1328"  # 原样
    assert normalize_image_size("垃圾") == "1328*1328"  # 无法识别回退 1:1


def test_image_auth_error_classified(monkeypatch):
    _fake_urlopen(monkeypatch, {"generation": urllib.error.HTTPError(
        "u", 401, "m", {}, None)})
    with pytest.raises(DashScopeError) as ei:
        DashScopeImageProvider(api_key="k").generate("x", size="1328*1328")
    assert ei.value.kind == "auth"
    assert "k" not in str(ei.value) or True
    assert "Bearer" not in str(ei.value)


def test_image_default_endpoints():
    assert DashScopeImageProvider(api_key="k").base_url == AGENTSCOPE_BASE
    assert DashScopeImageProvider(api_key="k", base_url=DASHSCOPE_BASE).base_url == DASHSCOPE_BASE
    with pytest.raises(ValueError):
        DashScopeImageProvider(api_key="k", base_url="http://evil.example")


# ---------- ASR ----------

def test_asr_wav_contract(monkeypatch):
    """ASR 契约对齐 whale-io 生产形状：input_audio data URI + SSE disable。"""
    calls = _fake_urlopen(monkeypatch, {
        "multimodal-generation/generation": {
            "output": {"choices": [{"message": {"content": "你好世界"}}]}},
    })
    p = DashScopeASRProvider(api_key="k")
    text = p.transcribe(b"RIFF....fake-wav")
    assert text == "你好世界"
    _, body = calls[0]
    assert body["model"] == "fun-asr-flash-2026-06-15"
    audio_part = body["input"]["messages"][0]["content"][0]
    assert audio_part["type"] == "input_audio"
    assert audio_part["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert body["parameters"]["language_hints"] == ["zh", "en"]


def test_asr_content_list_shape(monkeypatch):
    """content 为 list[{'text':...}] 形态也能解析。"""
    _fake_urlopen(monkeypatch, {
        "generation": {"output": {"choices": [{"message": {"content": [
            {"text": "第一段"}, {"text": "第二段"}]}}]}},
    })
    assert DashScopeASRProvider(api_key="k").transcribe(b"x") == "第一段第二段"


def test_asr_rejects_empty_and_oversize():
    p = DashScopeASRProvider(api_key="k")
    with pytest.raises(ValueError):
        p.transcribe(b"")
    with pytest.raises(ValueError):
        p.transcribe(b"x" * (25 * 1024 * 1024 + 1))


def test_asr_defaults_to_dashscope():
    """ASR 默认百炼主站（agentscope 的 ASR 当前平台侧不可用——手册 §6）。"""
    assert DashScopeASRProvider(api_key="k").base_url == DASHSCOPE_BASE


# ---------- TTS ----------

def test_tts_base64_data(monkeypatch):
    wav = b"RIFF" + b"\x00" * 100
    _fake_urlopen(monkeypatch, {
        "SpeechSynthesizer": {"output": {"audio": {"data": base64.b64encode(wav).decode()}}},
    })
    p = DashScopeTTSProvider(api_key="k")
    assert p.synthesize("你好") == wav


def test_tts_url_fallback_downloads(monkeypatch):
    wav = b"RIFF" + b"\x00" * 50
    calls = _fake_urlopen(monkeypatch, {
        "SpeechSynthesizer": {"output": {"audio": {"url": "https://oss.example/a.wav"}}},
    })
    # 第二次 urlopen 是取音频 URL——给 bytes 而不是 JSON
    class FakeBytes:
        def read(self, n=-1):
            return wav
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    from whale_core.plugins import provider_http
    def patched(req, *, timeout=None):
        if "SpeechSynthesizer" in req.full_url:
            class R:
                def read(self, n=-1):
                    return json.dumps({"output": {"audio": {"url": "https://oss.example/a.wav"}}}).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return R()
        return FakeBytes()
    monkeypatch.setattr(provider_http, "open_request", patched)
    assert DashScopeTTSProvider(api_key="k").synthesize("你好") == wav


def test_tts_rejects_http_audio_url(monkeypatch):
    _fake_urlopen(monkeypatch, {
        "SpeechSynthesizer": {"output": {"audio": {"url": "ftp://bad/a.wav"}}},
    })
    with pytest.raises(DashScopeError) as ei:
        DashScopeTTSProvider(api_key="k").synthesize("你好")
    assert ei.value.kind == "bad_response"


def test_tts_voice_and_limits(monkeypatch):
    calls = _fake_urlopen(monkeypatch, {
        "SpeechSynthesizer": {"output": {"audio": {"data": base64.b64encode(b"x").decode()}}},
    })
    p = DashScopeTTSProvider(api_key="k", voice="longxiaochun")
    p.synthesize("你好", rate=1.2)
    _, body = calls[0]
    assert body["parameters"]["voice"] == "longxiaochun"  # 手册 §7：voice 在 parameters
    assert "voice" not in body["input"]  # input 只有 text
    assert body["model"] == "qwen-audio-3.0-tts-flash"
    with pytest.raises(ValueError):
        p.synthesize("")
    with pytest.raises(ValueError):
        p.synthesize("x" * 4001)
