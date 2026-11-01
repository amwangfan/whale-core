"""issue #43：任务执行时间线聚合测试（离线）。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.client import HTTPConnection

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.core.runtime.task_timeline import build_task_timeline
from whale_core.life.trace import TraceLedger
from whale_core.memory.store import InMemoryStore

TOKEN = "task-timeline-test-token"


def _record(phase, events=(), **extra):
    record = {
        "task_id": "t1", "goal": "给主人发提醒", "phase": phase,
        "status": "answered" if phase in ("succeeded", "delivered") else phase,
        "capabilities": ["notify"], "receipts": ["tool:notify:ok:n-1"],
        "at": 1762000000.0,
        "events": [{"at": 1762000000.0 + i, "kind": kind}
                   for i, kind in enumerate(events)],
    }
    record.update(extra)
    return record


class TestTimeline:
    def test_full_story(self):
        store = InMemoryStore()
        store.put("task:t1", _record(
            "delivered", events=["pending", "running", "succeeded", "delivered"],
            delivery={"state": "delivered", "attempts": 1, "channel": "qq"}))
        tl = build_task_timeline(store, "t1")
        assert tl["current"]["label"] == "已送达"
        assert [e["text"] for e in tl["timeline"]] == [
            "任务已创建", "开始执行", "执行成功", "结果已送达"]
        assert tl["retry"]["allowed"] is False
        assert tl["receipts"] == ["tool:notify:ok:n-1"]

    def test_failed_retryable(self):
        store = InMemoryStore()
        store.put("task:t1", _record("failed", events=["pending", "running", "failed"]))
        tl = build_task_timeline(store, "t1")
        assert tl["current"]["label"] == "执行失败"
        assert tl["retry"]["allowed"] is True
        assert tl["retry"]["kind"] == "resume"
        assert "幂等" in tl["retry"]["reason"]

    def test_unknown_never_shown_as_success(self):
        store = InMemoryStore()
        store.put("task:t1", _record("unknown"))
        tl = build_task_timeline(store, "t1")
        assert tl["current"]["label"] == "状态未知"
        assert "不能当成成功" in tl["current"]["explanation"] or \
               "不代表失败" in tl["current"]["next_step"]
        assert tl["retry"]["allowed"] is False
        assert tl["retry"]["kind"] == "inspect"

    def test_delivery_failed_redeliver(self):
        store = InMemoryStore()
        store.put("task:t1", _record(
            "delivery_failed",
            delivery={"state": "failed", "attempts": 2,
                      "last_error": "send timeout", "channel": "qq"}))
        tl = build_task_timeline(store, "t1")
        assert tl["retry"]["kind"] == "redeliver"
        assert "不重复执行工具" in tl["retry"]["reason"]
        assert tl["delivery"]["last_error"] == "send timeout"

    def test_pending_approval_next_step(self):
        store = InMemoryStore()
        store.put("task:t1", _record("pending_approval"))
        tl = build_task_timeline(store, "t1")
        assert "批准" in tl["current"]["next_step"]

    def test_trace_linkage(self):
        store = InMemoryStore()
        store.put("task:t1", _record("succeeded"))
        led = TraceLedger(":memory:")
        tid = led.begin("task", task_id="t1")
        led.finish(tid, "replied")
        led.begin("task", task_id="other")  # 不相关
        tl = build_task_timeline(store, "t1", trace_ledger=led)
        assert tl["trace_ids"] == [tid]

    def test_missing_task_none(self):
        assert build_task_timeline(InMemoryStore(), "nope") is None


class TestTimelineApi:
    @contextmanager
    def _server(self):
        store = InMemoryStore()
        store.put("task:t1", _record("failed", events=["pending", "failed"]))
        ctx = ApiContext(store=store)
        server = WhaleApiServer(ctx, port=0, token=TOKEN)
        server.start()
        try:
            yield server
        finally:
            server.stop()

    def test_route(self):
        with self._server() as server:
            conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("GET", "/api/v1/tasks/t1/timeline",
                         headers={"Authorization": f"Bearer {TOKEN}"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            assert resp.status == 200
            assert body["result"]["current"]["label"] == "执行失败"
            conn.close()

    def test_route_404(self):
        with self._server() as server:
            conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
            conn.request("GET", "/api/v1/tasks/nope/timeline",
                         headers={"Authorization": f"Bearer {TOKEN}"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            assert resp.status == 404
