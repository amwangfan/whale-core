"""感知事件类型：智能层只消费语义事件，不碰设备 API。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class PerceptionEvent:
    """所有感知事件的公共形状。"""

    kind: str
    event_id: str = field(default_factory=_new_id)
    occurred_at: datetime = field(default_factory=_utc_now)
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PhoneForeground(PerceptionEvent):
    """手机前台应用变化（数据来自「前台监测」基础设施）。"""

    kind: str = "phone.foreground"
    package: str = ""
    label: str = ""


@dataclass(frozen=True)
class ScheduleTick(PerceptionEvent):
    """日程 / 定时提醒 tick。"""

    kind: str = "schedule.tick"
    title: str = ""
    due_at: datetime | None = None


@dataclass(frozen=True)
class LocationHint(PerceptionEvent):
    """粗粒度位置提示（地名 / geohash 等），禁止写真实坐标入库策略见 docs。"""

    kind: str = "location.hint"
    place_label: str = ""
    coarse_code: str = ""


@dataclass(frozen=True)
class AmbientSpeechSegment(PerceptionEvent):
    """环境语音片段元数据。

    约定：只存时长 / 语言 / 摘要标签等元数据；**不把原始音频字节写入记忆库**。
    采集与 ASR 在基础设施侧。
    """

    kind: str = "ambient.speech_segment"
    duration_ms: int = 0
    language_hint: str = ""
    summary_label: str = ""
    source: str = "ambient"  # ambient | phone_mic | glass_mic


@dataclass(frozen=True)
class ContextCourse(PerceptionEvent):
    """课程表条目（情境推理输入）。"""

    kind: str = "context.course"
    course_title: str = ""
    location_label: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None


@dataclass(frozen=True)
class ContextWeather(PerceptionEvent):
    """天气情境（粗粒度：晴/雨/温度区间等）。"""

    kind: str = "context.weather"
    condition: str = ""
    temp_c_range: str = ""  # e.g. "18-22"；勿绑供应商字段
    place_label: str = ""


@dataclass(frozen=True)
class ContextLocation(PerceptionEvent):
    """粗位置标签（校园 / 家 / 通勤等）。

    **勿要求存精确坐标**；精确定位留在基础设施且默认不入库。
    """

    kind: str = "context.location"
    place_label: str = ""
    coarse_area: str = ""


@dataclass(frozen=True)
class NavRouteProgress(PerceptionEvent):
    """导航进度事件，供路线提醒决策。"""

    kind: str = "nav.route_progress"
    route_label: str = ""
    next_step: str = ""
    eta_minutes: int | None = None
    progress_pct: int | None = None
