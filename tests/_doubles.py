"""测试替身集中地（owner 决策 2026-10-30：产品面不设 mock 模式）。

这些类只服务测试：确定性、不触网。它们**曾经**作为产品代码里的
「Mock provider 缺省默认」存在——已移除（mock 默认会伪装成可用能力，
生产风险）。测试需要时从这里取。
"""
from __future__ import annotations

from typing import Any

from whale_core.plugins.media_image import ImageTaskResult, _validate_request
from whale_core.plugins.media_video import (
    DEFAULT_DURATION_S,
    DEFAULT_SIZE,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    VideoError,
)


class MockASR:
    """mock ASR 替身：输出由输入派生，不走网络。"""

    def __init__(self, *, prefix: str = "[mock-asr]"):
        self.prefix = prefix

    def transcribe(self, audio: bytes, *, lang: str = "zh") -> str:
        return f"{self.prefix} lang={lang} bytes={len(audio)}"


class MockTTS:
    """mock TTS 替身：伪音频字节（头部标记 + 文本）。"""

    def __init__(self, *, magic: bytes = b"MOCKTTS\n"):
        self.magic = magic

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0) -> bytes:
        return self.magic + f"voice={voice or 'default'} rate={rate}\n".encode() \
            + text.encode("utf-8")


def _mock_svg_data_uri(prompt: str, size: str, index: int) -> str:
    import html
    import urllib.parse
    width, _, height = size.partition("x")
    label = html.escape(f"mock #{index + 1}: {prompt[:60]}", quote=False)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        f'<rect width="100%" height="100%" fill="#dfe9f3"/>'
        f'<text x="16" y="32" font-size="20" fill="#334455">{label}</text>'
        f"</svg>"
    )
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe="")


class MockImageProvider:
    """mock 图像替身：SVG data URI 罐装产物，不发起任何网络请求。"""

    def generate(self, prompt: str, *, size: str = "1024x1024", n: int = 1) -> ImageTaskResult:
        prompt, size, n = _validate_request(prompt, size, n)
        images = tuple(_mock_svg_data_uri(prompt, size, i) for i in range(n))
        return ImageTaskResult(task_id="mock-image-task", status="succeeded", images=images)


class MockVideoProvider:
    """mock 视频替身：脚本化生命周期（running N 次 → 终态），无网络。"""

    def __init__(self, *, running_polls: int = 2, final_status: str = STATUS_SUCCEEDED,
                 fail_error: str = "mock failure"):
        if final_status not in (STATUS_SUCCEEDED, STATUS_FAILED):
            raise ValueError(f"mock final_status must be succeeded/failed, got {final_status!r}")
        self.running_polls = max(0, int(running_polls))
        self.final_status = final_status
        self.fail_error = fail_error
        self.submit_calls = 0
        self.poll_calls = 0
        self.cancel_calls = 0
        self.submitted: list[tuple[str, int, str]] = []
        self._tasks: dict[str, dict[str, Any]] = {}

    def submit(self, prompt: str, *, duration_s: int = DEFAULT_DURATION_S,
               size: str = DEFAULT_SIZE) -> str:
        self.submit_calls += 1
        task_id = f"mock-video-{self.submit_calls:04d}"
        self._tasks[task_id] = {"polls": 0, "cancelled": False}
        self.submitted.append((prompt, duration_s, size))
        return task_id

    def poll(self, task_id: str) -> tuple[str, str, str]:
        self.poll_calls += 1
        rec = self._tasks.get(task_id)
        if rec is None:
            raise VideoError("not_found", f"unknown mock video task: {task_id!r}")
        if rec["cancelled"]:
            return (STATUS_CANCELLED, "", "")
        rec["polls"] += 1
        if rec["polls"] <= self.running_polls:
            return (STATUS_RUNNING, "", "")
        if self.final_status == STATUS_FAILED:
            return (STATUS_FAILED, "", self.fail_error)
        return (STATUS_SUCCEEDED, f"https://mock.invalid/video/{task_id}.mp4", "")

    def cancel(self, task_id: str) -> bool:
        self.cancel_calls += 1
        rec = self._tasks.get(task_id)
        if rec is None:
            return False
        rec["cancelled"] = True
        return True
