"""PR-10 ASR/TTS 能力契约 + provider 插件（roadmap §8 P1-1/2）。

全部离线：HTTP 层用 monkeypatch 替换 urllib.request.urlopen。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from whale_core.plugins.manifest import PluginKind
from whale_core.plugins.registry import PluginRegistry
from whale_core.plugins.speech import (
    ASR_TRANSCRIBE,
    TTS_SYNTHESIZE,
    MockASR,
    MockTTS,
    OpenAICompatibleASR,
    OpenAICompatibleTTS,
    SpeechError,
    register_speech,
)


# ---------- 测试工装：假 urlopen ----------

class _FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "application/json"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_urlopen(monkeypatch, response=None, *, error: Exception | None = None):
    """替换 urllib.request.urlopen，捕获 (request, timeout) 供断言。"""
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = req.data
        captured["timeout"] = timeout
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    """隔离宿主环境里的 OPENAI_API_KEY。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _asr_ok(monkeypatch, text: str = "你好，鲸鱼"):
    return _capture_urlopen(
        monkeypatch,
        _FakeResponse(json.dumps({"text": text}).encode("utf-8")),
    )


def _tts_ok(monkeypatch, audio: bytes = b"\xff\xfbFAKEAUDIO"):
    return _capture_urlopen(
        monkeypatch, _FakeResponse(audio, content_type="audio/mpeg"))


# ---------- ASR ----------

def test_asr_multipart_construction(monkeypatch):
    captured = _asr_ok(monkeypatch)
    asr = OpenAICompatibleASR(api_key="k-test", base_url="http://127.0.0.1:9000/v1")
    assert asr.transcribe(b"RIFF-fake-audio", lang="zh") == "你好，鲸鱼"

    assert captured["url"] == "http://127.0.0.1:9000/v1/audio/transcriptions"
    assert captured["method"] == "POST"
    content_type = captured["headers"]["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    body = captured["body"]
    assert body.startswith(b"--" + boundary)
    assert body.endswith(b"--" + boundary + b"--\r\n")
    # 字段：model / language / file（含 Content-Disposition 与音频字节）
    assert b'Content-Disposition: form-data; name="model"' in body
    assert b'Content-Disposition: form-data; name="language"' in body
    assert b'Content-Disposition: form-data; name="file"; filename=' in body
    assert b"RIFF-fake-audio" in body
    assert captured["headers"]["authorization"] == "Bearer k-test"


def test_asr_parses_text_and_sends_lang(monkeypatch):
    captured = _asr_ok(monkeypatch, text="transcribed words")
    asr = OpenAICompatibleASR(api_key="k", base_url="https://api.openai.com/v1",
                              model="whisper-9")
    assert asr.transcribe(b"\x00\x01", lang="en") == "transcribed words"
    assert b'name="model"\r\n\r\nwhisper-9' in captured["body"]
    assert b'name="language"\r\n\r\nen' in captured["body"]


def test_asr_rejects_oversized_audio(monkeypatch):
    _asr_ok(monkeypatch)  # 即便端点可用也应在本地拒绝
    asr = OpenAICompatibleASR(api_key="k", base_url="http://127.0.0.1:1/v1")
    with pytest.raises(ValueError, match="25MB|too large"):
        asr.transcribe(b"\x00" * (25 * 1024 * 1024 + 1))


# ---------- TTS ----------

def test_tts_returns_audio_bytes(monkeypatch):
    captured = _tts_ok(monkeypatch)
    tts = OpenAICompatibleTTS(api_key="k", base_url="https://api.openai.com/v1")
    audio = tts.synthesize("你好", voice="nova", rate=1.5)
    assert audio == b"\xff\xfbFAKEAUDIO"
    assert captured["url"] == "https://api.openai.com/v1/audio/speech"
    payload = json.loads(captured["body"].decode("utf-8"))
    assert payload == {"model": "tts-1", "input": "你好", "voice": "nova", "speed": 1.5}
    assert captured["headers"]["content-type"] == "application/json"


def test_tts_voice_empty_falls_back_to_default(monkeypatch):
    captured = _tts_ok(monkeypatch)
    tts = OpenAICompatibleTTS(api_key="k", voice="alloy")
    tts.synthesize("hi", voice="")
    assert json.loads(captured["body"].decode())["voice"] == "alloy"


@pytest.mark.parametrize("given,clamped", [(0.1, 0.25), (0.25, 0.25), (1.0, 1.0),
                                           (4.0, 4.0), (99.0, 4.0)])
def test_tts_rate_clamped(monkeypatch, given, clamped):
    captured = _tts_ok(monkeypatch)
    tts = OpenAICompatibleTTS(api_key="k")
    tts.synthesize("hi", rate=given)
    assert json.loads(captured["body"].decode())["speed"] == clamped


def test_tts_rejects_non_audio_content_type(monkeypatch):
    _capture_urlopen(monkeypatch, _FakeResponse(b"<html>nope</html>",
                                                content_type="text/html"))
    tts = OpenAICompatibleTTS(api_key="k")
    with pytest.raises(SpeechError) as ei:
        tts.synthesize("hi")
    assert ei.value.kind == "bad_response"


def test_tts_accepts_octet_stream(monkeypatch):
    _capture_urlopen(monkeypatch, _FakeResponse(b"\x00raw",
                                                content_type="application/octet-stream"))
    assert OpenAICompatibleTTS(api_key="k").synthesize("hi") == b"\x00raw"


def test_tts_rejects_overlong_text():
    tts = OpenAICompatibleTTS(api_key="k")
    with pytest.raises(ValueError, match="too long"):
        tts.synthesize("字" * 4001)


# ---------- 错误分类与凭据安全 ----------

@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (429, "quota"),
                                         (500, "server"), (503, "server")])
def test_http_error_classification(monkeypatch, status, kind):
    err = urllib.error.HTTPError("https://x", status, "boom", {}, None)
    _capture_urlopen(monkeypatch, error=err)
    asr = OpenAICompatibleASR(api_key="k")
    with pytest.raises(SpeechError) as ei:
        asr.transcribe(b"\x00")
    assert ei.value.kind == kind
    assert ei.value.status == status


def test_urlerror_classified_network(monkeypatch):
    _capture_urlopen(monkeypatch, error=urllib.error.URLError("conn refused"))
    tts = OpenAICompatibleTTS(api_key="k")
    with pytest.raises(SpeechError) as ei:
        tts.synthesize("hi")
    assert ei.value.kind == "network"


def test_bad_json_classified_bad_response(monkeypatch):
    _capture_urlopen(monkeypatch, _FakeResponse(b"not-json{{"))
    asr = OpenAICompatibleASR(api_key="k")
    with pytest.raises(SpeechError) as ei:
        asr.transcribe(b"\x00")
    assert ei.value.kind == "bad_response"


def test_error_text_never_contains_api_key(monkeypatch):
    secret = "sk-super-secret-0123456789"
    err = urllib.error.HTTPError("https://x", 401, f"bad key {secret}", {}, None)
    _capture_urlopen(monkeypatch, error=err)
    asr = OpenAICompatibleASR(api_key=secret)
    with pytest.raises(SpeechError) as ei:
        asr.transcribe(b"\x00")
    assert secret not in str(ei.value)
    assert secret not in repr(ei.value)


# ---------- base_url / key 策略 ----------

@pytest.mark.parametrize("url,ok", [
    ("https://api.openai.com/v1", True),
    ("https://my-proxy.example.com/v1", True),
    ("http://api.openai.com/v1", False),   # 公网 http 拒绝
    ("http://example.com", False),
    ("http://127.0.0.1:11434/v1", True),   # 回环放行
    ("http://localhost:8080", True),
    ("http://[::1]:9000", True),
    ("http://192.168.1.10:8000", True),    # 私网放行
    ("http://10.0.0.2", True),
    ("http://172.16.0.5", True),
    ("http://172.31.255.1", True),
    ("http://172.32.0.1", False),          # 172.32 已出私网段
])
def test_base_url_policy(url, ok):
    if ok:
        OpenAICompatibleASR(api_key="k", base_url=url)
        OpenAICompatibleTTS(api_key="k", base_url=url)
    else:
        with pytest.raises(ValueError):
            OpenAICompatibleASR(api_key="k", base_url=url)
        with pytest.raises(ValueError):
            OpenAICompatibleTTS(api_key="k", base_url=url)


def test_public_endpoint_empty_key_config_error():
    asr = OpenAICompatibleASR(api_key="", base_url="https://api.openai.com/v1")
    with pytest.raises(SpeechError) as ei:
        asr.transcribe(b"\x00")
    assert ei.value.kind == "config"
    tts = OpenAICompatibleTTS(api_key="", base_url="https://api.openai.com/v1")
    with pytest.raises(SpeechError) as ei2:
        tts.synthesize("hi")
    assert ei2.value.kind == "config"


def test_loopback_empty_key_allowed(monkeypatch):
    _asr_ok(monkeypatch, text="local ok")
    asr = OpenAICompatibleASR(api_key="", base_url="http://127.0.0.1:9000/v1")
    assert asr.transcribe(b"\x00") == "local ok"


def test_env_key_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    captured = _asr_ok(monkeypatch)
    asr = OpenAICompatibleASR(base_url="http://127.0.0.1:9000/v1")
    asr.transcribe(b"\x00")
    assert captured["headers"]["authorization"] == "Bearer sk-env"


# ---------- Mock 替身 ----------

def test_mocks_deterministic_and_marked():
    asr, tts = MockASR(), MockTTS()
    a1, a2 = asr.transcribe(b"\x01\x02\x03"), asr.transcribe(b"\x01\x02\x03")
    assert a1 == a2 and "3" in a1
    b1, b2 = tts.synthesize("你好"), tts.synthesize("你好")
    assert b1 == b2 and isinstance(b1, bytes) and b1
    assert "mock" in MockASR.__doc__.lower()
    assert "mock" in MockTTS.__doc__.lower()


# ---------- register_speech ----------

def test_register_speech_registers_both_and_resolves():
    registry = PluginRegistry()
    asr = OpenAICompatibleASR(api_key="k", base_url="http://127.0.0.1:9000/v1")
    tts = MockTTS()
    result = register_speech(registry, asr=asr, tts=tts)
    assert result == {"asr": asr, "tts": tts, "mock": ()}

    for pid, cap in (("speech_asr", ASR_TRANSCRIBE), ("speech_tts", TTS_SYNTHESIZE)):
        manifest = registry.manifest(pid)
        assert manifest is not None
        assert manifest.resolved_kind is PluginKind.MODEL
        assert manifest.provides == (cap,)

    assert registry.providers_for(ASR_TRANSCRIBE) == [("speech_asr", asr)]
    assert registry.providers_for(TTS_SYNTHESIZE) == [("speech_tts", tts)]


def test_register_speech_none_falls_back_to_mocks():
    registry = PluginRegistry()
    result = register_speech(registry)
    assert isinstance(result["asr"], MockASR)
    assert isinstance(result["tts"], MockTTS)
    assert result["mock"] == ("asr", "tts")
    # mock 也能被能力解析出来（demo 可用）
    assert registry.providers_for(ASR_TRANSCRIBE)[0][1] is result["asr"]
    assert registry.providers_for(TTS_SYNTHESIZE)[0][1] is result["tts"]
