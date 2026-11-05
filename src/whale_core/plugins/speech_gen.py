"""语音合成网关：文本 + 模型/音色/语速选择的单文件收口。

分层：内置 provider 编排（宿主装配点），非插件框架契约（builtin-provider，issue #31）。

2026-11-05 owner 定版的语音流程（与生图网关 image_gen.py 同形态）：

1. 群消息/私聊 → 意图模型（kimi-k3）判定要说话（或读一段文字）并产出文本；
2. kimi 可选指定 ``model`` / ``voice``（用户点名才填；默认 MiniMax
   speech-2.8-hd + female-tianmei，owner 决策「最好最自然」）；
3. 进入本模块 ``SpeechGenGateway.synthesize``：
   - 模型分派（未知模型 ValueError 并列可用清单，诚实报错）；
   - 音色空 → provider 按模型给合法默认（音色不跨模型混用）；
   - 返回音频字节 + 实际用声，落盘/发送由宿主负责。

速率钳制等 provider 侧护栏不变（minimax_media / dashscope_media 各自实现）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class SpeechGenResult:
    """一次语音合成的网关层结果：音频字节 + 实际模型/音色的如实回执。"""

    audio: bytes
    model: str
    voice: str
    note: str = ""


class SpeechGenGateway:
    """语音合成统一入口：模型分派 + 参数护栏。

    入参语义（对应工具参数）：
    - ``text``：要朗读的文字（必填）；
    - ``model``：语音模型名，空 = 默认模型（未知模型 ValueError 并列出可用）；
    - ``voice``：音色 id，空 = provider 按模型自动给合法默认；
    - ``rate``：语速倍率（provider 侧钳制到合法区间）。
    """

    def __init__(self, providers: Mapping[str, object], *, default_model: str) -> None:
        if default_model and default_model not in providers:
            raise ValueError(f"default model {default_model!r} not in providers")
        self.providers = dict(providers)
        self.default_model = default_model

    @property
    def available_models(self) -> list[str]:
        return sorted(self.providers)

    def synthesize(self, text: str, *, model: str = "", voice: str = "",
                   rate: float = 1.0) -> SpeechGenResult:
        text = (text or "").strip()
        if not text:
            raise ValueError("text must be non-empty")
        model = (model or "").strip() or self.default_model
        provider = self.providers.get(model)
        if provider is None:
            raise ValueError(
                f"未知语音模型 {model!r}（可用：{', '.join(self.available_models)}）")
        audio = provider.synthesize(text, voice=(voice or "").strip(),
                                    rate=float(rate or 1.0))
        # 回执如实带实际用声：显式 voice 优先，否则 provider 的模型默认音色
        actual_voice = (voice or "").strip() or str(getattr(provider, "voice", "") or "")
        return SpeechGenResult(audio=audio, model=model, voice=actual_voice)
