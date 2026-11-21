"""#58 GUI 任务生产入口测试（GuiTaskService + REST 端点；FakeTransport 离线）。

口径锚点：
- 任务记录复用既有任务面（task:<id> + tasks:recent 索引）；
- start 前置门控（设备状态/基础 scope/决策模型配置）诚实拒绝；
- stop = 协程取消 + best-effort device.stop，终态幂等；
- shutdown 把在飞任务标 stopped（跨重启可对账）。
"""
from __future__ import annotations

import asyncio
import json
import time
from http.client import HTTPConnection

import pytest

pytest.importorskip("websockets")
pytest.importorskip("cryptography")

from whale_core.api.server import ApiContext, WhaleApiServer  # noqa: E402
from whale_core.devices.gateway_v2 import DeviceGatewayV2  # noqa: E402
from whale_core.devices.gui_agent.decision import GuiAction, ScriptedDecision  # noqa: E402
from whale_core.devices.gui_agent.service import GuiTaskError, GuiTaskService  # noqa: E402
from whale_core.devices.gui_agent.testing import FakeScreen, FakeTransport  # noqa: E402
from whale_core.devices.registry import DeviceRegistry  # noqa: E402
from whale_core.devices.transports.base import now_ms  # noqa: E402
from whale_core.memory.store import InMemoryStore  # noqa: E402

TOKEN = "gui-task-test"

GUI_SCOPES = ["device.observe", "device.tap", "device.stop"]


def _rig(scopes=GUI_SCOPES):
    store = InMemoryStore()
    registry = DeviceRegistry(store)
    pairing = registry.request_pairing(
        label="gui-test", pubkey_hex="ab" * 32, scopes=list(scopes))
    registry.approve(pairing["code"])
    gw2 = DeviceGatewayV2(registry, store=store)
    gui = FakeTransport([FakeScreen("home")], "home")
    gw2.register_transport(gui)
    return store, registry, gw2, pairing["device_id"], gui


def _wait_terminal(svc, task_id, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = svc.get(task_id)
        if record and record.get("status") in ("succeeded", "failed", "stopped"):
            return record
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout_s}s 内到达终态")


class TestStartGating:
    def test_empty_goal_rejected(self):
        store, registry, gw2, device_id, _ = _rig()
        svc = GuiTaskService(store, gw2,
                             decision_factory=lambda: ScriptedDecision([]))
        with pytest.raises(GuiTaskError) as exc:
            svc.start(device_id, "  ")
        assert exc.value.code == "bad_request"

    def test_revoked_device_rejected(self):
        store, registry, gw2, device_id, _ = _rig()
        registry.revoke(device_id)
        svc = GuiTaskService(store, gw2,
                             decision_factory=lambda: ScriptedDecision([]))
        with pytest.raises(GuiTaskError) as exc:
            svc.start(device_id, "打开设置")
        assert exc.value.code == "device_revoked"

    def test_missing_observe_scope_rejected(self):
        store, registry, gw2, device_id, _ = _rig(scopes=["device.tap"])
        svc = GuiTaskService(store, gw2,
                             decision_factory=lambda: ScriptedDecision([]))
        with pytest.raises(GuiTaskError) as exc:
            svc.start(device_id, "打开设置")
        assert exc.value.code == "scope_denied"

    def test_unconfigured_model_rejected(self):
        store, registry, gw2, device_id, _ = _rig()
        svc = GuiTaskService(store, gw2)  # 默认工厂走 env
        import os
        saved = {k: os.environ.pop(k, None) for k in (
            "WHALE_GUI_AGENT_MODEL", "WHALE_GUI_AGENT_BASE_URL",
            "WHALE_GUI_AGENT_API_KEY", "QQ_INTENT_MODEL", "QQ_INTENT_BASE_URL",
            "QQ_INTENT_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
            "DEEPSEEK_API_KEY")}
        try:
            with pytest.raises(GuiTaskError) as exc:
                svc.start(device_id, "打开设置")
            assert exc.value.code == "unavailable"
        finally:
            os.environ.update({k: v for k, v in saved.items() if v is not None})


class TestLifecycle:
    def test_task_runs_to_succeeded_and_lands_in_task_surface(self):
        store, registry, gw2, device_id, _ = _rig()
        svc = GuiTaskService(
            store, gw2,
            decision_factory=lambda: ScriptedDecision([
                GuiAction(kind="finish", done=True, reason="done-ok"),
            ]),
            trace_dir="/tmp/gui-task-test-traces")
        record = svc.start(device_id, "看一眼屏幕", max_steps=3)
        assert record["status"] == "pending"
        final = _wait_terminal(svc, record["task_id"])
        assert final["status"] == "succeeded"
        assert final["reason"] == "done-ok"
        assert final["kind"] == "gui_agent"
        assert final["device_id"] == device_id
        # 任务面索引（tasks:recent + 设备索引）
        recent = store.get("tasks:recent") or []
        assert record["task_id"] in recent
        assert store.get(f"task:{record['task_id']}")["status"] == "succeeded"
        listed = svc.list_for_device(device_id)
        assert listed and listed[0]["task_id"] == record["task_id"]

    def test_stop_cancels_running_task(self):
        store, registry, gw2, device_id, gui = _rig()

        class SlowObserve(FakeTransport):
            async def observe(self, device_id, request):
                await asyncio.sleep(0.3)
                return await super().observe(device_id, request)

        gw2.register_transport(SlowObserve([FakeScreen("home")], "home"))
        # 决策一直 observe（永不 finish）→ 任务持续在飞
        svc = GuiTaskService(
            store, gw2,
            decision_factory=lambda: ScriptedDecision(
                [GuiAction(kind="observe")] * 1000),
            trace_dir="/tmp/gui-task-test-traces")
        record = svc.start(device_id, "一直观察", max_steps=50,
                           task_timeout_s=600)
        # 轮询到 running 再停（全量负载下固定 sleep 不稳）
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = svc.get(record["task_id"])
            if current and current.get("status") == "running":
                break
            time.sleep(0.05)
        stopped = svc.stop(record["task_id"])
        assert stopped["stop_requested"] is True
        final = _wait_terminal(svc, record["task_id"])
        assert final["status"] == "stopped"
        # 终态幂等：再 stop 不翻车
        again = svc.stop(record["task_id"])
        assert again["status"] == "stopped"

    def test_stop_unknown_task_404(self):
        store, registry, gw2, device_id, _ = _rig()
        svc = GuiTaskService(store, gw2,
                             decision_factory=lambda: ScriptedDecision([]))
        with pytest.raises(GuiTaskError) as exc:
            svc.stop("nope")
        assert exc.value.code == "not_found"

    def test_hung_transport_watchdog_yields_timeout_terminal(self):
        """#58 真机实证：transport 悬挂不得挂死任务——看门狗给 TIMEOUT 终态。"""
        from whale_core.devices.gui_agent.loop import GuiAgentLoop

        class HungTransport(FakeTransport):
            async def execute(self, device_id, action):
                await asyncio.sleep(3600)  # 永远悬挂
                raise AssertionError("不可达")

        from whale_core.devices.gui_agent.decision import ScriptedDecision
        loop = GuiAgentLoop(HungTransport([FakeScreen("home")], "home"),
                            max_steps=10, action_timeout_ms=200,
                            watchdog_grace_ms=100, max_consecutive_failures=2)
        result = asyncio.run(loop.run(
            "dev1", goal="测试",
            decision=ScriptedDecision([GuiAction(kind="device.tap",
                                                 arguments={"x": 1, "y": 2})] * 10)))
        assert result.status == "failed"
        timeouts = [s for s in result.steps if s.outcome == "timeout"]
        assert timeouts, "悬挂动作应产生 timeout 步"
        assert all(s.error_code == "timeout" for s in timeouts)

    def test_shutdown_marks_inflight_stopped(self):
        store, registry, gw2, device_id, _ = _rig()

        class HungObserve(FakeTransport):
            async def observe(self, device_id, request):
                await asyncio.sleep(60)
                return await super().observe(device_id, request)

        gw2.register_transport(HungObserve([FakeScreen("home")], "home"))
        svc = GuiTaskService(store, gw2,
                             decision_factory=lambda: ScriptedDecision([]),
                             trace_dir="/tmp/gui-task-test-traces")
        record = svc.start(device_id, "挂起的观察", task_timeout_s=600)
        time.sleep(0.3)
        svc.shutdown()
        final = svc.get(record["task_id"])
        assert final["status"] == "stopped"
        assert "shutdown" in (final.get("reason") or "")


# ---------------------------------------------------------------- REST 面

@pytest.fixture()
def api():
    ctx = ApiContext(store=InMemoryStore(), profile="demo")
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.start()
    yield server
    server.stop()


def _req(server, method, path, *, body=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=15)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    payload = json.dumps(body) if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw.decode("utf-8"))


def _pair(api, scopes=GUI_SCOPES):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    key = Ed25519PrivateKey.generate()
    status, body = _req(api, "POST", "/api/v1/devices/pair",
                        body={"label": "gui-api",
                              "pubkey_hex": key.public_key().public_bytes_raw().hex(),
                              "scopes": list(scopes)})
    assert status == 200, body
    device_id = body["result"]["device_id"]
    status, _ = _req(api, "POST", "/api/v1/devices/pair/approve",
                     body={"code": body["result"]["code"]})
    assert status == 200
    return device_id


class TestRestSurface:
    def test_full_cycle_via_rest(self, api):
        device_id = _pair(api)
        # 注入脚本化决策（生产默认 LLM 工厂；测试不落模型）
        gw2 = api.ctx.device_gateway_v2
        gui = FakeTransport([FakeScreen("home")], "home")
        gw2.register_transport(gui)
        api.ctx.gui_task_service._decision_factory = lambda: ScriptedDecision(
            [GuiAction(kind="finish", done=True, reason="ok")])
        api.ctx.gui_task_service._trace_dir = "/tmp/gui-task-test-traces"

        status, body = _req(api, "POST",
                            f"/api/v1/devices/{device_id}/gui_tasks",
                            body={"goal": "打开设置", "max_steps": 3})
        assert status == 202, body
        task_id = body["result"]["task_id"]
        assert body["result"]["status"] == "pending"

        # 等终态
        final = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, body = _req(
                api, "GET", f"/api/v1/devices/{device_id}/gui_tasks/{task_id}")
            assert status == 200
            if body["result"]["status"] in ("succeeded", "failed", "stopped"):
                final = body["result"]
                break
            time.sleep(0.1)
        assert final is not None and final["status"] == "succeeded"

        # 列表面可见
        status, body = _req(api, "GET",
                            f"/api/v1/devices/{device_id}/gui_tasks")
        assert status == 200
        assert any(t["task_id"] == task_id for t in body["result"]["tasks"])
        # 既有任务面也可见（/tasks 同入口）
        status, body = _req(api, "GET", "/api/v1/tasks")
        assert status == 200
        assert any(t.get("task_id") == task_id
                   for t in body["result"]["tasks"])

    def test_bad_goal_400_and_scope_403(self, api):
        device_id = _pair(api)
        status, _ = _req(api, "POST", f"/api/v1/devices/{device_id}/gui_tasks",
                         body={"goal": ""})
        assert status == 400
        device2 = _pair(api, scopes=["device.tap"])  # 缺 observe
        status, body = _req(api, "POST", f"/api/v1/devices/{device2}/gui_tasks",
                            body={"goal": "打开设置"})
        assert status == 403 and body["error"]["code"] == "scope_denied"

    def test_stop_via_rest(self, api):
        device_id = _pair(api)
        gw2 = api.ctx.device_gateway_v2

        class SlowObserve(FakeTransport):
            async def observe(self, device_id, request):
                await asyncio.sleep(0.3)
                return await super().observe(device_id, request)

        gw2.register_transport(SlowObserve([FakeScreen("home")], "home"))
        api.ctx.gui_task_service._decision_factory = lambda: ScriptedDecision(
            [GuiAction(kind="observe")] * 1000)
        api.ctx.gui_task_service._trace_dir = "/tmp/gui-task-test-traces"
        status, body = _req(api, "POST",
                            f"/api/v1/devices/{device_id}/gui_tasks",
                            body={"goal": "一直观察", "task_timeout_s": 600})
        assert status == 202
        task_id = body["result"]["task_id"]
        time.sleep(0.5)
        status, body = _req(
            api, "POST", f"/api/v1/devices/{device_id}/gui_tasks/{task_id}/stop")
        assert status == 200, body
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, body = _req(api, "GET",
                           f"/api/v1/devices/{device_id}/gui_tasks/{task_id}")
            if body["result"]["status"] == "stopped":
                return
            time.sleep(0.1)
        raise AssertionError("任务未停止")
