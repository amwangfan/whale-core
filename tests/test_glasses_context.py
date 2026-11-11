"""眼镜实时事实联动测试（api/glasses_context.py + build_delegates 接线）。

全部离线：纯函数直接测；delegates 用 FakeBrain + 临时目录真实 sqlite。
覆盖：legacy 前缀+[用户] 分离、普通/部分文本不误剥、structured calendar、
快照 replace/[]/null、跨日期不过夜、request 覆盖 snapshot（calendar 按日期合并，
仅替换同日期；空 calendar 不清快照；快照不被写改）、ContextVar 并发隔离、
companion 用户话/历史不含 facts、quick 不写历史。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from whale_core.api.glasses_context import (
    REQUEST_FACTS, SHANGHAI, SNAPSHOT_KEY, render_context,
    request_context_hint, save_context_snapshot, split_chat_context)
from whale_core.api.glasses import build_delegates
from whale_core.memory.history import load_history
from whale_core.memory.sqlite_store import SqliteMemoryStore

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=SHANGHAI)
TODAY = "2026-11-11"
TOMORROW = "2026-11-12"


def _server_today() -> str:
    """request_context_hint 以服务端真实时间为准：测试日期跟随服务端时钟。"""
    return datetime.now(SHANGHAI).date().isoformat()


def _server_tomorrow() -> str:
    return (datetime.now(SHANGHAI) + timedelta(days=1)).date().isoformat()

LEGACY_BLOCK = (
    "[现在] 2026-11-11 周三 10:00\n"
    "[今日日程] 14:00 组会、19:00 健身\n"
    "[明日日程] 09:00 考试\n"
    "[天气] 上海 晴 18°C\n"
)


# ---------- 纯函数：split_chat_context ----------

def test_split_legacy_block_yields_clean_user_text():
    user, facts = split_chat_context(LEGACY_BLOCK + "[用户] 明天有什么安排？")
    assert user == "明天有什么安排？"
    assert "[用户]" not in user and "[现在]" not in user
    assert facts["今日日程"] == "14:00 组会、19:00 健身"
    assert facts["明日日程"] == "09:00 考试"
    assert facts["天气"] == "上海 晴 18°C"
    assert facts["现在"].startswith("2026-11-11")


def test_split_plain_text_untouched():
    user, facts = split_chat_context("今天天气怎么样")
    assert user == "今天天气怎么样"
    assert facts == {}


def test_split_partial_block_without_user_marker_not_stripped():
    """有前缀行但没有 [用户] 分隔：整条原样返回，不静默吞掉内容。"""
    text = "[现在] 2026-11-11 周三 10:00\n[今日日程] 14:00 组会\n明天有什么安排？"
    user, facts = split_chat_context(text)
    assert user == text.strip()
    assert facts == {}


def test_split_unknown_label_not_stripped():
    text = "[位置] 上海\n[用户] 你好"
    user, facts = split_chat_context(text)
    assert user == text.strip()
    assert facts == {}


def test_split_user_marker_without_facts_is_ordinary_text():
    """裸 [用户] 开头（无已识别事实行）按普通文本处理，不剥。"""
    text = "[用户] 你好"
    user, facts = split_chat_context(text)
    assert user == text
    assert facts == {}


def test_split_structured_context_merges():
    user, facts = split_chat_context(
        "你好", context={"weather": "上海 晴 18°C"})
    assert user == "你好"
    assert facts == {"weather": "上海 晴 18°C"}


# ---------- 纯函数：save_context_snapshot ----------

@pytest.fixture()
def store(tmp_path):
    store = SqliteMemoryStore(str(tmp_path / "ctx.sqlite3"))
    yield store
    store.close()


def test_save_snapshot_normalizes_and_replaces(store):
    snap = save_context_snapshot(store, {"calendar": {TODAY: [
        {"id": "e1", "title": "组会", "start": "14:00", "end": "15:00",
         "allDay": False, "extra": "应被丢弃"}]}})
    event = snap["calendar"][TODAY][0]
    assert event == {"id": "e1", "title": "组会", "start": "14:00",
                     "end": "15:00", "allDay": False}
    assert "extra" not in event
    # 整体替换：第二次同步覆盖第一次，不留旧日期
    save_context_snapshot(store, {"calendar": {TOMORROW: [
        {"title": "考试", "start": "09:00"}]}})
    stored = store.get(SNAPSHOT_KEY)
    assert list(stored["calendar"].keys()) == [TOMORROW]
    # weather 不随日历快照持久化（保持 request-only）
    assert "weather" not in stored


def test_save_snapshot_empty_list_means_no_events(store):
    snap = save_context_snapshot(store, {"calendar": {TODAY: []}})
    assert snap["calendar"][TODAY] == []


def test_save_snapshot_null_means_unreadable(store):
    snap = save_context_snapshot(store, {"calendar": {TODAY: None}})
    assert snap["calendar"][TODAY] is None


def test_save_snapshot_validation(store):
    with pytest.raises(ValueError):
        save_context_snapshot(store, {})  # calendar 缺失
    with pytest.raises(ValueError):
        save_context_snapshot(store, {"calendar": {"11-11": []}})  # 非法日期
    with pytest.raises(ValueError):
        save_context_snapshot(store, {"calendar": {TODAY: "组会"}})  # 非数组
    with pytest.raises(ValueError):
        save_context_snapshot(store, {"calendar": {TODAY: [{"start": "14:00"}]}})  # 缺 title


# ---------- 纯函数：render_context ----------

def test_render_calendar_today_tomorrow():
    facts = {"calendar": {
        TODAY: [{"title": "组会", "start": "14:00"}],
        TOMORROW: [],
        "2026-11-20": [{"title": "远未来", "start": "10:00"}],
    }}
    out = render_context(facts, now=NOW)
    assert f"[今日日程 {TODAY}] 14:00 组会" in out
    assert f"[明日日程 {TOMORROW}] （无）" in out
    assert "远未来" not in out  # 跨日期不渲染


def test_render_calendar_null_means_unreadable():
    out = render_context({"calendar": {TODAY: None}}, now=NOW)
    assert "未能读取" in out
    assert "（无）" not in out  # 未同步/不可读 ≠ 无日程


def test_render_legacy_anchor_overnight_dropped():
    """legacy 块的日期锚点是昨天：今日/明日日程不过夜滚动。"""
    facts = {"现在": "2026-11-10 周二 23:59", "今日日程": "09:00 旧会",
             "明日日程": "10:00 旧会2", "天气": "上海 晴 18°C"}
    out = render_context(facts, now=NOW)
    assert out == ""


def test_render_legacy_anchor_today_renders():
    facts = {"现在": "2026-11-11 周三 09:00", "今日日程": "14:00 组会"}
    out = render_context(facts, now=NOW)
    assert "[今日日程] 14:00 组会" in out


def test_render_request_only_weather():
    out = render_context({"weather": "上海 晴 18°C"}, now=NOW)
    assert "[天气] 上海 晴 18°C" in out


def test_render_empty_facts_empty():
    assert render_context(None, now=NOW) == ""
    assert render_context({}, now=NOW) == ""


# ---------- request_context_hint：request 覆盖 snapshot ----------

def test_request_facts_override_snapshot(store):
    today = _server_today()
    save_context_snapshot(store, {"calendar": {today: [
        {"title": "同步会", "start": "09:00"}]}})
    token = REQUEST_FACTS.set({"今日日程": "10:00 请求版"})
    try:
        hint = request_context_hint(store)
    finally:
        REQUEST_FACTS.reset(token)
    assert "10:00 请求版" in hint
    assert "同步会" not in hint  # 同轮不给两份互相竞争的日程
    # 请求结束（ContextVar reset）后 snapshot 恢复可见，且未被改写
    hint2 = request_context_hint(store)
    assert "同步会" in hint2
    assert store.get(SNAPSHOT_KEY)["calendar"][today][0]["title"] == "同步会"


def test_snapshot_only_renders_without_request_facts(store):
    save_context_snapshot(store, {"calendar": {_server_today(): [
        {"title": "同步会", "start": "09:00"}]}})
    hint = request_context_hint(store)
    assert "同步会" in hint


# ---------- request_context_hint：calendar 按日期合并（PR#83 评论） ----------

@pytest.mark.parametrize("override,expected", [
    ([{"title": "请求会", "start": "20:00"}], "20:00 请求会"),  # 普通事件
    ([], "（无）"),                                             # 空数组覆盖该日期
    (None, "未能读取"),                                         # null 覆盖该日期
])
def test_request_calendar_tomorrow_only_keeps_today(store, override, expected):
    """请求只带明天的 structured calendar：仅替换明天，快照的今天保留。"""
    today, tomorrow = _server_today(), _server_tomorrow()
    save_context_snapshot(store, {"calendar": {
        today: [{"title": "同步会", "start": "09:00"}],
        tomorrow: [{"title": "旧明日会", "start": "08:00"}],
    }})
    token = REQUEST_FACTS.set({"calendar": {tomorrow: override}})
    try:
        hint = request_context_hint(store)
    finally:
        REQUEST_FACTS.reset(token)
    assert f"[今日日程 {today}] 09:00 同步会" in hint  # 今天不被整窗覆盖掉
    assert expected in hint                            # 明天按请求覆盖
    assert "旧明日会" not in hint                      # 同日期被替换
    # 原快照未被写改：仍是同步时的整窗内容（整窗替换只属于 save_context_snapshot）
    stored = store.get(SNAPSHOT_KEY)["calendar"]
    assert stored[today][0]["title"] == "同步会"
    assert stored[tomorrow][0]["title"] == "旧明日会"


@pytest.mark.parametrize("calendar", [{}, None])
def test_empty_request_calendar_does_not_clear_snapshot(store, calendar):
    """空 context.calendar 不是清快照：显式清快照只走 POST sync。"""
    today = _server_today()
    save_context_snapshot(store, {"calendar": {today: [
        {"title": "同步会", "start": "09:00"}]}})
    token = REQUEST_FACTS.set({"calendar": calendar})
    try:
        hint = request_context_hint(store)
    finally:
        REQUEST_FACTS.reset(token)
    assert "09:00 同步会" in hint
    assert store.get(SNAPSHOT_KEY)["calendar"][today][0]["title"] == "同步会"


def test_request_weather_leaves_snapshot_calendar(store):
    """请求只带天气等其它 context：calendar 窗口完整来自快照，不受影响。"""
    today, tomorrow = _server_today(), _server_tomorrow()
    save_context_snapshot(store, {"calendar": {
        today: [{"title": "同步会", "start": "09:00"}],
        tomorrow: [{"title": "同步明日会", "start": "08:00"}],
    }})
    token = REQUEST_FACTS.set({"weather": "上海 晴 18°C"})
    try:
        hint = request_context_hint(store)
    finally:
        REQUEST_FACTS.reset(token)
    assert "[天气] 上海 晴 18°C" in hint
    assert f"[今日日程 {today}] 09:00 同步会" in hint
    assert f"[明日日程 {tomorrow}] 08:00 同步明日会" in hint


def test_request_calendar_merge_does_not_mutate_snapshot(store):
    """按日期合并只作用于当轮渲染：快照内容前后完全一致，不被写改。"""
    today, tomorrow = _server_today(), _server_tomorrow()
    save_context_snapshot(store, {"calendar": {
        today: [{"title": "同步会", "start": "09:00"}],
        tomorrow: [{"title": "旧明日会", "start": "08:00"}],
    }})
    before = store.get(SNAPSHOT_KEY)
    token = REQUEST_FACTS.set({
        "calendar": {tomorrow: [{"title": "请求会", "start": "20:00"}]}})
    try:
        hint = request_context_hint(store)
    finally:
        REQUEST_FACTS.reset(token)
    assert "请求会" in hint
    assert store.get(SNAPSHOT_KEY) == before
    # 请求结束后的下一轮仍看到原快照
    assert "旧明日会" in request_context_hint(store)


# ---------- ContextVar 并发隔离 ----------

def test_request_facts_contextvar_thread_isolation():
    token = REQUEST_FACTS.set({"今日日程": "主线请求"})
    seen = {}
    done = threading.Event()
    try:
        def worker():
            seen["initial"] = REQUEST_FACTS.get()  # 新线程不继承 → None
            REQUEST_FACTS.set({"今日日程": "并发请求"})
            done.wait(5)
            seen["own"] = (REQUEST_FACTS.get() or {}).get("今日日程")

        t = threading.Thread(target=worker)
        t.start()
        while "initial" not in seen:
            done.wait(0.01)
        assert seen["initial"] is None
        done.set()
        t.join(5)
        assert seen["own"] == "并发请求"
        # 主上下文不被线程写入污染
        assert REQUEST_FACTS.get()["今日日程"] == "主线请求"
    finally:
        REQUEST_FACTS.reset(token)
    assert REQUEST_FACTS.get() is None


# ---------- build_delegates 接线（FakeBrain，无 HTTP） ----------

class _FakeBrain:
    def __init__(self):
        self.calls = []

    def reply(self, text, *, persona="", memories=(), history=(), images=(),
              location=None):
        self.calls.append({"text": text, "persona": persona,
                           "history": list(history), "location": location})
        return f"[stub] {text}"


class _FakePort:
    def send_c2c(self, uid, text):
        return True


@pytest.fixture()
def delegates(tmp_path):
    env = {
        "WHALE_GLASSES_TOKEN": "phone-token",
        "WHALE_GLASSES_STORE": str(tmp_path / "g.sqlite3"),
        "OWNER_UID": "u_owner",
        "OWNER_QQ": "3563425362",
    }
    companion_brain, quick_brain = _FakeBrain(), _FakeBrain()
    d = build_delegates(
        env, brain_factory=lambda: companion_brain,
        quick_brain_factory=lambda: quick_brain,
        send_port_factory=lambda: _FakePort())
    d["_companion_brain"] = companion_brain
    d["_quick_brain"] = quick_brain
    return d


def _all_stored_text(store) -> str:
    import json
    return "\n".join(
        json.dumps(store.get(k), ensure_ascii=False) for k in store.keys())


@pytest.fixture
def frozen_context_clock(monkeypatch):
    """仅冻结 whale_core.api.glasses_context 模块的时钟到 NOW（2026-11-11）。

    LEGACY_BLOCK 是固定日期（11-11）的事实块，而 render_context 以真实
    datetime.now 过滤「跨日期不过夜」——真实时钟走到 11-12 后过滤行为
    本身正确，但这两例因此丢 facts。冻时只作用于本模块的 datetime 引用
    （strptime 等 classmethod 经子类继承不受影响），不冻 delegates/全局。
    """
    import whale_core.api.glasses_context as gc

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return NOW.replace(tzinfo=None)
            return NOW.astimezone(tz)

    monkeypatch.setattr(gc, "datetime", FixedDateTime)
    return FixedDateTime


def test_companion_user_text_and_history_exclude_facts(delegates,
                                                       frozen_context_clock):
    reply, _ = delegates["companion_chat"](
        LEGACY_BLOCK + "[用户] 今天有什么安排？",
        conversation_id="glasses:ctx1")
    assert reply
    call = delegates["_companion_brain"].calls[0]
    # 模型收到的用户话是净文本
    assert call["text"] == "今天有什么安排？"
    # facts 只作当轮背景进 persona，不进用户话
    assert "当轮手机事实资料" in call["persona"]
    assert "14:00 组会" in call["persona"]
    # 会话历史只落净用户话
    history = load_history(delegates["store"], "glasses:ctx1")
    assert history[0]["content"] == "今天有什么安排？"
    # facts 不落任何存储键值（不污染记忆/情节）
    assert "14:00 组会" not in _all_stored_text(delegates["store"])
    assert "[现在]" not in _all_stored_text(delegates["store"])


def test_companion_plain_text_unchanged(delegates):
    reply, _ = delegates["companion_chat"](
        "我叫小凡", conversation_id="glasses:ctx2")
    assert reply
    call = delegates["_companion_brain"].calls[0]
    assert call["text"] == "我叫小凡"
    assert "当轮手机事实资料" not in call["persona"]


def test_companion_uses_synced_snapshot_as_background(delegates):
    delegates["sync_context"]({"calendar": {_server_today(): [
        {"title": "同步会", "start": "09:00"}]}})
    reply, _ = delegates["companion_chat"](
        "今天有啥事？", conversation_id="glasses:ctx3")
    assert reply
    call = delegates["_companion_brain"].calls[0]
    assert call["text"] == "今天有啥事？"
    assert "同步会" in call["persona"]
    history = load_history(delegates["store"], "glasses:ctx3")
    assert history[0]["content"] == "今天有啥事？"


def test_quick_facts_in_persona_and_no_history(delegates,
                                               frozen_context_clock):
    reply, _ = delegates["quick_chat"](LEGACY_BLOCK + "[用户] 你好")
    assert reply
    call = delegates["_quick_brain"].calls[0]
    assert call["text"] == "你好"
    assert "14:00 组会" in call["persona"]
    # quick 不写长期记忆/会话历史
    assert not [k for k in delegates["store"].keys()
                if str(k).startswith("chat:history:")]
    assert "14:00 组会" not in _all_stored_text(delegates["store"])


def test_quick_plain_call_unchanged(delegates):
    reply, _ = delegates["quick_chat"]("你好")
    assert reply
    call = delegates["_quick_brain"].calls[0]
    assert call["text"] == "你好"
    assert "当轮手机事实资料" not in call["persona"]


def test_concurrent_requests_facts_isolation(delegates):
    """两个并发请求各自带不同日程：persona 不互相串 facts。"""
    barrier = threading.Barrier(2)
    errors = []

    def worker(tag, schedule):
        try:
            barrier.wait(5)
            delegates["quick_chat"](f"[今日日程] {schedule}\n[用户] {tag}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("问甲", "08:00 甲的会"))
    t2 = threading.Thread(target=worker, args=("问乙", "22:00 乙的会"))
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    assert not errors
    calls = {c["text"]: c for c in delegates["_quick_brain"].calls}
    assert "08:00 甲的会" in calls["问甲"]["persona"]
    assert "22:00 乙的会" not in calls["问甲"]["persona"]
    assert "22:00 乙的会" in calls["问乙"]["persona"]
    assert "08:00 甲的会" not in calls["问乙"]["persona"]


def test_snapshot_date_normalization_and_all_day_rendering(store):
    snap = save_context_snapshot(store, {"calendar": {"2026-1-2": [
        {"title": "全天活动", "start": "00:00", "allDay": True}]}})
    assert "2026-01-02" in snap["calendar"]
    assert snap["calendar"]["2026-01-02"][0]["allDay"] is True
    out = render_context(snap, now=datetime(2026, 1, 2, 8, tzinfo=SHANGHAI))
    assert "全天 全天活动" in out and "00:00" not in out


def test_stale_legacy_block_does_not_hide_current_synchronized_calendar(store):
    now = datetime.now(SHANGHAI)
    save_context_snapshot(store, {"calendar": {now.date().isoformat(): [
        {"title": "今天有效的日程", "start": "10:00"}]}})
    yesterday = (now - timedelta(days=1)).date().isoformat()
    token = REQUEST_FACTS.set({"现在": yesterday, "今日日程": "昨天过期的日程"})
    try:
        out = request_context_hint(store)
        assert "今天有效的日程" in out
        assert "昨天过期的日程" not in out
    finally:
        REQUEST_FACTS.reset(token)
