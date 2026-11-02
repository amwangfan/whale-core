"""Neutral expression contract; identity is supplied only by selected cards."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class WhalePersona:
    """智能层侧的人格声明；真正播报 / 上镜由基础设施执行。"""

    name: str = ""
    tone: str = ""
    address_user_as: str = "你"
    taboos: Sequence[str] = field(default_factory=tuple)
    system_preamble: str = ""
