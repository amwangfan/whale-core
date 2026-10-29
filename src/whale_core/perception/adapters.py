"""感知适配器协议。

数据来源说明（此处不实现）：
- PhoneForeground ← 「前台监测」仓（ADB / Accessibility 等在基础设施侧完成）
- ScheduleTick ← 本机日历 / 提醒服务推送
- LocationHint ← 可选粗粒度位置源

智能层只订阅语义事件流；禁止在本包内写 ADB、截图或厂商 SDK。
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from whale_core.perception.events import PerceptionEvent


@runtime_checkable
class PerceptionAdapter(Protocol):
    """将基础设施侧原始信号转为 PerceptionEvent。"""

    def poll(self) -> Iterator[PerceptionEvent]:
        """拉取一批可用事件（可为空）。"""
        ...


class NullPerceptionAdapter:
    """空适配器：永不产生事件。"""

    def poll(self) -> Iterator[PerceptionEvent]:
        return iter(())
