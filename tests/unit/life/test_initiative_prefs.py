"""issue #42：主动行为统一配置源与渠道默认档位测试（离线）。"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from whale_core.life.initiative import (
    PREFS_KEY,
    ProactiveDraft,
    ProactiveOutbox,
    ProactivePrefs,
    load_prefs,
)
from whale_core.life.rhythm import RhythmStore
from whale_core.memory.store import InMemoryStore


class TestPrefsKvSource:
    def test_kv_overrides_default(self):
        store = InMemoryStore()
        store.put(PREFS_KEY, {"daily_limit": 5, "quiet_start": "23:30"})
        prefs = load_prefs(store, ProactivePrefs(
            enabled=True, allowed_channels=frozenset({"direct_message"}),
            daily_limit=2))
        assert prefs.daily_limit == 5
        assert prefs.quiet_start == time(23, 30)
        assert prefs.quiet_end == time(7)  # 未覆盖字段回落默认

    def test_null_field_restores_default_semantics(self):
        store = InMemoryStore()
        store.put(PREFS_KEY, {"daily_limit": 5})
        # 删除覆盖 → 回落
        data = store.get(PREFS_KEY)
        data.pop("daily_limit")
        store.put(PREFS_KEY, data)
        prefs = load_prefs(store, ProactivePrefs(daily_limit=2))
        assert prefs.daily_limit == 2

    def test_corrupt_kv_falls_back(self):
        store = InMemoryStore()
        store.put(PREFS_KEY, "not-a-dict")
        prefs = load_prefs(store, ProactivePrefs(daily_limit=9))
        assert prefs.daily_limit == 9  # 坏 kv 完全忽略，回落 env

    def test_safety_flags_not_kv_editable(self):
        """enabled/allowed_channels 等安全开关不受 kv 影响（部署侧专属）。"""
        store = InMemoryStore()
        store.put(PREFS_KEY, {"enabled": True, "daily_limit": 3,
                              "allowed_channels": ["direct_message"]})
        prefs = load_prefs(store, ProactivePrefs(enabled=False))
        assert prefs.enabled is False           # kv 提不动
        assert prefs.allowed_channels == frozenset()
        assert prefs.daily_limit == 3           # 白名单字段生效


class TestQuietHoursBehavior:
    """四类行为正/负例：主动提醒、群插话（档位）、深夜主动、被动回复豁免。"""

    def _outbox(self, store):
        return ProactiveOutbox(store, topic_dedup_hours=0)

    def _prefs(self, **kw):
        base = dict(enabled=True, allowed_channels=frozenset({"direct_message"}),
                    auto_send=True, daily_limit=10)
        base.update(kw)
        return ProactivePrefs(**base)

    def test_proactive_blocked_in_quiet(self):
        store = InMemoryStore()
        outbox = ProactiveOutbox(store, topic_dedup_hours=0)
        draft = ProactiveDraft(
            draft_id="d1", channel="direct_message", text="主动提醒",
            reason="t", evidence=("e",))
        outbox.propose(draft)
        night = datetime(2026, 11, 1, 19, 30, tzinfo=timezone.utc)  # = CST 03:30
        prefs = self._prefs(quiet_start=time(22), quiet_end=time(7))
        verdict = outbox.inspect("d1", prefs, now=night)
        assert verdict.status == "deferred"
        assert "quiet" in verdict.reason

    def test_proactive_allowed_daytime(self):
        store = InMemoryStore()
        outbox = ProactiveOutbox(store, topic_dedup_hours=0)
        outbox.propose(ProactiveDraft(
            draft_id="d2", channel="direct_message", text="主动提醒",
            reason="t", evidence=("e",)))
        noon = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)  # = CST 12:00
        verdict = outbox.inspect("d2", self._prefs(), now=noon)
        # direct_message 永远人工审批（既定政策）；白天不触发静默压制
        assert verdict.status == "needs_approval"

    def test_passive_reply_exempt_in_quiet(self):
        store = InMemoryStore()
        outbox = ProactiveOutbox(store, topic_dedup_hours=0)
        outbox.propose(ProactiveDraft(
            draft_id="d3", channel="direct_message", text="被动回复",
            reason="t", evidence=("e",)))
        night = datetime(2026, 11, 1, 19, 30, tzinfo=timezone.utc)  # = CST 03:30
        verdict = outbox.inspect("d3", self._prefs(), now=night,
                                 response_origin="reactive_inbound")
        # #22：被动回复豁免静默——不应是 deferred；direct_message 仍需人工审批
        assert verdict.status == "needs_approval"

    def test_daily_budget_caps_proactive(self):
        store = InMemoryStore()
        outbox = ProactiveOutbox(store, topic_dedup_hours=0)
        prefs = self._prefs(daily_limit=1)
        noon = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)  # = CST 12:00
        outbox.propose(ProactiveDraft(
            draft_id="d4", channel="direct_message", text="a",
            reason="t", evidence=("e",)))
        outbox.propose(ProactiveDraft(
            draft_id="d5", channel="direct_message", text="b",
            reason="t", evidence=("e",)))
        # direct_message 需显式审批后才真正外发（既定政策）；审批后计配额
        v1 = outbox.dispatch("d4", prefs, now=noon, explicitly_approved=True,
                             port=type("P", (), {"send": lambda self, d: True})())
        assert v1.status in ("sent", "accepted")  # 渠道受理（非伪报送达）
        v2 = outbox.inspect("d5", prefs, now=noon)
        assert v2.status == "over_budget"


class TestChannelDefaultRhythm:
    def test_channel_default_applies_and_conversation_wins(self):
        store = InMemoryStore()
        rs = RhythmStore(store)
        rs.set_channel_default("qq", activity="lively", max_segments=2)
        # 会话未设置 → 渠道默认
        r = rs.get_with_channel_default("qq:qq_group:group:1", "qq")
        assert r.activity == "lively" and r.max_segments == 2
        # 会话级设置优先于渠道默认
        from whale_core.life.rhythm import ConversationRhythm
        rs.put("qq:qq_group:group:1", ConversationRhythm(activity="quiet"))
        r = rs.get_with_channel_default("qq:qq_group:group:1", "qq")
        assert r.activity == "quiet"
        # 其他渠道不受影响
        assert rs.get_with_channel_default("x", "call").activity == "balanced"

    def test_clear_channel_default(self):
        store = InMemoryStore()
        rs = RhythmStore(store)
        rs.set_channel_default("qq", activity="lively")
        rs.set_channel_default("qq", activity="")
        assert rs.channel_default("qq") is None
