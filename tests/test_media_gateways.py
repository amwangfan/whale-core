"""2026-11-05 语音/视频生成网关（speech_gen / video_gen）单元测试。

与生图网关 image_gen.py 同形态：单文件收口「提示词/文本 + 模型选择」，
未知模型诚实报错并列可用清单；视频异步任务由网关内嵌 manager 托管。
"""
from __future__ import annotations

import pytest

from whale_core.memory.store import InMemoryStore
from whale_core.plugins.media_video import STATUS_PENDING
from whale_core.plugins.speech_gen import SpeechGenGateway
from whale_core.plugins.video_gen import VideoGenGateway


class _StubTTS:
    def __init__(self, voice=""):
        self.voice = voice
        self.calls = []

    def synthesize(self, text, *, voice="", rate=1.0):
        self.calls.append((text, voice, rate))
        return b"wav-bytes"


def _speech_gateway(**kw):
    providers = {"speech-2.8-hd": _StubTTS(voice="female-tianmei"),
                 "qwen-audio-3.0-tts-flash": _StubTTS()}
    defaults = dict(default_model="speech-2.8-hd")
    defaults.update(kw)
    return SpeechGenGateway(providers, **defaults), providers


def test_speech_default_model_and_default_voice():
    gw, providers = _speech_gateway()
    out = gw.synthesize("你好呀")
    assert out.model == "speech-2.8-hd"
    assert out.audio == b"wav-bytes"
    # 音色未指定 → 回执如实带 provider 的模型默认音色
    assert out.voice == "female-tianmei"
    assert providers["speech-2.8-hd"].calls == [("你好呀", "", 1.0)]


def test_speech_explicit_model_and_voice():
    gw, providers = _speech_gateway()
    out = gw.synthesize("哈喽", model="qwen-audio-3.0-tts-flash",
                        voice="longanfengyue", rate=1.2)
    assert out.model == "qwen-audio-3.0-tts-flash"
    assert out.voice == "longanfengyue"
    assert providers["qwen-audio-3.0-tts-flash"].calls == [("哈喽", "longanfengyue", 1.2)]
    assert providers["speech-2.8-hd"].calls == []


def test_speech_unknown_model_lists_available():
    gw, _ = _speech_gateway()
    with pytest.raises(ValueError) as ei:
        gw.synthesize("x", model="no-such")
    assert "未知语音模型" in str(ei.value)
    assert "speech-2.8-hd" in str(ei.value)


def test_speech_empty_text_rejected():
    gw, _ = _speech_gateway()
    with pytest.raises(ValueError):
        gw.synthesize("   ")


class _StubVideo:
    def __init__(self, name):
        self.name = name
        self.calls = []
        self._n = 0

    def submit(self, prompt, *, duration_s=5, size="1280x720"):
        self._n += 1
        self.calls.append((prompt, duration_s, size))
        return f"{self.name}-{self._n}"

    def poll(self, task_id):
        return STATUS_PENDING, "", ""

    def cancel(self, task_id):
        return True


def _video_gateway(**kw):
    providers = {"happyhorse-1.1-t2v": _StubVideo("hh"),
                 "MiniMax-Hailuo-2.3": _StubVideo("mm")}
    defaults = dict(default_model="happyhorse-1.1-t2v", store=InMemoryStore())
    defaults.update(kw)
    return VideoGenGateway(providers, **defaults), providers


def test_video_default_model_submit():
    gw, providers = _video_gateway()
    out = gw.generate("一只鲸鱼在云里游")
    assert out.model == "happyhorse-1.1-t2v"
    assert out.status == STATUS_PENDING and out.task_id == "hh-1"
    assert providers["happyhorse-1.1-t2v"].calls == [("一只鲸鱼在云里游", 5, "1280x720")]
    # 回执 JSON 视图带 task_id/status（qq_workflow 跟踪线程的解析契约）
    d = out.as_dict()
    assert d["task_id"] == "hh-1" and d["status"] == "pending"


def test_video_explicit_model_routes_and_persists():
    gw, providers = _video_gateway()
    out = gw.generate("海浪", model="MiniMax-Hailuo-2.3", duration_s=6,
                      size="1280x720")
    assert out.model == "MiniMax-Hailuo-2.3"
    assert providers["MiniMax-Hailuo-2.3"].calls == [("海浪", 6, "1280x720")]
    # 任务落库可由 manager 查回（状态持久化契约不变）
    task = gw.manager.poll(out.task_id)
    assert task.prompt == "海浪" and task.model == "MiniMax-Hailuo-2.3"


def test_video_unknown_model_lists_available():
    gw, _ = _video_gateway()
    with pytest.raises(ValueError) as ei:
        gw.generate("x", model="no-such-video")
    assert "未知视频模型" in str(ei.value)
    assert "happyhorse-1.1-t2v" in str(ei.value)


def test_gateway_rejects_bad_default_model():
    providers = {"a": _StubVideo("a")}
    with pytest.raises(ValueError):
        VideoGenGateway(providers, default_model="nope", store=InMemoryStore())
    with pytest.raises(ValueError):
        SpeechGenGateway({"a": _StubTTS()}, default_model="nope")


def test_capability_setup_routes_through_gateways(tmp_path, monkeypatch):
    """装配层端到端：generate_video/synthesize_speech 工具走网关，语义不变。"""
    import json
    from whale_core.plugins import capability_setup as cs
    from whale_core.plugins.registry import PluginRegistry
    from whale_core.core.runtime import ToolRegistry
    from whale_core.plugins import provider_http

    env = {"WHALE_MEDIA_BASE_URL": "http://localhost:1234/v1",
           "WHALE_MEDIA_API_KEY": "sk-test",
           "WHALE_MINIMAX_API_KEY": "mm-key"}
    registry, tools = PluginRegistry(), ToolRegistry()
    bundle = cs.configure_optional_capabilities(
        registry, tools, InMemoryStore(), env=env, artifact_dir=tmp_path)

    # 视频：工具提交 → 网关 → manager（stub 掉 provider 网络层）
    monkeypatch.setattr(provider_http, "open_request",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("network should be stubbed by model layer")))
    from whale_core.plugins.media_video import DashScopeHappyHorseVideoProvider
    monkeypatch.setattr(DashScopeHappyHorseVideoProvider, "submit",
                        lambda self, prompt, *, duration_s=5, size="1280x720": "vid-1")
    out = json.loads(tools.for_capability("media.video.generate")[0].handler(
        {"prompt": "鲸鱼", "model": "happyhorse-1.1-t2v"}))
    assert out["task_id"] == "vid-1" and out["status"] == "pending"
    assert out["model"] == "happyhorse-1.1-t2v"
    with pytest.raises(ValueError) as ei:
        tools.for_capability("media.video.generate")[0].handler(
            {"prompt": "x", "model": "no-such"})
    assert "未知视频模型" in str(ei.value)
    assert bundle.video_manager is not None  # 宿主跟踪线程仍拿得到 manager

    # 语音：工具合成 → 网关（stub 掉 minimax 网络层）
    from whale_core.plugins.minimax_media import MiniMaxTTSProvider
    monkeypatch.setattr(MiniMaxTTSProvider, "synthesize",
                        lambda self, text, *, voice="", rate=1.0: b"wav")
    out = json.loads(tools.for_capability("tts.synthesize")[0].handler(
        {"text": "你好"}))
    assert out["model"] and "artifact" in json.dumps(out) or out.get("url")
    with pytest.raises(ValueError) as ei:
        tools.for_capability("tts.synthesize")[0].handler(
            {"text": "x", "model": "no-such-tts"})
    assert "未知语音模型" in str(ei.value)
