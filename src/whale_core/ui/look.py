"""鲸鱼形象 / 像素画 / 惊喜 HUD 的设计意图字段（非像素资源）。

真正渲染由 Turbo-IO / dashboard 或眼镜侧完成；本模块只描述「长什么样」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(frozen=True)
class WhaleLook:
    """形象与惊喜呈现的设计意图。"""

    silhouette: str = "whale.pixel.default"  # 形象 ID，非位图路径
    pixel_palette: str = "8bit.ocean"
    mood: str = "calm"  # calm | playful | focused | surprise
    surprise_hud: bool = False
    surprise_motif: str = ""  # e.g. confetti.soft — 主题标签
    animation_hint: str = "idle.breathe"
    variants: Sequence[str] = ()
    notes: Mapping[str, str] = field(default_factory=dict)

    def as_hud_meta(self) -> dict:
        return {
            "look_silhouette": self.silhouette,
            "look_palette": self.pixel_palette,
            "look_mood": self.mood,
            "look_surprise": self.surprise_hud,
            "look_motif": self.surprise_motif,
            "look_animation": self.animation_hint,
        }
