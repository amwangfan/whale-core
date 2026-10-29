"""HUD 呈现意图：文案 / 布局提示，非像素渲染。

真正上镜仍由 Turbo-IO / dashboard 执行；本模块只产出呈现意图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(frozen=True)
class HudPresentation:
    """一次 HUD 呈现意图。"""

    headline: str
    lines: Sequence[str] = ()
    layout_hint: str = "card.compact"  # card.compact | card.detail | toast
    emphasis: str = "normal"  # normal | urgent | quiet
    meta: Mapping[str, str] = field(default_factory=dict)

    def as_show_card_payload(self) -> dict:
        return {
            "title": self.headline,
            "body": "\n".join(self.lines),
            "layout_hint": self.layout_hint,
            "emphasis": self.emphasis,
            **dict(self.meta),
        }
