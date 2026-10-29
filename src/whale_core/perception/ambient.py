"""环境语音元数据提取 stub。

只处理 summary_label / language / duration 等结构化标签；
**禁止**把音频字节写入 memory。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from whale_core.perception.events import AmbientSpeechSegment, PerceptionEvent


@dataclass(frozen=True)
class AmbientEventTags:
    """从 AmbientSpeechSegment 抽出的结构化标签（无音频）。"""

    summary_label: str = ""
    language_hint: str = ""
    duration_ms: int = 0
    source: str = "ambient"
    event_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary_label": self.summary_label,
            "language_hint": self.language_hint,
            "duration_ms": self.duration_ms,
            "source": self.source,
            "event_id": self.event_id,
        }

    def text_summary(self) -> str:
        """供 remember_episode 用的纯文本摘要（无音频）。"""
        bits = [
            b
            for b in (
                self.summary_label,
                f"{self.duration_ms}ms" if self.duration_ms else "",
                self.language_hint,
                self.source,
            )
            if b
        ]
        return "环境语音：" + " · ".join(bits) if bits else "环境语音：片段"


def extract_ambient_events(
    segment: AmbientSpeechSegment | PerceptionEvent | Mapping[str, Any],
) -> AmbientEventTags:
    """从片段或 mapping 提取标签；忽略任何疑似音频字段。"""
    if isinstance(segment, Mapping):
        payload = dict(segment)
        return AmbientEventTags(
            summary_label=str(payload.get("summary_label") or ""),
            language_hint=str(payload.get("language_hint") or ""),
            duration_ms=int(payload.get("duration_ms") or 0),
            source=str(payload.get("source") or "ambient"),
            event_id=str(payload.get("event_id") or ""),
        )

    payload = dict(getattr(segment, "payload", None) or {})
    # 显式丢弃可能误传入的音频键，绝不向上游传播
    for bad in ("audio", "audio_bytes", "pcm", "wav", "raw_audio"):
        payload.pop(bad, None)

    return AmbientEventTags(
        summary_label=str(
            getattr(segment, "summary_label", None) or payload.get("summary_label") or ""
        ),
        language_hint=str(
            getattr(segment, "language_hint", None) or payload.get("language_hint") or ""
        ),
        duration_ms=int(
            getattr(segment, "duration_ms", None) or payload.get("duration_ms") or 0
        ),
        source=str(getattr(segment, "source", None) or payload.get("source") or "ambient"),
        event_id=str(getattr(segment, "event_id", None) or ""),
    )
