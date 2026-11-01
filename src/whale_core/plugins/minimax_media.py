"""MiniMax 媒体 provider：视频生成（Hailuo/T2V）+ 语音合成（t2a_v2）。

分层：内置 provider 实现，非插件框架契约（builtin-provider，issue #31）。

协议契约来源：MiniMax 官方 OpenAPI（platform.minimax.io / platform.minimax.cn）：
- 视频 ``POST {base}/v1/video_generation`` → ``task_id``；轮询
  ``GET {base}/v1/query/video_generation?task_id=``（status:
  processing/success/failed）→ ``GET {base}/v1/files/retrieve?file_id=``
  取下载链接。模型：MiniMax-Hailuo-2.3 / MiniMax-Hailuo-02 / T2V-01(-Director)，
  duration 6 或 10 秒，resolution 720P/768P/1080P（依模型）。
- TTS ``POST {base}/v1/t2a_v2``（stream=false, output_format=hex）→
  data.audio 十六进制字节。模型：speech-02-hd / speech-02-turbo /
  speech-2.6-* / speech-2.8-*；voice_setting.voice_id 为平台音色 id。

安全：key 只从构造参数读，永不进日志/异常；仅 https（或回环调试）。
MiniMax 视频无取消接口：cancel 如实返回 False。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from whale_core.plugins.media_video import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    VideoError,
)
from whale_core.plugins.speech import _is_local_or_private_http

MINIMAX_BASE = "https://api.minimaxi.com"

VIDEO_MODELS = frozenset({
    "MiniMax-Hailuo-2.3", "MiniMax-Hailuo-02", "T2V-01-Director", "T2V-01",
})
TTS_MODELS = frozenset({
    "speech-02-hd", "speech-02-turbo", "speech-2.6-hd", "speech-2.6-turbo",
    "speech-2.8-hd", "speech-2.8-turbo",
})
#: Hailuo 系只支持 768P/1080P；T2V-01 系只支持 720P（官方矩阵）
_HAILUO = ("MiniMax-Hailuo-2.3", "MiniMax-Hailuo-02")


class MiniMaxError(RuntimeError):
    """kind: config / auth / quota / server / network / bad_response / rejected。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class _MiniMaxBase:
    def __init__(self, *, api_key: str, base_url: str = MINIMAX_BASE,
                 timeout: float = 60.0) -> None:
        base = (base_url or "").rstrip("/")
        if not (base.startswith("https://") or _is_local_or_private_http(base)):
            raise ValueError("minimax provider requires HTTPS (or loopback/private HTTP)")
        self.api_key = api_key or ""
        self.base_url = base
        self.timeout = timeout

    def _request(self, path: str, body: dict | None = None, *, method: str = "POST") -> dict:
        if not self.api_key:
            raise MiniMaxError("config", "minimax api key not configured")
        url = self.base_url + path
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        # 重定向不携带凭据（与 dashscope_media 同款纪律）
        req.add_unredirected_header("Authorization", f"Bearer {self.api_key}")
        from whale_core.plugins import provider_http
        try:
            with provider_http.open_request(req, timeout=self.timeout) as resp:
                raw = provider_http.read_response(resp, 16 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MiniMaxError("auth", f"auth failed ({exc.code})") from None
            if exc.code == 429:
                raise MiniMaxError("quota", "rate limited (429)") from None
            if 500 <= exc.code < 600:
                raise MiniMaxError("server", f"server error ({exc.code})") from None
            raise MiniMaxError("bad_response", f"HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MiniMaxError("network", f"network error: {type(exc).__name__}") from exc
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MiniMaxError("bad_response", "invalid JSON") from None
        if not isinstance(data, dict):
            raise MiniMaxError("bad_response", "response is not an object")
        base_resp = data.get("base_resp")
        if isinstance(base_resp, dict):
            code = base_resp.get("status_code")
            if code not in (None, 0):
                kind = {1002: "quota", 1039: "quota", 1004: "auth", 2049: "auth",
                        1008: "quota", 1026: "rejected", 2013: "bad_response"}.get(
                    int(code), "server")
                raise MiniMaxError(kind, f"minimax base_resp {code}")
        return data


class MiniMaxVideoProvider(_MiniMaxBase):
    """MiniMax 文生视频（VideoProvider 契约：submit/poll/cancel）。"""

    def __init__(self, *, api_key: str, base_url: str = MINIMAX_BASE,
                 model: str = "MiniMax-Hailuo-2.3", timeout: float = 60.0) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
        if model not in VIDEO_MODELS:
            raise ValueError(f"unknown minimax video model {model!r}")
        self.model = model

    def submit(self, prompt: str, *, duration_s: int = 6, size: str = "1280x720") -> str:
        prompt = (prompt or "").strip()[:2000]
        if not prompt:
            raise ValueError("prompt must be non-empty")
        duration = int(duration_s)
        # 官方矩阵：Hailuo 6/10s + 768P/1080P；T2V-01 固定 6s + 720P
        if self.model in _HAILUO:
            duration = 10 if duration >= 10 else 6
            resolution = "1080P" if size.endswith("x1080") else "768P"
        else:
            duration = 6
            resolution = "720P"
        data = self._request("/v1/video_generation", {
            "model": self.model, "prompt": prompt,
            "duration": duration, "resolution": resolution,
        })
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise MiniMaxError("bad_response", "video submit returned no task_id")
        return task_id

    def poll(self, task_id: str) -> tuple[str, str, str]:
        path = "/v1/query/video_generation?" + urllib.parse.urlencode({"task_id": task_id})
        data = self._request(path, None, method="GET")
        status = str(data.get("status") or "").strip().lower()
        if status == "success":
            file_id = str(data.get("file_id") or "").strip()
            if not file_id:
                raise MiniMaxError("bad_response", "succeeded but no file_id")
            fdata = self._request(
                "/v1/files/retrieve?" + urllib.parse.urlencode({"file_id": file_id}),
                None, method="GET")
            file_obj = fdata.get("file") if isinstance(fdata.get("file"), dict) else {}
            url = str(file_obj.get("download_url") or "")
            if not url.startswith("https://"):
                raise MiniMaxError("bad_response", "video result missing HTTPS URL")
            return STATUS_SUCCEEDED, url, ""
        if status == "failed":
            return STATUS_FAILED, "", "provider_failed"
        return STATUS_RUNNING, "", ""

    def cancel(self, task_id: str) -> bool:
        return False  # MiniMax 视频无取消接口（官方文档未提供）


class MiniMaxTTSProvider(_MiniMaxBase):
    """MiniMax t2a_v2 语音合成（TTSProvider 契约：synthesize → 音频字节）。"""

    def __init__(self, *, api_key: str, base_url: str = MINIMAX_BASE,
                 model: str = "speech-02-hd", voice: str = "female-tianmei",
                 timeout: float = 60.0) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
        if model not in TTS_MODELS:
            raise ValueError(f"unknown minimax tts model {model!r}")
        self.model = model
        self.voice = voice

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0) -> bytes:
        text = (text or "").strip()
        if not text:
            raise ValueError("text must be non-empty")
        if len(text) > 4000:
            raise ValueError("text exceeds 4000 chars")
        rate = max(0.5, min(2.0, float(rate)))
        data = self._request("/v1/t2a_v2", {
            "model": self.model,
            "text": text,
            "stream": False,
            "output_format": "hex",
            "voice_setting": {"voice_id": voice or self.voice,
                              "speed": rate, "vol": 1.0, "pitch": 0},
            "audio_setting": {"format": "wav", "sample_rate": 24000,
                              "bitrate": 128000, "channel": 1},
        })
        d = data.get("data")
        if not isinstance(d, dict) or not isinstance(d.get("audio"), str) or not d["audio"]:
            raise MiniMaxError("bad_response", "tts response has no audio hex")
        try:
            return bytes.fromhex(d["audio"])
        except ValueError:
            raise MiniMaxError("bad_response", "invalid hex audio") from None
