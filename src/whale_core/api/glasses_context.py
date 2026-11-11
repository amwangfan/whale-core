"""Request-only glasses facts and replaceable phone calendar snapshots."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SNAPSHOT_KEY = "glasses:context:snapshot"
REQUEST_FACTS: ContextVar[dict | None] = ContextVar("glasses_facts", default=None)
LABELS = {"现在", "今日日程", "明日日程", "天气"}


def split_chat_context(text: str, context: dict | None = None) -> tuple[str, dict]:
    """Accept the shipped ChatContext.wrap format without treating facts as speech."""
    facts = {}
    lines = text.splitlines(keepends=True)
    consumed = 0
    for line in lines:
        if line.startswith("[用户]") and facts:
            user = "".join(lines[consumed:])[len("[用户]"):].strip()
            facts.update(context or {})
            return user, facts
        if not line.startswith("[") or "]" not in line:
            break
        label, value = line[1:].split("]", 1)
        if label not in LABELS:
            break
        facts[label] = value.strip()[:1500]
        consumed += 1
    # Partial or ordinary text is not silently removed.
    return text.strip(), dict(context or {})


def save_context_snapshot(store, body: dict) -> dict:
    """Replace a calendar window; [] means empty, null means not readable."""
    calendar = body.get("calendar")
    if not isinstance(calendar, dict):
        raise ValueError("calendar must map ISO dates to event arrays or null")
    normalized = {}
    for day, events in calendar.items():
        day = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
        if events is None:
            normalized[day] = None
            continue
        if not isinstance(events, list):
            raise ValueError("calendar entries must be arrays or null")
        normalized[day] = []
        for event in events[:100]:
            if not isinstance(event, dict) or not str(event.get("title") or "").strip():
                raise ValueError("event title required")
            if "allDay" in event and not isinstance(event["allDay"], bool):
                raise ValueError("allDay must be boolean")
            normalized[day].append({
                key: (bool(event[key]) if key == "allDay" else str(event[key])[:300])
                for key in ("id", "title", "start", "end", "allDay") if key in event})
    now = datetime.now(SHANGHAI).isoformat()
    snapshot = {"calendar": normalized, "source": str(body.get("source") or "android.calendar")[:120],
                "observedAt": str(body.get("observedAt") or now), "receivedAt": now}
    # Weather remains request-only: a calendar refresh must not extend its life.
    store.put(SNAPSHOT_KEY, snapshot)
    return snapshot


def render_context(facts: dict | None, *, now: datetime | None = None) -> str:
    """Render bounded dated data; the model's current clock remains server-owned."""
    if not facts:
        return ""
    now = now or datetime.now(SHANGHAI)
    lines = []
    calendar = facts.get("calendar")
    if isinstance(calendar, dict):
        for offset, label in ((0, "今日日程"), (1, "明日日程")):
            day = (now + timedelta(days=offset)).date().isoformat()
            if day not in calendar:
                continue
            events = calendar[day]
            if events is None:
                value = "未能读取"
            elif not events:
                value = "（无）"
            else:
                value = "、".join(
                    (str("全天" if e.get("allDay") else e.get("start") or "")
                     + " " + str(e.get("title") or "")).strip()
                    for e in events[:30] if isinstance(e, dict))
            lines.append(f"[{label} {day}] {value}")
    # A legacy block has a client date anchor; do not roll yesterday's tomorrow forward.
    anchor = str(facts.get("现在") or "")[:10]
    legacy_current = not anchor or anchor == now.date().isoformat()
    for label in ("今日日程", "明日日程", "天气"):
        value = facts.get(label)
        if value and legacy_current:
            lines.append(f"[{label}] {str(value)[:1500]}")
    weather = facts.get("weather")
    if isinstance(weather, dict):
        text = str(weather.get("text") or "")
        age = weather.get("ageSec", 0)
        if text:
            suffix = "（缓存，可能已变化）" if isinstance(age, (int, float)) and age > 1800 else ""
            lines.append("[天气] " + text[:500] + suffix)
    elif isinstance(weather, str) and weather:
        lines.append("[天气] " + weather[:500])
    if not lines:
        return ""
    return "当轮手机事实资料：\n" + "\n".join(lines)[:3000]


def request_context_hint(store) -> str:
    snapshot = store.get(SNAPSHOT_KEY) or {}
    current = REQUEST_FACTS.get() or {}
    # Explicit current facts override synchronized facts, never modify the snapshot.
    merged = dict(snapshot)
    merged.update(current)
    current_calendar = current.get("calendar")
    if isinstance(current_calendar, dict):
        # Calendar overrides are per-date: a request carrying only tomorrow must
        # keep the snapshot's today; [] / null still cover their own date.
        snapshot_calendar = snapshot.get("calendar")
        calendar = dict(snapshot_calendar) if isinstance(snapshot_calendar, dict) else {}
        calendar.update(current_calendar)
        merged["calendar"] = calendar
    elif "calendar" in current:
        # An empty request calendar is not an explicit clear (POST sync only):
        # the synced window stays visible for this turn.
        merged["calendar"] = snapshot.get("calendar")
    anchor = str(current.get("现在") or "")[:10]
    today = datetime.now(SHANGHAI).date()
    legacy_current = not anchor or anchor == today.isoformat()
    if legacy_current and ("今日日程" in current or "明日日程" in current):
        calendar = dict(merged.get("calendar") or {})
        for offset, label in ((0, "今日日程"), (1, "明日日程")):
            if label in current:
                calendar.pop((today + timedelta(days=offset)).isoformat(), None)
        merged["calendar"] = calendar
    return render_context(merged)
