from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from whale_core.life.channels import Incoming, OwnerChannelRegistry
from whale_core.life.discovery import DiscoveredContent
from whale_core.life.initiative import ProactiveDraft, ProactivePrefs
from whale_core.life.runtime import WhaleLifeRuntime
from whale_core.memory.store import InMemoryStore

TZ = ZoneInfo("Asia/Shanghai")
DAY = datetime(2026, 10, 28, 15, 0, tzinfo=TZ)
NIGHT = datetime(2026, 10, 28, 23, 0, tzinfo=TZ)


class FakePort:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok
    def send(self, draft):
        self.calls.append(draft)
        return self.ok


def make_runtime():
    return WhaleLifeRuntime(InMemoryStore(), allowed_discovery_hosts=frozenset({"www.bilibili.com"}))


def test_exploration_produces_reviewable_draft_not_fake_watch():
    rt = make_runtime()
    rt.state.update(style="curious", add_interest="FPGA")
    items = [
        DiscoveredContent("bv1", "FPGA 入门视频", "https://www.bilibili.com/video/BV1",
                          "B站", summary="FPGA 教程"),
        DiscoveredContent("evil", "FPGA 其他", "http://evil.example/a", "untrusted"),
        DiscoveredContent("other", "美食", "https://www.bilibili.com/video/BV2", "B站"),
    ]
    drafts = rt.consider_discoveries(items, now=DAY)
    assert len(drafts) == 1
    assert drafts[0].draft_id == "discovery:bv1"
    assert "发现" in drafts[0].text and "看完" not in drafts[0].text
    assert rt.outbox.get(drafts[0].draft_id) is not None


def test_default_disabled_and_review_opt_in():
    rt = make_runtime()
    rt.state.update(open_topic="模拟电路错题")
    draft = rt.propose_unfinished_topic("模拟电路错题", evidence_id="chat-1", now=DAY)
    port = FakePort()
    assert rt.deliver(draft.draft_id, prefs=ProactivePrefs(), now=DAY, port=port).status == "disabled"
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"hud"}))
    assert rt.deliver(draft.draft_id, prefs=prefs, now=DAY, port=port).status == "needs_approval"
    assert not port.calls
    assert rt.deliver(draft.draft_id, prefs=prefs, now=DAY, port=port, approved=True).status == "accepted"
    assert len(port.calls) == 1
    assert rt.deliver(draft.draft_id, prefs=prefs, now=DAY, port=port, approved=True).status == "already_attempted"
    rt.outbox.feedback(draft.draft_id, "not_now")


def test_quiet_busy_expiry_and_daily_cap():
    rt = make_runtime()
    prefs = ProactivePrefs(enabled=True, auto_send=True, daily_limit=1,
                           allowed_channels=frozenset({"hud"}))
    draft = ProactiveDraft("1", "提醒", "日程", ("event-1",))
    rt.outbox.propose(draft)
    port = FakePort()
    assert rt.deliver("1", prefs=prefs, now=NIGHT, port=port).status == "deferred"
    assert rt.deliver("1", prefs=prefs, now=DAY, port=port, busy=True).status == "deferred"
    assert rt.deliver("1", prefs=prefs, now=DAY, port=port).status == "accepted"
    rt.outbox.propose(ProactiveDraft("2", "第二条", "日程", ("event-2",)))
    assert rt.deliver("2", prefs=prefs, now=DAY, port=port).status == "over_budget"
    assert len(port.calls) == 1


def test_call_always_needs_separate_permission_and_review():
    rt = make_runtime()
    draft = ProactiveDraft("call-1", "打电话", "提醒", ("event-1",), channel="call")
    rt.outbox.propose(draft)
    port = FakePort()
    prefs = ProactivePrefs(enabled=True, auto_send=True, allowed_channels=frozenset({"call"}))
    assert rt.deliver("call-1", prefs=prefs, now=DAY, port=port, approved=True).status == "call_denied"
    prefs = ProactivePrefs(enabled=True, auto_send=True, allow_calls=True,
                           allowed_channels=frozenset({"call"}))
    assert rt.deliver("call-1", prefs=prefs, now=DAY, port=port).status == "needs_approval"
    assert not port.calls


def test_unknown_delivery_not_retried():
    rt = make_runtime()
    rt.outbox.propose(ProactiveDraft("d", "Hi", "reason", ("event",)))
    prefs = ProactivePrefs(enabled=True, auto_send=True, allowed_channels=frozenset({"hud"}))
    port = FakePort(ok=False)
    assert rt.deliver("d", prefs=prefs, now=DAY, port=port).status == "unknown"
    assert rt.deliver("d", prefs=prefs, now=DAY, port=port).status == "already_attempted"
    assert len(port.calls) == 1


def test_channel_identity_pairing():
    registry = OwnerChannelRegistry(owner_id="u1", bindings={("qq", "100"): "u1"})
    incoming = Incoming("qq", "100", "private", "m1", "hello", True)
    assert registry.accept(incoming).owner_id == "u1"
    assert registry.accept(Incoming("qq", "100", "private", "m1", "hi", False)) is None
    assert registry.accept(Incoming("qq", "101", "private", "m2", "hi", True)) is None

def test_external_direct_message_needs_individual_review():
    rt = make_runtime()
    rt.outbox.propose(ProactiveDraft("qq-1", "hi", "reason", ("event-3",), channel="direct_message"))
    prefs = ProactivePrefs(enabled=True, auto_send=True,
                           allowed_channels=frozenset({"direct_message"}))
    port = FakePort()
    assert rt.deliver("qq-1", prefs=prefs, now=DAY, port=port).status == "needs_approval"
    assert port.calls == []
