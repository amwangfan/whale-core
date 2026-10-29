"""Auditable proactive-message outbox. No implicit network or device access.

Single-worker MVP ledger: persistent via MemoryStore; distributed senders will require
an atomic claim/lease and provider-side idempotency before production deployment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, time
from typing import Protocol
from zoneinfo import ZoneInfo

from whale_core.memory.store import MemoryStore

CHANNELS = frozenset({"hud", "phone_notice", "direct_message", "call"})


@dataclass(frozen=True)
class ProactiveDraft:
    draft_id: str
    text: str
    reason: str
    evidence: tuple[str, ...]
    channel: str = "hud"
    importance: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    # 主题标签（可选）：主动行为的主题去重键（roadmap P2-3），如 "weather:shanghai"。
    topic: str = ""

    def __post_init__(self) -> None:
        if not self.draft_id or not self.text.strip() or not self.reason.strip() or not self.evidence:
            raise ValueError("draft requires id, text, reason and evidence")
        if self.channel not in CHANNELS:
            raise ValueError("unknown channel")
        if not 0 <= self.importance <= 1:
            raise ValueError("importance outside [0,1]")

    def as_dict(self) -> dict:
        return {"draft_id": self.draft_id, "text": self.text[:800],
                "reason": self.reason[:400], "evidence": list(self.evidence)[:10],
                "channel": self.channel, "importance": self.importance,
                "created_at": self.created_at.isoformat(),
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "topic": self.topic[:80]}

    @classmethod
    def from_dict(cls, data: dict) -> ProactiveDraft:
        return cls(draft_id=data["draft_id"], text=data["text"],
                   reason=data["reason"], evidence=tuple(data["evidence"]),
                   channel=data["channel"], importance=float(data["importance"]),
                   created_at=datetime.fromisoformat(data["created_at"]),
                   expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
                   topic=str(data.get("topic") or ""))


@dataclass(frozen=True)
class ProactivePrefs:
    enabled: bool = False
    allowed_channels: frozenset[str] = frozenset()
    auto_send: bool = False
    allow_calls: bool = False
    daily_limit: int = 2
    quiet_start: time = time(22)
    quiet_end: time = time(7)
    tz: str = "Asia/Shanghai"
    # 被动回复与主动行为是两套策略（issue #13）：
    # - passive_reply_in_quiet：被动回复（仅回应已配对 owner 的入站消息）是否豁免
    #   静默时段。豁免的正当性：对话由 owner 发起，回复是对话延续而非打扰；
    #   **主动行为永远不豁免**。核心默认 False（保守），由运行器显式开启并公示。
    # - passive_daily_limit：被动回复的独立额度（防跑飞循环的保险丝），
    #   None = 回落 daily_limit；配额计数也与主动分开（life:quota:passive:*）。
    passive_reply_in_quiet: bool = False
    passive_daily_limit: int | None = None

    def __post_init__(self) -> None:
        if not set(self.allowed_channels) <= CHANNELS:
            raise ValueError("invalid allowed channels")
        if self.daily_limit < 0:
            raise ValueError("negative daily limit")
        if self.passive_daily_limit is not None and self.passive_daily_limit < 0:
            raise ValueError("negative passive daily limit")


@dataclass(frozen=True)
class DispatchVerdict:
    status: str
    reason: str
    draft_id: str


class OutboundPort(Protocol):
    def send(self, draft: ProactiveDraft) -> bool:
        """True = channel accepted this request; NOT proof a human read it."""
        ...


class ProactiveOutbox:
    def __init__(self, store: MemoryStore, *, topic_dedup_hours: float = 24.0,
                 now_fn=None):
        self.store = store
        # 主题去重窗口（roadmap P2-3）：同 topic 的草稿在窗口内不重复提案；
        # 0 = 关闭去重。topic 为空的草稿不参与去重（幂等键仍是 draft_id）。
        self.topic_dedup_hours = topic_dedup_hours
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def propose(self, draft: ProactiveDraft) -> bool:
        key = f"life:draft:{draft.draft_id}"
        if self.store.get(key) is not None:
            return False
        if draft.topic and self._topic_seen(draft.topic):
            return False
        self.store.put(key, draft.as_dict())
        self.store.put(f"life:ledger:{draft.draft_id}",
                       {"status": "draft", "reason": "awaiting policy", "attempts": 0})
        if draft.topic:
            self._topic_mark(draft.topic)
        return True

    _TOPICS_KEY = "life:topics:recent"

    def _topic_seen(self, topic: str) -> bool:
        if self.topic_dedup_hours <= 0:
            return False
        now = self._now()
        window_s = self.topic_dedup_hours * 3600
        for entry in self._topic_entries():
            if entry["topic"] == topic and (now - entry["at"]).total_seconds() < window_s:
                return True
        return False

    def _topic_mark(self, topic: str) -> None:
        entries = [e for e in self._topic_entries()
                   if (self._now() - e["at"]).total_seconds() < self.topic_dedup_hours * 3600
                   and e["topic"] != topic]
        entries.append({"topic": topic, "at": self._now().isoformat()})
        self.store.put(self._TOPICS_KEY, entries[-200:])

    def _topic_entries(self) -> list[dict]:
        raw = self.store.get(self._TOPICS_KEY)
        if not isinstance(raw, list):
            return []
        out = []
        for e in raw:
            if not isinstance(e, dict) or not e.get("topic"):
                continue
            try:
                at = datetime.fromisoformat(str(e["at"]))
            except (ValueError, KeyError):
                continue
            out.append({"topic": str(e["topic"]), "at": at})
        return out

    def get(self, draft_id: str) -> ProactiveDraft | None:
        data = self.store.get(f"life:draft:{draft_id}")
        return ProactiveDraft.from_dict(data) if data else None

    @staticmethod
    def _is_quiet(local: time, prefs: ProactivePrefs) -> bool:
        start, end = prefs.quiet_start, prefs.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= local < end
        return local >= start or local < end

    def inspect(self, draft_id: str, prefs: ProactivePrefs, *, now: datetime,
                busy: bool = False, explicitly_approved: bool = False,
                passive_reply: bool = False) -> DispatchVerdict:
        draft = self.get(draft_id)
        if draft is None:
            return DispatchVerdict("missing", "not found", draft_id)
        ledger = self.store.get(f"life:ledger:{draft_id}") or {}
        if ledger.get("attempts", 0) > 0:
            return DispatchVerdict("already_attempted", "no automatic repeat", draft_id)
        if not prefs.enabled:
            return DispatchVerdict("disabled", "proactivity disabled", draft_id)
        if draft.channel not in prefs.allowed_channels:
            return DispatchVerdict("channel_denied", "channel not enabled", draft_id)
        if draft.channel == "call" and not prefs.allow_calls:
            return DispatchVerdict("call_denied", "calls need separate opt-in", draft_id)
        if draft.expires_at is not None and now >= draft.expires_at:
            return DispatchVerdict("expired", "candidate expired", draft_id)
        local = now.astimezone(ZoneInfo(prefs.tz))
        quiet_exempt = passive_reply and prefs.passive_reply_in_quiet
        if (self._is_quiet(local.time(), prefs) and not quiet_exempt) or busy:
            return DispatchVerdict("deferred", "quiet hours or busy context", draft_id)
        date = local.date().isoformat()
        limit, count = self._quota_view(prefs, date, passive_reply=passive_reply)
        if count >= limit:
            return DispatchVerdict("over_budget", "daily delivery cap", draft_id)
        # A call is always reviewed; auto_send is limited to explicitly allowed low-risk channels.
        if not explicitly_approved and (draft.channel in {"call", "direct_message"} or not prefs.auto_send):
            return DispatchVerdict("needs_approval", "human review required", draft_id)
        return DispatchVerdict("ready", "authorized and within budget", draft_id)

    def _quota_view(self, prefs: ProactivePrefs, date: str, *, passive_reply: bool) -> tuple[int, int]:
        """(额度上限, 当日已用)——被动回复走独立配额键，不与主动行为混账。"""
        if passive_reply:
            key = f"life:quota:passive:{date}"
            limit = prefs.passive_daily_limit if prefs.passive_daily_limit is not None else prefs.daily_limit
        else:
            key = f"life:quota:{date}"
            limit = prefs.daily_limit
        return limit, int(self.store.get(key) or 0)

    def dispatch(self, draft_id: str, prefs: ProactivePrefs, *, now: datetime,
                 port: OutboundPort, busy: bool = False,
                 explicitly_approved: bool = False,
                 passive_reply: bool = False) -> DispatchVerdict:
        verdict = self.inspect(draft_id, prefs, now=now, busy=busy,
                               explicitly_approved=explicitly_approved,
                               passive_reply=passive_reply)
        if verdict.status != "ready":
            self.store.put(f"life:last_review:{draft_id}",
                           {"status": verdict.status, "reason": verdict.reason,
                            "at": now.isoformat()})
            return verdict
        draft = self.get(draft_id)
        assert draft is not None
        # Record attempt BEFORE external call. Unknown outcome will not be blindly retried.
        self.store.put(f"life:ledger:{draft_id}",
                       {"status": "attempting", "reason": "outbound attempt", "attempts": 1,
                        "at": now.isoformat()})
        local = now.astimezone(ZoneInfo(prefs.tz))
        date = local.date().isoformat()
        quota_key = (f"life:quota:passive:{date}" if passive_reply else f"life:quota:{date}")
        self.store.put(quota_key, (self.store.get(quota_key) or 0) + 1)
        try:
            accepted = port.send(draft)
        except Exception:
            accepted = False
        status = "accepted" if accepted else "unknown"
        self.store.put(f"life:ledger:{draft_id}",
                       {"status": status, "reason": "channel accepted" if accepted else "delivery not confirmed",
                        "attempts": 1, "at": now.isoformat()})
        return DispatchVerdict(status, "sent to channel" if accepted else "uncertain; manual review", draft_id)

    def feedback(self, draft_id: str, feedback: str) -> None:
        if feedback not in {"useful", "irrelevant", "dismissed", "not_now"}:
            raise ValueError("unsupported feedback")
        if self.get(draft_id) is None:
            raise KeyError(draft_id)
        self.store.put(f"life:feedback:{draft_id}", {"value": feedback,
                       "at": datetime.now(timezone.utc).isoformat()})

    def ledger(self, draft_id: str) -> dict:
        return self.store.get(f"life:ledger:{draft_id}") or {}
