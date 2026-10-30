"""全天上下文：可信日程 + 天气 → 最小打扰的主动提醒（G3 场景 1）。

原则（任务书 §0.4「不扰人」）：

- 日程事件必须带**可信来源**（如日历导入/owner 手工录入），陌生来源拒绝入库；
- 天气快照必须带**来源**（URL 或提供方标签），提醒草稿的 evidence 同时
  包含事件来源与天气来源——可点击、可追溯；
- 提醒只是 ProactiveDraft：走既有 Outbox 策略闸门（enabled/渠道许可/
  静默时段/日额度），可 revoke、可整体关闭；本模块**不发送任何消息**；
- 不下雨 / 无课 / 超窗 → 零产出（宁可漏提醒，不可乱打扰）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs

_EVENTS_KEY = "life:dayplan:events"
_DEPARTURE_HINTS = (
    "过会去上课", "过会儿去上课", "一会去上课", "一会儿去上课", "等会去上课",
    "等会要去上课", "马上去上课", "去上课了", "准备去上课",
    "过会出门", "一会出门", "一会儿出门", "待会出门", "马上出门",
)


@dataclass(frozen=True)
class DayEvent:
    """一条可信日程（课程/约会）；source 是可追溯来源，必填。"""

    event_id: str
    title: str
    start: datetime
    end: datetime
    location: str
    source: str  # 例："calendar-import:owner" / "manual:qq"

    def as_dict(self) -> dict:
        return {"event_id": self.event_id, "title": self.title,
                "start": self.start.isoformat(), "end": self.end.isoformat(),
                "location": self.location, "source": self.source}

    @classmethod
    def from_dict(cls, data: dict) -> "DayEvent":
        return cls(event_id=str(data["event_id"]), title=str(data["title"]),
                   start=datetime.fromisoformat(data["start"]),
                   end=datetime.fromisoformat(data["end"]),
                   location=str(data.get("location", "")),
                   source=str(data["source"]))


@dataclass(frozen=True)
class WeatherSnapshot:
    """某地点某时刻的天气快照；source 必填（可点击来源语义）。"""

    condition: str
    rain: bool
    source: str
    at: datetime


class WeatherProvider(Protocol):
    """天气提供方契约：真实实现经 web.fetch/供应商 API；测试用替身。"""

    def snapshot(self, location: str, when: datetime) -> WeatherSnapshot:
        ...


class DayPlanStore:
    """owner 日程的可信存储（只接受带来源的事件）。"""

    def __init__(self, store) -> None:
        self.store = store

    def add_event(self, *, title: str, start: datetime, end: datetime,
                  location: str = "", source: str) -> DayEvent:
        title = (title or "").strip()
        source = (source or "").strip()
        if not title:
            raise ValueError("title required")
        if not source:
            raise ValueError("trusted source required")  # 陌生来源事件不入库
        if end <= start:
            raise ValueError("end must be after start")
        event = DayEvent(event_id=uuid4().hex, title=title[:120], start=start,
                         end=end, location=location[:120], source=source[:200])
        events = self._read()
        events.append(event.as_dict())
        self.store.put(_EVENTS_KEY, events[-200:])  # 有界
        return event

    def remove(self, event_id: str) -> bool:
        events = self._read()
        kept = [e for e in events if e.get("event_id") != event_id]
        if len(kept) == len(events):
            return False
        self.store.put(_EVENTS_KEY, kept)
        return True

    def upcoming(self, now: datetime, *, within_hours: float = 4.0) -> list[DayEvent]:
        if within_hours <= 0:
            raise ValueError("within_hours must be positive")
        horizon = now + timedelta(hours=within_hours)
        out = []
        for raw in self._read():
            try:
                event = DayEvent.from_dict(raw)
            except (KeyError, ValueError, TypeError):
                continue
            if event.start <= now < event.end or now <= event.start <= horizon:
                out.append(event)
        return sorted(out, key=lambda e: e.start)

    def _read(self) -> list[dict]:
        raw = self.store.get(_EVENTS_KEY)
        return list(raw) if isinstance(raw, list) else []


class ContextReminder:
    """「过会去上课/出门」触发的情景提醒评估器（规则触发，不调模型）。"""

    def __init__(self, store, outbox: ProactiveOutbox, prefs: ProactivePrefs,
                 weather: WeatherProvider, *, channel: str = "direct_message",
                 now_fn=None) -> None:
        self.store = store
        self.outbox = outbox
        self.prefs = prefs
        self.weather = weather
        self.channel = channel
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.day_plan = DayPlanStore(store)

    def evaluate_departure(self, text: str, *, conversation_id: str = "") -> dict:
        """评估一条用户消息是否触发「带伞」类提醒；返回结构化结论。

        返回键：triggered / draft_id / verdict / reason。零产出场景
        （disabled/无课/晴天/超窗）都如实体现在 reason，不静默。
        """
        text = (text or "").strip()
        if not self.prefs.enabled:
            return {"triggered": False, "draft_id": None,
                    "verdict": "", "reason": "proactivity-disabled"}
        if not any(hint in text for hint in _DEPARTURE_HINTS):
            return {"triggered": False, "draft_id": None,
                    "verdict": "", "reason": "no-departure-intent"}
        now = self._now()
        events = self.day_plan.upcoming(now, within_hours=4.0)
        if not events:
            return {"triggered": False, "draft_id": None,
                    "verdict": "", "reason": "no-upcoming-event"}
        event = events[0]
        try:
            weather = self.weather.snapshot(event.location, event.start)
        except Exception as exc:
            # 天气不可用：诚实不提醒（不猜天气）
            return {"triggered": False, "draft_id": None, "verdict": "",
                    "reason": f"weather-unavailable:{type(exc).__name__}"}
        if not weather.rain:
            return {"triggered": False, "draft_id": None, "verdict": "",
                    "reason": "weather-clear"}
        draft_id = f"ctx:umbrella:{event.event_id}"
        when = event.start.astimezone().strftime("%H:%M")
        draft = ProactiveDraft(
            draft_id=draft_id,
            text=(f"你{when}有「{event.title}」，{event.location or '那边'}"
                  f"{weather.condition or '有雨'}——记得带伞。"),
            reason="owner 提到马上去上课/出门，且日程与天气来源都显示有雨",
            evidence=(event.source, weather.source),
            channel=self.channel,
            importance=0.6,
            expires_at=event.end,
            topic=f"umbrella:{event.event_id}",  # 同一事件 24h 内不重复提醒
        )
        if not self.outbox.propose(draft):
            return {"triggered": True, "draft_id": draft_id, "verdict": "",
                    "reason": "duplicate-or-deduped"}
        verdict = self.outbox.inspect(draft_id, self.prefs, now=now)
        return {"triggered": True, "draft_id": draft_id,
                "verdict": verdict.status, "reason": "rain-before-event"}
