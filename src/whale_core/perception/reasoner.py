"""情境推理 stub：course / weather / location / ambient → SituationBrief。

规则 / 模板即可，不调用真实 LLM，不拉天气/日历 SDK，不存音频字节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from whale_core.perception.ambient import extract_ambient_events
from whale_core.perception.events import (
    AmbientSpeechSegment,
    ContextCourse,
    ContextLocation,
    ContextWeather,
    LocationHint,
    PerceptionEvent,
    ScheduleTick,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SituationBrief:
    """结构化情境摘要，供 agent decide 引用。"""

    headline: str = "情境未知"
    course_note: str = ""
    weather_note: str = ""
    location_note: str = ""
    schedule_note: str = ""
    ambient_note: str = ""
    suggestions: tuple[str, ...] = ()
    urgency_hint: str = "normal"  # normal | elevated | quiet_preferred
    raw_kinds: tuple[str, ...] = ()
    reasoned_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "course_note": self.course_note,
            "weather_note": self.weather_note,
            "location_note": self.location_note,
            "schedule_note": self.schedule_note,
            "ambient_note": self.ambient_note,
            "suggestions": list(self.suggestions),
            "urgency_hint": self.urgency_hint,
            "raw_kinds": list(self.raw_kinds),
            "reasoned_at": self.reasoned_at.isoformat(),
        }


class ContextReasoner:
    """非 LLM 情境推理：按事件类型填模板。"""

    def reason(self, events: Sequence[PerceptionEvent]) -> SituationBrief:
        course_note = ""
        weather_note = ""
        location_note = ""
        schedule_note = ""
        ambient_note = ""
        suggestions: list[str] = []
        urgency = "normal"
        kinds: list[str] = []

        for e in events:
            kinds.append(e.kind)
            if isinstance(e, ContextCourse) or e.kind == "context.course":
                title = getattr(e, "course_title", "") or (e.payload or {}).get(
                    "course_title", ""
                )
                loc = getattr(e, "location_label", "") or (e.payload or {}).get(
                    "location_label", ""
                )
                starts = getattr(e, "starts_at", None) or (e.payload or {}).get("starts_at")
                when = ""
                if isinstance(starts, datetime):
                    when = starts.isoformat()
                elif starts:
                    when = str(starts)
                bits = [b for b in (str(title), str(loc), when) if b]
                course_note = "课表：" + " · ".join(bits) if bits else "课表：有课程事件"
                suggestions.append("课前安静提醒（若未被打扰策略抑制）")
                # 课表临近默认倾向少打扰
                urgency = "quiet_preferred"

            elif isinstance(e, ContextWeather) or e.kind == "context.weather":
                cond = getattr(e, "condition", "") or (e.payload or {}).get("condition", "")
                temp = getattr(e, "temp_c_range", "") or (e.payload or {}).get(
                    "temp_c_range", ""
                )
                weather_note = f"天气：{cond}" + (f"（{temp}℃）" if temp else "")
                if cond and any(x in str(cond) for x in ("雨", "雪", "storm", "rain")):
                    suggestions.append("出门前提示带伞/防滑")
                    if urgency == "normal":
                        urgency = "elevated"

            elif isinstance(e, ContextLocation) or e.kind == "context.location":
                place = getattr(e, "place_label", "") or (e.payload or {}).get(
                    "place_label", ""
                )
                area = getattr(e, "coarse_area", "") or (e.payload or {}).get(
                    "coarse_area", ""
                )
                location_note = "位置：" + " / ".join(p for p in (str(place), str(area)) if p)

            elif isinstance(e, LocationHint) or e.kind == "location.hint":
                place = getattr(e, "place_label", "") or (e.payload or {}).get(
                    "place_label", ""
                )
                if place and not location_note:
                    location_note = f"位置：{place}"

            elif isinstance(e, ScheduleTick) or e.kind == "schedule.tick":
                title = getattr(e, "title", "") or (e.payload or {}).get("title", "")
                schedule_note = f"日程：{title}" if title else "日程：tick"
                suggestions.append("按日程提醒（受打扰策略约束）")

            elif isinstance(e, AmbientSpeechSegment) or e.kind == "ambient.speech_segment":
                tags = extract_ambient_events(e)
                ambient_note = tags.text_summary()
                if tags.summary_label:
                    # 确保 ambient_note 含 summary_label（测试断言）
                    if tags.summary_label not in ambient_note:
                        ambient_note = f"{ambient_note}（{tags.summary_label}）"
                suggestions.append("环境语音事件已提取")
                # 环境语音本身不强制升紧急；由上游 context 决定是否主动呼叫

        headline_parts = [
            p
            for p in (
                course_note,
                weather_note,
                location_note,
                schedule_note,
                ambient_note,
            )
            if p
        ]
        headline = "；".join(headline_parts) if headline_parts else "情境未知"

        # 去重 suggestions 保序
        seen: set[str] = set()
        uniq_sugg: list[str] = []
        for s in suggestions:
            if s not in seen:
                seen.add(s)
                uniq_sugg.append(s)

        return SituationBrief(
            headline=headline,
            course_note=course_note,
            weather_note=weather_note,
            location_note=location_note,
            schedule_note=schedule_note,
            ambient_note=ambient_note,
            suggestions=tuple(uniq_sugg),
            urgency_hint=urgency,
            raw_kinds=tuple(kinds),
        )
