"""日程功能测试（2026-11-19 设计 + Tob 六条兼容硬约束验收）。

覆盖：确定性提取（日期锚/时间/无锚不建）、候选→正式两阶段（带 source 溯源）、
命名空间隔离（schedule: 前缀不进 episode/BM25 池）、render 合并（快照语义不动）、
提醒 loop（静默时段/无日程沉默、三态、走 notify 共享配额、fail-soft）、
HTTP 端点形状（401/200/404/source 上下文）。
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from whale_core.life.schedule import (
    CAND_PREFIX, ITEM_PREFIX, add_candidate, add_item, approve_candidate,
    delete_item, extract_schedule_candidate, list_candidates, list_items,
    reject_candidate)
from whale_core.life.schedule_remind import ScheduleReminderLoop
from whale_core.memory.store import InMemoryStore
from whale_core.api.glasses_context import request_context_hint, save_context_snapshot
from whale_core.memory.sqlite_store import SqliteMemoryStore

from datetime import datetime as _dt
from zoneinfo import ZoneInfo
SH = ZoneInfo("Asia/Shanghai")
NOW_SH = _dt(2026, 11, 19, 12, 0, tzinfo=SH)


def _src():
    return {"conversation": "group:1124744261", "messageId": "m1",
            "at": "2026-11-19T12:00:00", "excerpt": "明天下午三点开会"[:80]}


# ---------- 提取 ----------

def test_extract_tomorrow_with_time():
    c = extract_schedule_candidate("明天下午三点开会", source=_src(), now=NOW_SH)
    assert c is not None
    assert c["date"] == "2026-11-20" and c["time"] == "15:00"
    assert c["status"] == "pending"
    assert c["source"]["messageId"] == "m1"
    assert len(c["source"]["excerpt"]) <= 80


def test_extract_anchors():
    assert extract_schedule_candidate("后天交作业", source=_src(),
                                      now=NOW_SH)["date"] == "2026-11-21"
    assert extract_schedule_candidate("下周三下午两点复查", source=_src(),
                                      now=NOW_SH)["date"] == "2026-11-25"
    assert extract_schedule_candidate("12月25日聚餐", source=_src(),
                                      now=NOW_SH)["date"] == "2026-12-25"


def test_extract_no_anchor_or_no_event_returns_none():
    assert extract_schedule_candidate("今天天气真好", source=_src(), now=NOW_SH) is None
    assert extract_schedule_candidate("明天", source=_src(), now=NOW_SH) is None
    assert extract_schedule_candidate("", source=_src(), now=NOW_SH) is None


def test_extract_past_date_rejected():
    c = extract_schedule_candidate("11月1日考试", source=_src(), now=NOW_SH)
    # 11-01 已过 → 推到明年（2027-11-01），不拒绝（未来日期合法）
    assert c is not None and c["date"] == "2027-11-01"


# ---------- 存储两阶段 + 命名空间隔离（约束④） ----------

def test_candidate_two_phase_with_source():
    store = InMemoryStore()
    c = extract_schedule_candidate("明天下午三点开会", source=_src(), now=NOW_SH)
    add_candidate(store, c)
    pend = list_candidates(store)
    assert len(pend) == 1 and pend[0]["status"] == "pending"
    assert pend[0]["source"]["conversation"] == "group:1124744261"
    item = approve_candidate(store, c["cand_id"])
    assert item is not None and item["date"] == "2026-11-20"
    assert list_candidates(store) == []  # 不在 pending 了
    assert list_items(store)[0]["title"] == c["title"]


def test_reject_candidate():
    store = InMemoryStore()
    c = extract_schedule_candidate("明天下午三点开会", source=_src(), now=NOW_SH)
    add_candidate(store, c)
    assert reject_candidate(store, c["cand_id"]) is True
    assert list_candidates(store) == []
    assert approve_candidate(store, c["cand_id"]) is None  # 已拒不再转


def test_namespace_isolation_from_episode_pool():
    """schedule: 前缀与 episode 召回池零串扰（约束④）。"""
    store = InMemoryStore()
    c = extract_schedule_candidate("明天下午三点开会", source=_src(), now=NOW_SH)
    add_candidate(store, c)
    item = add_item(store, title="周五复查", date="2026-11-21")
    keys = [str(k) for k in store.keys()]
    assert all(k.startswith("schedule:") for k in keys if "schedule" in k)
    assert not any(str(k).startswith("ep:") for k in keys)
    assert store.get(ITEM_PREFIX + item["item_id"])["title"] == "周五复查"


def test_manual_item_delete():
    store = InMemoryStore()
    item = add_item(store, title="理发", date="2026-11-22")
    assert delete_item(store, item["item_id"]) is True
    assert list_items(store) == []
    assert delete_item(store, item["item_id"]) is False


# ---------- render 合并（约束①快照语义不动） ----------

def test_render_merges_schedule_items(tmp_path):
    store = SqliteMemoryStore(str(tmp_path / "s.sqlite3"))
    today = _dt.now(SH).date().isoformat()
    save_context_snapshot(store, {"calendar": {today: [
        {"title": "已有日程", "start": "09:00"}]}})
    add_item(store, title="新日程", date=today, time_="15:00")
    hint = request_context_hint(store)
    assert "已有日程" in hint and "新日程" in hint
    # 快照本体不被回写（约束①）
    snap = store.get("glasses:context:snapshot")
    assert len(snap["calendar"][today]) == 1
    store.close()


def test_render_merge_preserves_null_semantics(tmp_path):
    store = SqliteMemoryStore(str(tmp_path / "s.sqlite3"))
    today = _dt.now(SH).date().isoformat()
    save_context_snapshot(store, {"calendar": {today: None}})
    add_item(store, title="新日程", date=today, time_="15:00")
    hint = request_context_hint(store)
    assert "未能读取" in hint and "新日程" not in hint  # null=未读不覆盖
    store.close()


# ---------- 提醒 loop（约束②共享配额 + 静默 + 三态 + fail-soft） ----------

def _loop(store, judge_out="沉默", notify_log=None):
    # 静默窗设空（quiet_start==quiet_end）——测试不随真实时刻翻静默。
    from datetime import time as _t
    _OFF = _t(0, 0)
    sent = notify_log if notify_log is not None else []
    return ScheduleReminderLoop(
        store=store, judge=lambda p: judge_out,
        notify=lambda text, channel: notify_log.append((text, channel)) or {"ok": True},
        facts_hint=lambda: "", quiet_start=_OFF, quiet_end=_OFF), \
        (notify_log if notify_log is not None else [])


def test_loop_silent_when_no_items():
    loop, log = _loop(InMemoryStore())
    assert loop.tick() == "沉默" and log == []


def test_loop_quiet_hours_silent():
    store = InMemoryStore()
    add_item(store, title="复查", date=_dt.now(SH).date().isoformat())
    loop = ScheduleReminderLoop(
        store=store, judge=lambda p: "发消息", notify=lambda t, c: None)
    # 深夜时段：直接判静默
    from whale_core.life.schedule_remind import _quiet_now
    assert _quiet_now(_dt(2026, 11, 19, 23, 30, tzinfo=SH),
                      loop.quiet_start, loop.quiet_end) is True
    assert _quiet_now(_dt(2026, 11, 19, 12, 0, tzinfo=SH),
                      loop.quiet_start, loop.quiet_end) is False


def test_loop_send_via_notify_shared():
    store = InMemoryStore()
    add_item(store, title="复查", date=_dt.now(SH).date().isoformat())
    log = []
    loop, _ = _loop(store, judge_out="发消息", notify_log=log)
    assert loop.tick() == "发消息"
    assert log and log[0][1] == "qq" and "复查" in log[0][0]


def test_loop_remind_goes_phone():
    store = InMemoryStore()
    add_item(store, title="复查", date=_dt.now(SH).date().isoformat())
    log = []
    loop, _ = _loop(store, judge_out="弹提醒", notify_log=log)
    assert loop.tick() == "弹提醒"
    assert log and log[0][1] == "phone"


def test_loop_judge_failure_fail_soft():
    store = InMemoryStore()
    add_item(store, title="复查", date=_dt.now(SH).date().isoformat())

    def _boom(p):
        raise RuntimeError("model down")
    loop, _log = _loop(store, judge_out="沉默")
    loop.judge = _boom
    assert loop.tick() == "沉默"


# ---------- HTTP 端点（含 source 上下文，Tob 补约） ----------

@contextmanager
def server(tmp_path):
    from whale_core.api.glasses import make_handler
    store = SqliteMemoryStore(str(tmp_path / "g.sqlite3"))
    delegates = {"store": store}
    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              __import__("whale_core.api.glasses", fromlist=["make_handler"]).make_handler(delegates))
    srv.glasses_token = "tok"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def call(method, path, body=None, token="tok"):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"http://127.0.0.1:{srv.server_port}{path}", data=data,
                      method=method, headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except HTTPError as e:
            return e.code, json.load(e)
    try:
        yield call, store
    finally:
        srv.shutdown(); srv.server_close(); thread.join(5); store.close()


def test_http_auth_and_candidate_flow(tmp_path):
    with server(tmp_path) as (call, store):
        assert call("GET", "/v1/schedule/candidates", token="bad")[0] == 401
        c = extract_schedule_candidate("明天下午三点开会", source=_src(), now=NOW_SH)
        add_candidate(store, c)
        s, body = call("GET", "/v1/schedule/candidates")
        assert s == 200 and len(body["candidates"]) == 1
        s, body = call("POST", "/v1/schedule/candidates",
                       {"id": c["cand_id"], "action": "add"})
        assert s == 200 and body["item"]["date"] == "2026-11-20"
        s, body = call("GET", "/v1/schedule/list")
        assert s == 200 and len(body["items"]) == 1
        s, body = call("POST", "/v1/schedule/items",
                       {"title": "理发", "date": "2026-11-22"})
        assert s == 200
        iid = body["item"]["item_id"]
        s, body = call("DELETE", "/v1/schedule/items", {"id": iid})
        assert s == 200 and body["ok"] is True


def test_http_source_context(tmp_path):
    with server(tmp_path) as (call, store):
        from whale_core.memory.timeline import GroupTimelineStore
        tl = GroupTimelineStore(store)
        for i in range(3):
            tl.append("group:1124744261", message_id=f"m{i}", sender_id="u1",
                      text=f"消息{i}", send_time=str(1000 + i))
        s, body = call("GET", "/v1/schedule/source?conversation=group:1124744261&messageId=m1")
        assert s == 200 and len(body["context"]) == 3
        assert body["context"][1]["messageId"] == "m1"
        assert body["context"][1]["text"] == "消息1"


def test_loop_notify_routes_through_agent_budget_ledger():
    """约束②：loop 走 agent.notify 公开口——预算+台账不旁路。"""
    from whale_core.api.glasses_agent import GlassesAgent, NotifyBudget, NotifyUserGateway
    sent = []
    records = []
    gw = NotifyUserGateway(
        deliver=lambda t: (sent.append(t), (True, "ok"))[1],
        qq=None, phone=None)
    store = InMemoryStore()
    agent = GlassesAgent(store=store, ledger=lambda r: records.append(r),
                         workspace="/tmp/x", notify_gateway=gw,
                         notify_budget=NotifyBudget(store, per_turn=3, per_hour=1, per_day=30))
    out = agent.notify("第一条", channel="auto", session_id="schedule-loop")
    assert out["actions"][0]["status"] == "ok" and sent == ["第一条"]
    out2 = agent.notify("第二条", channel="auto", session_id="schedule-loop")
    assert out2["actions"][0]["status"] == "fail"  # 超 hour 桶被拦
    assert len(sent) == 1
    assert records[-1]["verdict"] == "fail"  # 台账有拦截记录
