"""DashScope 兼容协议的媒体 provider（生图/ASR/TTS），多平台可用。

协议契约来源：whale-io 生产 bridge（infra/call-bridge/audio-bridge/agent_api.py）
实测通过的形状 + AgentScope 平台媒体手册（docs/media-and-voice.md 口径）：

- 生图 ``qwen-image-3.0``：POST {base}/services/aigc/multimodal-generation/generation，
  同步返回 output.choices[].message.content[].image；异步则 output.task_id →
  轮询 GET {base}/tasks/{id}（task_status SUCCEEDED/FAILED/CANCELED）。
- ASR ``fun-asr-flash``：同一 generation 端点，input_audio 为 base64 data URI。
- TTS ``qwen-audio-3.0-tts-*``：POST {base}/services/audio/tts/SpeechSynthesizer，
  output.audio.data（base64）或 output.audio.url（签名临时链接，尽快下载）。

平台兼容：同一套路径在阿里云百炼（dashscope.aliyuncs.com/api/v1）与
AgentScope 平台（platform.agentscope.io/v1）都成立——base_url 可换。

安全：key 只从构造参数/env 读，永不进日志/异常；结果 URL 是签名临时链接，
不落盘由宿主决定（ArtifactStore）。本模块未与真实服务联调的部分在测试中以
离线替身验证；真实联通记录见 docs/archive/2026-10-30/gap-analysis-2026-10-30.md。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from whale_core.plugins.media_image import ImageTaskResult
from whale_core.plugins.speech import _is_local_or_private_http

DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/api/v1"
AGENTSCOPE_BASE = "https://platform.agentscope.io/v1"

DEFAULT_IMAGE_MODEL = "qwen-image-3.0"
DEFAULT_ASR_MODEL = "fun-asr-flash-2026-06-15"
DEFAULT_TTS_MODEL = "qwen-audio-3.0-tts-flash"

# qwen-image 的合法尺寸（手册口径：宽高比 → 尺寸）
IMAGE_SIZES = frozenset({
    "1328*1328", "1472*1104", "1664*928", "1104*1472", "928*1664",
    "1472*976", "976*1472",
})
# 比例别名 → 手册尺寸（LLM 规划器常传 "1:1"/"16:9" 或 "1024x1024"，归一化而非硬失败）
_RATIO_TO_SIZE = {
    "1:1": "1328*1328", "4:3": "1472*1104", "16:9": "1664*928",
    "3:4": "1104*1472", "9:16": "928*1664", "3:2": "1472*976", "2:3": "976*1472",
}
_WH_TO_SIZE = {  # 常见 "WxH" → 最接近的手册比例
    (1024, 1024): "1328*1328", (1792, 1024): "1664*928", (1024, 1792): "928*1664",
    (1280, 720): "1664*928", (720, 1280): "928*1664",
}


def normalize_image_size(size: str) -> str:
    """把各种尺寸写法归一到手册合法值；无法识别时回退 1:1。"""
    s = (size or "").strip()
    if s in IMAGE_SIZES:
        return s
    if s in _RATIO_TO_SIZE:
        return _RATIO_TO_SIZE[s]
    if "x" in s:
        try:
            w, _, h = s.partition("x")
            wh = (int(w), int(h))
            if wh in _WH_TO_SIZE:
                return _WH_TO_SIZE[wh]
            # 按宽高比近似匹配
            if wh[0] and wh[1]:
                r = wh[0] / wh[1]
                best = min(_RATIO_TO_SIZE.items(),
                           key=lambda kv: abs(_ratio_value(kv[0]) - r))
                return best[1]
        except ValueError:
            pass
    return "1328*1328"


def _ratio_value(ratio: str) -> float:
    w, _, h = ratio.partition(":")
    return float(w) / float(h)
ASR_MAX_BYTES = 25 * 1024 * 1024
TTS_MAX_TEXT = 4000


class DashScopeError(RuntimeError):
    """kind: config / auth / quota / server / network / bad_response。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class _DashScopeBase:
    def __init__(self, *, api_key: str = "", base_url: str = AGENTSCOPE_BASE,
                 timeout: float = 60.0) -> None:
        base = (base_url or "").rstrip("/")
        if not (base.startswith("https://") or _is_local_or_private_http(base)):
            raise ValueError("dashscope provider requires HTTPS (or loopback/private HTTP)")
        self.api_key = api_key or ""
        self.base_url = base
        self.timeout = timeout

    def _post(self, path: str, body: dict, *, extra_headers: dict | None = None) -> dict:
        if not self.api_key and not _is_local_or_private_http(self.base_url):
            raise DashScopeError("config", "api key not configured")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(self.base_url + path,
                                     data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        return self._open(req)

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(self.base_url + path,
                                     headers={"Authorization": f"Bearer {self.api_key}"},
                                     method="GET")
        return self._open(req)

    def _open(self, req: urllib.request.Request) -> dict:
        from whale_core.plugins import provider_http
        try:
            with provider_http.open_request(req, timeout=self.timeout) as resp:
                raw = provider_http.read_response(resp, 4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise DashScopeError("auth", f"auth failed ({exc.code})") from None
            if exc.code == 429:
                raise DashScopeError("quota", "rate limited (429)") from None
            if 500 <= exc.code < 600:
                raise DashScopeError("server", f"server error ({exc.code})") from None
            raise DashScopeError("bad_response", f"HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DashScopeError("network", f"network error: {type(exc).__name__}") from exc
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DashScopeError("bad_response", "invalid JSON") from None
        if not isinstance(data, dict):
            raise DashScopeError("bad_response", "response is not an object")
        return data


def _task_output(data: dict) -> dict:
    out = data.get("output")
    if not isinstance(out, dict):
        raise DashScopeError("bad_response", "missing output object")
    return out


class DashScopeImageProvider(_DashScopeBase):
    """qwen-image 生图：同步直出 + 异步轮询两种返回形态都处理（手册 §1）。"""

    def __init__(self, *, api_key: str = "", base_url: str = AGENTSCOPE_BASE,
                 model: str = DEFAULT_IMAGE_MODEL, timeout: float = 180.0,  # 生图慢
                 poll_interval: float = 5.0, poll_timeout: float = 300.0) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
        self.model = model or DEFAULT_IMAGE_MODEL
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def generate(self, prompt: str, *, size: str = "1328*1328", n: int = 1) -> ImageTaskResult:
        prompt = (prompt or "").strip()[:2000]
        if not prompt:
            raise ValueError("prompt must be non-empty")
        size = normalize_image_size(size)  # 各种写法归一到手册合法值，不再硬失败
        n = max(1, min(int(n), 4))
        body = {
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
            "parameters": {"size": size, "watermark": False},
        }
        data = self._post("/services/aigc/multimodal-generation/generation", body)
        output = _task_output(data)
        # 同步形态：choices[].message.content[].image
        urls = _extract_image_urls(output)
        if urls:
            return ImageTaskResult(task_id=str(output.get("task_id") or "sync"),
                                   status="succeeded", images=tuple(urls[:n]))
        # 异步形态：task_id → 轮询
        task_id = str(output.get("task_id") or "")
        if not task_id:
            raise DashScopeError("bad_response", "image response has neither images nor task_id")
        return self._poll(task_id, n)

    def _poll(self, task_id: str, n: int) -> ImageTaskResult:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            data = self._get(f"/tasks/{task_id}")
            output = _task_output(data)
            status = str(output.get("task_status") or "")
            if status == "SUCCEEDED":
                urls = _extract_image_urls(output)
                if not urls:
                    raise DashScopeError("bad_response", "succeeded but no image url")
                return ImageTaskResult(task_id=task_id, status="succeeded",
                                       images=tuple(urls[:n]))
            if status in ("FAILED", "CANCELED"):
                err = output.get("error")
                msg = err.get("message", "task failed") if isinstance(err, dict) else "task failed"
                return ImageTaskResult(task_id=task_id, status="failed", images=(), error=str(msg)[:200])
            if time.monotonic() > deadline:
                return ImageTaskResult(task_id=task_id, status="failed", images=(),
                                       error="poll timeout")
            time.sleep(self.poll_interval)


def _extract_image_urls(output: dict) -> list[str]:
    """同步/异步两种形态的 image URL 提取（手册 §3：choices[].content[].image 或 output.image/url）。"""
    urls: list[str] = []
    choices = output.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            content = (message or {}).get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("image"), str):
                        urls.append(part["image"])
            elif isinstance(content, str):
                pass
    for key in ("image", "url"):
        value = output.get(key)
        if isinstance(value, str) and value.startswith("http"):
            urls.append(value)
    return urls


class DashScopeASRProvider(_DashScopeBase):
    """fun-asr-flash 同步 ASR：WAV 字节 → 文本（whale-io 生产契约）。"""

    def __init__(self, *, api_key: str = "", base_url: str = DASHSCOPE_BASE,
                 model: str = DEFAULT_ASR_MODEL, timeout: float = 90.0) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
        self.model = model or DEFAULT_ASR_MODEL

    def transcribe(self, audio: bytes, *, lang: str = "zh") -> str:
        if not audio:
            raise ValueError("audio must be non-empty")
        if len(audio) > ASR_MAX_BYTES:
            raise ValueError("audio exceeds 25MB limit")
        b64 = base64.b64encode(audio).decode("ascii")
        body = {
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": [
                {"type": "input_audio",
                 "input_audio": {"data": f"data:audio/wav;base64,{b64}"}}]}]},
            "parameters": {"format": "wav", "sample_rate": "16000",
                           "language_hints": ["zh", "en"]},
        }
        data = self._post("/services/aigc/multimodal-generation/generation", body,
                          extra_headers={"X-DashScope-SSE": "disable"})
        text = _extract_asr_text(data)
        if not text:
            raise DashScopeError("bad_response", "asr response has no text")
        return text


def _extract_asr_text(data: dict) -> str:
    """从 generation 响应里提取转写文本（choices[].message.content 兼容 str/list）。"""
    output = _task_output(data)
    choices = output.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = (message or {}).get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(part.get("text", "")) for part in content if isinstance(part, dict)]
        return "".join(parts).strip()
    return ""


class DashScopeTTSProvider(_DashScopeBase):
    """qwen-audio-3.0-tts SpeechSynthesizer：文本 → WAV 字节。"""

    def __init__(self, *, api_key: str = "", base_url: str = AGENTSCOPE_BASE,
                 model: str = DEFAULT_TTS_MODEL, voice: str = "", timeout: float = 60.0) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
        self.model = model or DEFAULT_TTS_MODEL
        # 默认音色按模型给合法值（音色不能跨模型混用——手册 §7）
        if not voice:
            voice = ("longanlingxin" if "plus" in self.model else "longanfengyue")
        self.voice = voice

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0) -> bytes:
        text = (text or "").strip()
        if not text:
            raise ValueError("text must be non-empty")
        if len(text) > TTS_MAX_TEXT:
            raise ValueError("text exceeds 4000 chars")
        # 手册 §7 实测形状：input 只有 text；voice 在 parameters。
        # 写错位置会被上游 400 包装成 502（生产实证）。
        body = {
            "model": self.model,
            "input": {"text": text},
            "parameters": {"voice": voice or self.voice},
        }
        data = self._post("/services/audio/tts/SpeechSynthesizer", body)
        output = _task_output(data)
        audio = output.get("audio")
        if not isinstance(audio, dict):
            raise DashScopeError("bad_response", "tts response has no audio object")
        data_b64 = audio.get("data")
        if isinstance(data_b64, str) and data_b64:
            try:
                return _normalize_wav(base64.b64decode(data_b64))
            except ValueError:
                raise DashScopeError("bad_response", "invalid base64 audio") from None
        url = audio.get("url")
        if isinstance(url, str) and url:
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            if not url.startswith("https://"):
                raise DashScopeError("bad_response", "tts audio url not https")
            req = urllib.request.Request(url, headers={"User-Agent": "whale-core/0.1"})
            from whale_core.plugins import provider_http
            try:
                with provider_http.open_request(req, timeout=self.timeout) as resp:
                    return _normalize_wav(provider_http.read_response(resp, 64 * 1024 * 1024))
            except (urllib.error.URLError, OSError) as exc:
                raise DashScopeError("network", f"audio fetch failed: {type(exc).__name__}") from exc
        raise DashScopeError("bad_response", "tts response has neither data nor url")


def _normalize_wav(wav_bytes: bytes) -> bytes:
    """把上游 WAV 洗成标准头（PCM 原采样率/位深）。

    上游（qwen-audio TTS）的 WAV 帧计数是 0x3FFFFFFF 占位符，嫩解析器
    （silk-wasm 语音编码）会被它绊倒（生产实证 divide-by-zero）。用 wave
    模块重写头即修复；读不了就原样返回（不假装能修）。
    """
    import io
    import wave
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            frames = w.readframes(w.getnframes())
            out = io.BytesIO()
            with wave.open(out, "wb") as w2:
                w2.setnchannels(w.getnchannels())
                w2.setsampwidth(w.getsampwidth())
                w2.setframerate(w.getframerate())
                w2.writeframes(frames)
            return out.getvalue()
    except Exception:
        return wav_bytes
