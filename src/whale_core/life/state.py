"""鲸鱼的内在状态：表达风格、兴趣与未完话题，持久化于 MemoryStore。

状态只参与人格表达与话题连续性（issue #54 会把它接进对话上下文）；
它不含、也不授予任何权限。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from whale_core.memory.store import MemoryStore

STATE_KEY = "life:state:v1"
#: 常用的风格标签（建议值，不是白名单——issue #59 移除阶段性约束）。
ALLOWED_STYLES = frozenset({"neutral", "focused", "curious", "quiet"})
MAX_STYLE_CHARS = 24


@dataclass(frozen=True)
class InnerState:
    style: str = "neutral"
    interests: tuple[str, ...] = ()
    unfinished_topics: tuple[str, ...] = ()
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict:
        return {"style": self.style, "interests": list(self.interests),
                "unfinished_topics": list(self.unfinished_topics), "updated_at": self.updated_at}


class InnerStateStore:
    def __init__(self, store: MemoryStore):
        self.store = store

    def load(self) -> InnerState:
        data = self.store.get(STATE_KEY) or {}
        return InnerState(style=data.get("style", "neutral"),
                          interests=tuple(data.get("interests", ())),
                          unfinished_topics=tuple(data.get("unfinished_topics", ())),
                          updated_at=data.get("updated_at") or datetime.now(timezone.utc).isoformat())

    def update(self, *, style: str | None = None, add_interest: str | None = None,
               open_topic: str | None = None, close_topic: str | None = None) -> InnerState:
        old = self.load()
        new_style = style if style is not None else old.style
        if not isinstance(new_style, str) or not new_style.strip():
            raise ValueError("style must be a non-empty label")
        new_style = new_style.strip()[:MAX_STYLE_CHARS]
        interests = list(old.interests)
        if add_interest and add_interest not in interests:
            interests.append(add_interest[:100])
        topics = list(old.unfinished_topics)
        if close_topic:
            topics = [t for t in topics if t != close_topic]
        if open_topic and open_topic not in topics:
            topics.append(open_topic[:160])
        new = InnerState(style=new_style, interests=tuple(interests[-12:]),
                         unfinished_topics=tuple(topics[-12:]))
        self.store.put(STATE_KEY, new.as_dict())
        return new

    def reset(self) -> None:
        self.store.delete(STATE_KEY)

