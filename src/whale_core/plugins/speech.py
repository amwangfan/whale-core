"""ASR/TTS 能力契约与 OpenAI 兼容 provider 插件（PR-10，roadmap §8 P1-1/2）。

- 契约：``ASRProvider.transcribe`` / ``TTSProvider.synthesize`` 两个 Protocol，
  宿主按能力名 ``asr.transcribe`` / ``tts.synthesize`` 从 PluginRegistry 解析；
- provider：``OpenAICompatibleASR``（/v1/audio/transcriptions，multipart/form-data
  纯 stdlib 手搓）与 ``OpenAICompatibleTTS``（/v1/audio/speech，JSON POST，
  响应即音频字节）；
- ``MockASR`` / ``MockTTS``：demo/测试替身，确定性输出、不走网络；
- ``register_speech`` 把两者注册为 kind="model" 插件（端点凭据只在宿主 env /
  私有配置，manifest 不含）。

流式变体（asr.stream / tts.stream）**有意不做**：本 PR 只冻结整段请求/响应契约，
流式是后续可选增强（roadmap §8 允许），届时以 Protocol 扩展方法接入，不破坏本契约。

错误分类（``SpeechError.kind``）：config / auth / quota / server / network /
bad_response——与 core.deepseek.ModelError 同款思路，供宿主做可理解报错与回退。
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

_DEFAULT_BASE = "https://api.openai.com/v1"
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25MB：whisper 类端点的常见上限
_MAX_TTS_CHARS = 4000
_RATE_MIN, _RATE_MAX = 0.25, 4.0

ASR_TRANSCRIBE = "asr.transcribe"
TTS_SYNTHESIZE = "tts.synthesize"


class SpeechError(RuntimeError):
    """语音能力调用失败（分类化）。kind: config/auth/quota/server/network/bad_response。"""

    def __init__(self, kind: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _is_local_or_private_http(url: str) -> bool:
    """http:// 仅放行回环与私网地址（公网 http 仍拒绝）。

    镜像自 ``whale_core.core.deepseek._is_local_or_private_http``——下划线私有名
    不跨包引用，故在此复制同一份小函数；改动时应与 deepseek.py 保持同步。
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() != "http":
            return False
        host = (parts.hostname or "").strip().lower()
    except ValueError:
        return False
    if host in ("localhost", "::1"):
        return True
    if host.startswith("127."):
        return True
    if host.startswith("192.168.") or host.startswith("10."):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


class ASRProvider(Protocol):
    """语音识别能力契约：音频字节 → 文本。"""

    def transcribe(self, audio: bytes, *, lang: str = "zh") -> str:
        ...


class TTSProvider(Protocol):
    """语音合成能力契约：文本 → 音频字节。"""

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0) -> bytes:
        ...


def _classify_http_error(exc: urllib.error.HTTPError) -> SpeechError:
    """HTTP 状态 → SpeechError 分类（消息不含任何凭据）。"""
    status = exc.code
    if status in (401, 403):
        return SpeechError("auth", f"speech auth failed ({status})", status=status)
    if status == 429:
        return SpeechError("quota", "speech quota/rate limited (429)", status=429)
    if 500 <= status < 600:
        return SpeechError("server", f"speech server error ({status})", status=status)
    return SpeechError("bad_response", f"speech HTTP error ({status})", status=status)


def _multipart_form_data(fields: dict[str, str],
                         file_field: str, filename: str, data: bytes,
                         boundary: str) -> bytes:
    """手搓 multipart/form-data 请求体（stdlib 无现成构造器）。"""
    crlf = b"\r\n"
    out = bytearray()
    for name, value in fields.items():
        out += b"--" + boundary.encode() + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        out += value.encode("utf-8") + crlf
    out += b"--" + boundary.encode() + crlf
    out += (f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"').encode() + crlf
    out += b"Content-Type: application/octet-stream" + crlf + crlf
    out += data + crlf
    out += b"--" + boundary.encode() + b"--" + crlf
    return bytes(out)


class _OpenAICompatibleBase:
    """公共基址/key 策略：构造参数优先，env（OPENAI_API_KEY）回落。

    base_url 策略与 core/deepseek.py 相同：公网必须 https，回环/私网允许 http；
    回环/私网 http 端点允许空 key，公网端点缺 key 在**首次调用**时报
    SpeechError(config)（与 DeepSeekCompanion 同款延迟失败）。
    """

    def __init__(self, *, api_key: str = "", base_url: str = _DEFAULT_BASE,
                 model: str, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        if not (self.base_url.startswith("https://")
                or _is_local_or_private_http(self.base_url)):
            raise ValueError("speech provider requires HTTPS (or loopback/private HTTP)")
        self.model = model
        self.timeout = timeout

    def _require_key(self) -> None:
        if not self.api_key and not _is_local_or_private_http(self.base_url):
            raise SpeechError("config", "OPENAI_API_KEY not configured")

    def _headers(self, content_type: str) -> dict[str, str]:
        headers = {"Content-Type": content_type}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


class OpenAICompatibleASR(_OpenAICompatibleBase):
    """OpenAI 兼容 /v1/audio/transcriptions（multipart/form-data，stdlib 手搓）。

    音频字节上限 25MB（超限 ValueError）；响应取 JSON 的 ``text`` 字段。
    """

    def __init__(self, *, api_key: str = "", base_url: str = _DEFAULT_BASE,
                 model: str = "whisper-1", timeout: float = 30.0):
        super().__init__(api_key=api_key, base_url=base_url, model=model, timeout=timeout)

    def transcribe(self, audio: bytes, *, lang: str = "zh") -> str:
        if not isinstance(audio, (bytes, bytearray)):
            raise TypeError("audio must be bytes")
        if len(audio) > _MAX_AUDIO_BYTES:
            raise ValueError(
                f"audio too large: {len(audio)} bytes > {_MAX_AUDIO_BYTES} (25MB)")
        self._require_key()
        boundary = "----whale" + secrets.token_hex(16)
        body = _multipart_form_data(
            {"model": self.model, "language": lang},
            "file", "audio.wav", bytes(audio), boundary)
        req = urllib.request.Request(
            self.base_url + "/audio/transcriptions", data=body,
            headers=self._headers(f"multipart/form-data; boundary={boundary}"),
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SpeechError("network",
                              f"speech network error: {type(exc).__name__}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpeechError("bad_response", "asr returned invalid JSON") from exc
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise SpeechError("bad_response", "asr response missing text")
        return text


class OpenAICompatibleTTS(_OpenAICompatibleBase):
    """OpenAI 兼容 /v1/audio/speech（JSON POST，返回音频字节）。

    文本上限 4000 字符（超限 ValueError）；rate 钳制到 [0.25, 4.0]；
    ``voice=""`` 时用构造默认音色；响应 content-type 非音频（audio/* 或
    application/octet-stream）→ SpeechError(bad_response)。
    """

    def __init__(self, *, api_key: str = "", base_url: str = _DEFAULT_BASE,
                 model: str = "tts-1", voice: str = "alloy", timeout: float = 30.0):
        super().__init__(api_key=api_key, base_url=base_url, model=model, timeout=timeout)
        self.voice = voice

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0) -> bytes:
        if len(text) > _MAX_TTS_CHARS:
            raise ValueError(f"text too long: {len(text)} chars > {_MAX_TTS_CHARS}")
        self._require_key()
        rate = max(_RATE_MIN, min(_RATE_MAX, float(rate)))
        body = json.dumps({
            "model": self.model,
            "input": text,
            "voice": voice or self.voice,
            "speed": rate,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/audio/speech", data=body,
            headers=self._headers("application/json"), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").split(";")[0]
                audio = response.read()
        except urllib.error.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SpeechError("network",
                              f"speech network error: {type(exc).__name__}") from exc
        media = content_type.strip().lower()
        if not (media.startswith("audio/") or media == "application/octet-stream"):
            raise SpeechError(
                "bad_response", f"tts returned non-audio content-type: {media or '?'}")
        return audio


class MockASR:
    """mock：demo/测试替身（docstring 标注 mock）。确定性输出，不走网络。

    输出由输入派生（语言 + 字节数），同一输入永远同一输出。
    """

    def __init__(self, *, prefix: str = "[mock-asr]"):
        self.prefix = prefix

    def transcribe(self, audio: bytes, *, lang: str = "zh") -> str:
        return f"{self.prefix} lang={lang} bytes={len(audio)}"


class MockTTS:
    """mock：demo/测试替身（docstring 标注 mock）。确定性输出，不走网络。

    输出是伪音频字节（输入文本的 UTF-8 加上头部标记），仅用于打通链路。
    """

    def __init__(self, *, magic: bytes = b"MOCKTTS\n"):
        self.magic = magic

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0) -> bytes:
        return self.magic + f"voice={voice or 'default'} rate={rate}\n".encode() \
            + text.encode("utf-8")


def register_speech(registry: PluginRegistry, *,
                    asr: ASRProvider | None = None,
                    tts: TTSProvider | None = None) -> dict:
    """注册 ASR/TTS 两个 model 插件（plugin_id="speech_asr"/"speech_tts"）。

    provider 对象注入；任一参数为 None 时挂对应 Mock（demo 可用）。返回值标注
    哪些位置用的是 mock：``{"asr": obj, "tts": obj, "mock": ("asr", ...)}``。
    """
    mocked: list[str] = []
    if asr is None:
        asr = MockASR()
        mocked.append("asr")
    if tts is None:
        tts = MockTTS()
        mocked.append("tts")
    registry.register(
        PluginManifest(
            plugin_id="speech_asr", name="ASR（语音识别）", version="1.0.0",
            kind="model", provides=(ASR_TRANSCRIBE,),
            description="语音识别端点（OpenAI 兼容 /v1/audio/transcriptions）",
        ),
        provider=asr,
    )
    registry.register(
        PluginManifest(
            plugin_id="speech_tts", name="TTS（语音合成）", version="1.0.0",
            kind="model", provides=(TTS_SYNTHESIZE,),
            description="语音合成端点（OpenAI 兼容 /v1/audio/speech）",
        ),
        provider=tts,
    )
    return {"asr": asr, "tts": tts, "mock": tuple(mocked)}
