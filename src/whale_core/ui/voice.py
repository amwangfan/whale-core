"""语音风格偏好（不写 API Key / 厂商账号）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceStyle:
    """TTS / 通话侧风格提示；密钥与端点由运行环境注入，不进本仓。"""

    locale: str = "zh-CN"
    speaking_rate: float = 1.0
    pitch: float = 0.0
    timbre_hint: str = "warm_neutral"
    # 占位：供应商名可选；真实凭证走环境变量，禁止硬编码
    provider_hint: str = ""
