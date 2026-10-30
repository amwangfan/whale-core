"""自主探索/养鱼能力（PR-17）：兴趣台账、探索窗口退避、候选草稿、日记。

诚实边界：
- 本模块 **不联网、不抓站**：探索内容只来自外部供给的 ``DiscoveredContent``
  元数据（标题/摘要/URL/来源名），一律视为不可信数据而非指令。
- 匹配到的候选只进 ``ProactiveOutbox`` 草稿箱（``life:draft:*``），**不直接
  发送**；发送仍走 initiative.py 的 inspect/dispatch 人工审批与配额链。
- 「已核查」语义沿用 ``DiscoveredContent.inspected`` 字段：True 必须由真实
  来源适配器证据支撑，本模块绝不自行置真；inspected=False 不阻断候选，
  但会在草稿 reason 中如实标注（由 DiscoveryPlanner 负责措辞）。

持久化（MemoryStore kv）：
- ``life:explore:interests``      兴趣台账 {小写键: {topic, weight}}
- ``life:explore:last_at``        上次真实探索时间（ISO8601）
- ``life:explore:backoff``        无回应退避等级（int，0..max_backoff）
- ``life:explore:diary:{day}``    当日日记条目列表（day 为 UTC 日期）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from whale_core.life.discovery import DiscoveredContent, DiscoveryPlanner
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox
from whale_core.life.state import InnerState
from whale_core.memory.store import MemoryStore

WEIGHT_MIN, WEIGHT_MAX = 0.0, 2.0
DIARY_NOTE_MAX = 500

INTERESTS_KEY = "life:explore:interests"
LAST_AT_KEY = "life:explore:last_at"
BACKOFF_KEY = "life:explore:backoff"
DIARY_KEY_PREFIX = "life:explore:diary:"

SIGNALS = frozenset({"liked", "disliked", "ignored"})
_SIGNAL_DELTA = {"liked": 0.2, "disliked": -0.3}


def _clamp_weight(value: float) -> float:
    return max(WEIGHT_MIN, min(WEIGHT_MAX, float(value)))


@dataclass(frozen=True)
class Interest:
    topic: str
    weight: float = 1.0  # 反馈调权范围钳制 [0.0, 2.0]


class InterestTracker:
    """兴趣台账（store kv ``life:explore:interests``）。话题大小写不敏感、去重。

    去重键为 strip 后的小写形式；展示名保留首次登记的大小写。权重无论新增
    还是调权都钳制在 [0.0, 2.0]。重复 add 同一话题视为更新权重，不产生重复。
    """

    def __init__(self, store: MemoryStore):
        self.store = store

    @staticmethod
    def _key(topic: str) -> str:
        text = str(topic).strip()
        if not text:
            raise ValueError("topic must be non-empty")
        return text.lower()

    def _load(self) -> dict[str, dict]:
        raw = self.store.get(INTERESTS_KEY) or {}
        return {str(k): {"topic": str(v["topic"]), "weight": _clamp_weight(v["weight"])}
                for k, v in raw.items()}

    def _save(self, data: dict[str, dict]) -> None:
        self.store.put(INTERESTS_KEY, data)

    def add(self, topic: str, *, weight: float = 1.0) -> Interest:
        data = self._load()
        key = self._key(topic)
        if key in data:
            data[key]["weight"] = _clamp_weight(weight)
        else:
            data[key] = {"topic": str(topic).strip(), "weight": _clamp_weight(weight)}
        self._save(data)
        return Interest(topic=data[key]["topic"], weight=data[key]["weight"])

    def adjust(self, topic: str, delta: float) -> Interest | None:
        """按 delta 调权并钳制到 [0.0, 2.0]；话题不存在返回 None（不报错）。"""
        data = self._load()
        key = str(topic).strip().lower()
        if key not in data:
            return None
        data[key]["weight"] = _clamp_weight(data[key]["weight"] + delta)
        self._save(data)
        return Interest(topic=data[key]["topic"], weight=data[key]["weight"])

    def remove(self, topic: str) -> bool:
        data = self._load()
        key = str(topic).strip().lower()
        if key not in data:
            return False
        del data[key]
        self._save(data)
        return True

    def topics(self) -> tuple[Interest, ...]:
        """按 weight 倒序；sorted 稳定，同权重保持登记先后。"""
        ordered = sorted(self._load().values(), key=lambda d: -d["weight"])
        return tuple(Interest(topic=d["topic"], weight=d["weight"]) for d in ordered)

    def as_interest_list(self) -> list[str]:
        """话题名列表（与 topics() 同序），供 InnerState/DiscoveryPlanner 使用。"""
        return [interest.topic for interest in self.topics()]


@dataclass(frozen=True)
class DiaryEntry:
    day: str  # YYYY-MM-DD（UTC）
    note: str
    sources: tuple[str, ...] = ()


class ExplorationManager:
    """探索窗口管理：定时窗口内才探索；候选经 DiscoveryPlanner 过滤后进 Outbox
    草稿（不直接发）；反馈调权；无回应退避（连续 ignored 指数拉长探索间隔）。
    """

    def __init__(self, store: MemoryStore, *, planner: DiscoveryPlanner,
                 now_fn: Callable[[], datetime] | None = None,
                 base_interval_s: int = 21600, max_backoff: int = 4):
        self.store = store
        self.planner = planner
        self.tracker = InterestTracker(store)
        self.outbox = ProactiveOutbox(store)
        self._now_fn = now_fn
        self.base_interval_s = int(base_interval_s)
        self.max_backoff = max(0, int(max_backoff))

    # ------------------------------------------------------------------ time
    def _now(self, now: datetime | None) -> datetime:
        at = (now or (self._now_fn() if self._now_fn is not None else None)
              or datetime.now(timezone.utc))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at

    # -------------------------------------------------------------- explore
    def maybe_explore(self, items: list[DiscoveredContent], *,
                      now: datetime | None = None) -> tuple[ProactiveDraft, ...]:
        """在探索窗口内对外部供给内容做兴趣匹配，产出候选草稿。

        - 兴趣为空（tracker 无话题）→ 不探索返回 ()，且不刷新探索时间戳。
        - 退避门控：距上次真实探索 < base_interval_s * 2**backoff_level → ()。
        - 窗口内：以 InnerState(interests=tracker 话题) 调 planner.drafts()；
          产出草稿自动 propose 进 Outbox（同 draft_id 幂等去重）。
        - 每次真实探索（不论产出几篇）更新 ``life:explore:last_at`` 并记日记。
        """
        at = self._now(now)
        interests = self.tracker.as_interest_list()
        if not interests:
            return ()
        last_raw = self.store.get(LAST_AT_KEY)
        if last_raw is not None:
            last_at = datetime.fromisoformat(str(last_raw))
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=timezone.utc)
            interval = self.base_interval_s * (2 ** self.backoff_level())
            if (at - last_at).total_seconds() < interval:
                return ()
        drafts = self.planner.drafts(list(items),
                                     InnerState(interests=tuple(interests)), now=at)
        for draft in drafts:
            self.outbox.propose(draft)
        self.store.put(LAST_AT_KEY, at.isoformat())
        sources = tuple(dict.fromkeys(url for d in drafts for url in d.evidence))
        self.write_diary(
            f"探索窗口：审查外部供给内容 {len(items)} 条，"
            f"{len(drafts)} 条进入候选草稿箱（未发送）。",
            sources=sources, now=at)
        return drafts

    # -------------------------------------------------------------- feedback
    def feedback(self, topic: str, signal: str) -> None:
        """signal ∈ {"liked","disliked","ignored"}。

        liked +0.2 / disliked -0.3（经 InterestTracker 钳制 [0.0, 2.0]；未知话题
        静默跳过调权）；两者都会把退避等级清零。ignored 不调权，仅退避 +1
        （上限 max_backoff）。
        """
        if signal not in SIGNALS:
            raise ValueError(f"unsupported signal: {signal!r}")
        if signal == "ignored":
            self.store.put(BACKOFF_KEY, min(self.backoff_level() + 1, self.max_backoff))
            return
        self.tracker.adjust(topic, _SIGNAL_DELTA[signal])
        self.store.put(BACKOFF_KEY, 0)

    def backoff_level(self) -> int:
        return int(self.store.get(BACKOFF_KEY) or 0)

    # ----------------------------------------------------------------- diary
    def diary(self, day: str) -> tuple[DiaryEntry, ...]:
        raw = self.store.get(f"{DIARY_KEY_PREFIX}{day}") or []
        return tuple(DiaryEntry(day=str(e.get("day", day)),
                                note=str(e.get("note", "")),
                                sources=tuple(e.get("sources", ())))
                     for e in raw)

    def write_diary(self, note: str, *, sources=(), now: datetime | None = None) -> None:
        """向当日（UTC 日期）日记追加一条；note 截断 500 字符，sources 保留 URL 列表。"""
        at = self._now(now)
        day = at.date().isoformat()
        key = f"{DIARY_KEY_PREFIX}{day}"
        entries = list(self.store.get(key) or [])
        entries.append({"day": day, "note": str(note)[:DIARY_NOTE_MAX],
                        "sources": [str(s) for s in sources]})
        self.store.put(key, entries)
