"""G3 三条全天助手场景的自动化模拟器验收（离线，不触网）。

- 场景1 上课与天气：day_context.ContextReminder + 可信日程/天气替身；
- 场景2 慢任务与结果交付：QQ 入站带图 → 两段式 ack → 完成补发 → 送达回执；
- 场景3 连续电话/语音：会话绑定 scope、转写只存文本、挂断后资源释放。

真机验收缺口记录在 docs/g3-scenarios.md（owner 确认本轮只到模拟器层）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from whale_core.life.day_context import (
    ContextReminder,
    DayPlanStore,
    WeatherSnapshot,
)
from whale_core.life.initiative import ProactiveOutbox, ProactivePrefs
from whale_core.memory.store import InMemoryStore

NOW = datetime(2026, 10, 31, 13, 0, tzinfo=timezone(timedelta(hours=8)))  # 午后


class FakeWeather:
    def __init__(self, rain: bool, condition: str = "小雨"):
        self.rain = rain
        self.condition = condition
        self.calls: list[tuple[str, datetime]] = []

    def snapshot(self, location: str, when: datetime) -> WeatherSnapshot:
        self.calls.append((location, when))
        return WeatherSnapshot(condition=self.condition, rain=self.rain,
                               source="https://weather.example.com/shanghai",
                               at=when)


class BrokenWeather:
    def snapshot(self, location, when):
        raise TimeoutError("upstream down")


def _prefs(**kw):
    base = dict(enabled=True, allowed_channels=frozenset({"direct_message"}),
                auto_send=True, daily_limit=10)
    base.update(kw)
    return ProactivePrefs(**base)


def _reminder(store, prefs, weather, now=NOW):
    outbox = ProactiveOutbox(store, now_fn=lambda: now)
    return ContextReminder(store, outbox, prefs, weather, now_fn=lambda: now)


def _add_class(store, now=NOW, title="操作系统课", hours_ahead=2):
    plan = DayPlanStore(store)
    return plan.add_event(
        title=title, start=now + timedelta(hours=hours_ahead),
        end=now + timedelta(hours=hours_ahead + 2), location="教学楼 A201",
        source="calendar-import:owner")


# ---------- 场景 1：上课与天气 ----------


def test_class_and_rain_produces_sourced_reminder():
    store = InMemoryStore()
    _add_class(store)
    weather = FakeWeather(rain=True)
    reminder = _reminder(store, _prefs(), weather)
    result = reminder.evaluate_departure("我过会去上课", conversation_id="c1")
    assert result["triggered"] and result["reason"] == "rain-before-event"
    draft = reminder.outbox.get(result["draft_id"])
    assert draft is not None and "带伞" in draft.text and "操作系统课" in draft.text
    # 可点击来源：事件来源 + 天气来源都在 evidence
    assert "calendar-import:owner" in draft.evidence
    assert "https://weather.example.com/shanghai" in draft.evidence
    assert weather.calls[0][0] == "教学楼 A201"


def test_class_and_clear_weather_no_reminder():
    store = InMemoryStore()
    _add_class(store)
    reminder = _reminder(store, _prefs(), FakeWeather(rain=False, condition="晴"))
    result = reminder.evaluate_departure("过会去上课")
    assert not result["triggered"] and result["reason"] == "weather-clear"
    assert reminder.outbox.get("ctx:umbrella:x") is None


def test_quiet_hours_deferred_by_policy():
    store = InMemoryStore()
    night = NOW.replace(hour=23, minute=30)
    _add_class(store, now=night)
    prefs = _prefs(quiet_start=__import__("datetime").time(22),
                   quiet_end=__import__("datetime").time(7))
    reminder = _reminder(store, prefs, FakeWeather(rain=True), now=night)
    result = reminder.evaluate_departure("过会去上课")
    assert result["triggered"] and result["verdict"] == "deferred"  # 静默时段不派发
    ledger = reminder.outbox.ledger(result["draft_id"])
    assert ledger  # 草稿在案、可审批，但没有自动发送


def test_disabled_prefs_zero_output():
    store = InMemoryStore()
    _add_class(store)
    reminder = _reminder(store, ProactivePrefs(enabled=False), FakeWeather(rain=True))
    result = reminder.evaluate_departure("过会去上课")
    assert result["reason"] == "proactivity-disabled"
    assert [k for k in store.keys() if k.startswith("life:draft:")] == []


def test_no_class_no_reminder_and_untrusted_source_rejected():
    store = InMemoryStore()
    reminder = _reminder(store, _prefs(), FakeWeather(rain=True))
    assert reminder.evaluate_departure("过会去上课")["reason"] == "no-upcoming-event"
    # 无可信来源的日程拒绝入库（陌生事件须经来源检查）
    with pytest.raises(ValueError):
        DayPlanStore(store).add_event(
            title="神秘会议", start=NOW, end=NOW + timedelta(hours=1), source="")


def test_weather_unavailable_is_honest_no_guess():
    store = InMemoryStore()
    _add_class(store)
    reminder = _reminder(store, _prefs(), BrokenWeather())
    result = reminder.evaluate_departure("过会去上课")
    assert not result["triggered"]
    assert result["reason"].startswith("weather-unavailable:")


def test_same_event_deduped_24h():
    store = InMemoryStore()
    _add_class(store)
    reminder = _reminder(store, _prefs(), FakeWeather(rain=True))
    first = reminder.evaluate_departure("过会去上课")
    second = reminder.evaluate_departure("马上要去上课了")
    assert first["triggered"] and first["draft_id"]
    assert second["reason"] == "duplicate-or-deduped"


# ---------- 场景 2：慢任务与结果交付（QQ 带图 → ack → 补发 → 送达） ----------


def _png_bytes():
    import struct

    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", 8, 6) + b"\x08\x06\x00\x00\x00" + b"\x00" * 16)


class _StubBrain:
    def reply(self, text, *, persona="", memories=(), history=(), **kw):
        # 替身语义：工具结果含 URL 时把它带进答复（验证结果链路），否则固定短句
        if "cdn.example.com" in text:
            return "画好了：https://cdn.example.com/a1.png"
        return "好嘞"


def _slow_intent():
    class _I:
        def plan(self, text, *, context=""):
            return json.dumps({"goal": "画图", "kind": "task",
                               "capabilities": ["media.image.generate"],
                               "timeout_s": 300})
    return _I()


def _qq_workflow(tmp_path, tool_handler):
    from whale_core.core.companion import WhaleCompanion
    from whale_core.core.media_input import ImageInputValidator
    from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
    from whale_core.life.channels import OwnerEvent
    from whale_core.life.qq_workflow import QqInboundWorkflow

    store = InMemoryStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="gen", capability="media.image.generate",
                            description="生图", handler=tool_handler))
    companion = WhaleCompanion(store, _StubBrain())
    from whale_core.core.runtime import ConversationOrchestrator as CO
    orc = CO(store, companion, intent_model=_slow_intent(), tools=tools)
    prefs = ProactivePrefs(enabled=True,
                           allowed_channels=frozenset({"direct_message"}))
    sent: list = []

    def completion_sender(event, reply, audio_ids=()):
        sent.append({"event": event, "reply": reply, "audio_ids": audio_ids})

    workflow = QqInboundWorkflow(
        store, _StubBrain(), prefs=prefs, orchestrator=orc,
        completion_sender=completion_sender,
        media_validator=ImageInputValidator(allowed_roots=(str(tmp_path),)))
    return store, orc, workflow, sent


def test_qq_image_redraw_full_delivery_loop(tmp_path):
    """QQ 发图要求再创作 → ack 草稿 → 后台完成 → 补发含图链接 → 送达回执。"""
    from whale_core.life.channels import OwnerEvent

    img = tmp_path / "ref.png"
    img.write_bytes(_png_bytes())

    def gen(slots):
        return json.dumps({"task_id": "t1", "status": "succeeded",
                           "images": ["artifact://a1"], "urls": ["https://cdn.example.com/a1.png"]})

    store, orc, workflow, sent = _qq_workflow(tmp_path, gen)
    event = OwnerEvent(owner_id="owner-1", channel="qq", conversation="c1",
                       message_id="m1", text="参考他画一个",
                       attachments=({"kind": "pic", "file_path": str(img), "url": ""},))
    outcome = workflow.handle_owner_event(event)
    assert outcome["reply"]  # ack 草稿产生
    # 后台完成 → 补发
    deadline = datetime.now().timestamp() + 5
    while not sent and datetime.now().timestamp() < deadline:
        threading.Event().wait(0.02)
    assert len(sent) == 1
    assert "cdn.example.com/a1.png" in sent[0]["reply"]  # 补发含真实图片链接
    # 送达回执：台账 delivered，且跨渠道进度可见
    tasks = orc.tasks_for_owner("owner-1")
    assert len(tasks) == 1 and tasks[0]["phase"] == "delivered"
    assert tasks[0]["delivery"] == "delivered"


def test_qq_slow_task_failure_is_honest(tmp_path):
    """工具失败：用户收到诚实失败补发（不含成功假象），phase=failed 可见。"""
    from whale_core.life.channels import OwnerEvent

    def broken(slots):
        raise RuntimeError("provider exploded")

    store, orc, workflow, sent = _qq_workflow(tmp_path, broken)
    event = OwnerEvent(owner_id="owner-1", channel="qq", conversation="c1",
                       message_id="m2", text="画一只鲸鱼", attachments=())
    workflow.handle_owner_event(event)
    deadline = datetime.now().timestamp() + 5
    while not sent and datetime.now().timestamp() < deadline:
        threading.Event().wait(0.02)
    assert len(sent) == 1
    reply = sent[0]["reply"]
    assert "成功" not in reply.replace("没有成功", "").replace("别假装成功", "")
    tasks = orc.tasks_for_owner("owner-1")
    assert tasks and tasks[0]["phase"] == "failed"


# ---------- 场景 3：连续电话/语音 ----------


def test_voice_session_scope_progress_and_hangup_release():
    from whale_core.core.companion import RealtimeTranscriptRecorder
    from whale_core.life.identity import OwnerRegistry
    from whale_core.memory.store import MemoryKind

    store = InMemoryStore()
    reg = OwnerRegistry(store)
    reg.bind("owner-1", "qq", "3563425362")
    reg.bind_session("call-1", "owner-1", "voice", scopes=("task_progress",))
    binding = reg.resolve_session("call-1")
    assert binding.allows("task_progress")
    assert not binding.allows("private_context")  # 未授权范围不可得

    # 通话中转写只落文本情节，不落音频字节
    recorder = RealtimeTranscriptRecorder(store)
    assert recorder.offer("call-1", "user", "我那张图怎么样了")
    assert recorder.offer("call-1", "assistant", "已经完成了")
    recorder.close()
    episodes = store.list_by_kind(MemoryKind.EPISODE)
    assert any("我那张图怎么样了" in str(r.content.get("summary")) for r in episodes)
    # 不存在任何音频形态数据键
    for key in store.keys():
        value = store.get(key)
        assert not isinstance(value, (bytes, bytearray))
        assert "audio" not in key.lower() or isinstance(value, str)

    # 挂断：会话撤销 → 绑定失效 → 不再可查
    assert reg.revoke_session("call-1") is True
    assert reg.resolve_session("call-1") is None
