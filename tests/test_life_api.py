"""POST/GET /api/v1/life*：兴趣台账与探索日记的真实回环测试（不触外网）。

覆盖移植自 PR#4 的差异交互：兴趣增删与上限校验、反馈调权/退避、日记读取。
探索执行与草稿审批不在此路由——分别属 /workflows/explore 与 /approvals。
"""

from __future__ import annotations

import json
from http.client import HTTPConnection

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.life.discovery import DiscoveryPlanner
from whale_core.life.exploration import (
    BACKOFF_KEY,
    ExplorationManager,
    InterestTracker,
)
from whale_core.memory.store import InMemoryStore

TOKEN = "test-token-123"


@pytest.fixture()
def api():
    ctx = ApiContext(store=InMemoryStore())
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server
    server.stop()


def _req(server, method, path, *, token=TOKEN, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


def _manager(store):
    return ExplorationManager(store, planner=DiscoveryPlanner(allowed_hosts=frozenset()))


def test_life_requires_auth(api):
    status, body = _req(api, "GET", "/api/v1/life", token="")
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_life_empty_state(api):
    status, body = _req(api, "GET", "/api/v1/life")
    assert status == 200
    assert body["result"] == {"interests": [], "diary": []}


def test_interest_add_list_remove_roundtrip(api):
    status, body = _req(api, "POST", "/api/v1/life/interests", body={"topic": "AR 眼镜"})
    assert status == 200
    assert body["result"] == {"topic": "AR 眼镜", "weight": 1.0}
    status, body = _req(api, "GET", "/api/v1/life")
    assert [i["topic"] for i in body["result"]["interests"]] == ["AR 眼镜"]
    status, body = _req(
        api, "POST", "/api/v1/life/interests", body={"topic": "AR 眼镜", "remove": True}
    )
    assert status == 200 and body["result"] == {"removed": True}
    status, body = _req(api, "GET", "/api/v1/life")
    assert body["result"]["interests"] == []


def test_interest_validation_and_cap(api):
    for bad in ({}, {"topic": ""}, {"topic": "   "}, {"topic": "x" * 101}, {"topic": 42}):
        status, _ = _req(api, "POST", "/api/v1/life/interests", body=bad)
        assert status == 422, bad
    for n in range(20):
        status, _ = _req(api, "POST", "/api/v1/life/interests", body={"topic": f"t{n}"})
        assert status == 200
    status, _ = _req(api, "POST", "/api/v1/life/interests", body={"topic": "one-more"})
    assert status == 422
    # 已达上限时更新既有话题仍然允许（不产生新条目）。
    status, _ = _req(api, "POST", "/api/v1/life/interests", body={"topic": "t0"})
    assert status == 200


def test_feedback_adjusts_weight_and_backoff(api):
    _req(api, "POST", "/api/v1/life/interests", body={"topic": "机器人"})
    status, body = _req(
        api, "POST", "/api/v1/life/feedback", body={"topic": "机器人", "signal": "liked"}
    )
    assert status == 200 and body["result"] == {"saved": True}
    status, body = _req(api, "GET", "/api/v1/life")
    assert body["result"]["interests"][0]["weight"] == pytest.approx(1.2)
    # ignored 不调权，只抬退避等级（与 WorkflowsService.explore 共享同一退避键）。
    status, _ = _req(
        api, "POST", "/api/v1/life/feedback", body={"topic": "机器人", "signal": "ignored"}
    )
    assert status == 200
    assert api.ctx.store.get(BACKOFF_KEY) == 1
    status, body = _req(api, "GET", "/api/v1/life")
    assert body["result"]["interests"][0]["weight"] == pytest.approx(1.2)
    # liked 会把退避清零。
    _req(api, "POST", "/api/v1/life/feedback", body={"topic": "机器人", "signal": "liked"})
    assert api.ctx.store.get(BACKOFF_KEY) == 0


def test_feedback_rejects_bad_signal(api):
    for body in (
        {"topic": "x", "signal": "love"},
        {"topic": "x"},
        {"signal": "liked"},
        {"topic": 1, "signal": "liked"},
    ):
        status, _ = _req(api, "POST", "/api/v1/life/feedback", body=body)
        assert status == 422, body
    # 反馈不发送任何消息：不产草稿、不触碰渠道。
    assert not [k for k in api.ctx.store.keys() if k.startswith("life:draft:")]


def test_life_returns_recent_diary_entries(api):
    store = api.ctx.store
    manager = _manager(store)
    manager.write_diary("第一天", sources=["https://example.com/a"])
    store.put("life:explore:diary:2020-01-01", [
        {"day": "2020-01-01", "note": "旧条目", "sources": []}
    ])
    status, body = _req(api, "GET", "/api/v1/life")
    assert status == 200
    diary = body["result"]["diary"]
    assert len(diary) == 2
    assert diary[0]["day"] == "2020-01-01"  # 按日期升序，旧条目在前
    assert diary[-1]["sources"] == ["https://example.com/a"]


def test_life_diary_window_limits_to_recent_seven_days(api):
    store = api.ctx.store
    for n in range(9):
        store.put(f"life:explore:diary:2020-01-{n + 1:02d}", [
            {"day": f"2020-01-{n + 1:02d}", "note": f"d{n}", "sources": []}
        ])
    status, body = _req(api, "GET", "/api/v1/life")
    days = [e["day"] for e in body["result"]["diary"]]
    assert days == [f"2020-01-0{n}" for n in range(3, 10)]


def test_life_unknown_routes_404(api):
    assert _req(api, "POST", "/api/v1/life", body={})[0] == 404
    assert _req(api, "GET", "/api/v1/life/interests")[0] == 404
    assert _req(api, "DELETE", "/api/v1/life")[0] == 404


def test_interest_tracker_consistency_with_workflows_namespace(api):
    """API 写入的兴趣必须落在 WorkflowsService.explore 读取的同一台账键。"""
    _req(api, "POST", "/api/v1/life/interests", body={"topic": "FPGA"})
    tracker = InterestTracker(api.ctx.store)
    assert tracker.as_interest_list() == ["FPGA"]
