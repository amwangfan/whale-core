"""GuiAgentLoop 测试（全部 FakeTransport，不依赖真机/模型）。

覆盖任务书用例 1-9。
"""
from __future__ import annotations

import asyncio
import json
import os

from whale_core.devices.gui_agent.approval import classify_risk
from whale_core.devices.gui_agent.decision import (
    GuiAction,
    LlmJsonDecision,
    ScriptedDecision,
    parse_action_json,
)
from whale_core.devices.gui_agent.loop import GuiAgentLoop
from whale_core.devices.gui_agent.testing import (
    FakeScreen,
    FakeTransition,
    FakeTransport,
    make_element,
)
from whale_core.devices.gui_agent.trace import RunTraceWriter


def run(coro):
    return asyncio.run(coro)


def home_then_settings():
    """两屏：home --app.launch com.android.settings--> settings。"""
    home = FakeScreen(
        "home", foreground_package="com.android.launcher",
        elements=[make_element(1, "设置")],
        transitions=[FakeTransition("device.app.launch",
                                    {"package": "com.android.settings"}, "settings")],
    )
    settings = FakeScreen(
        "settings", foreground_package="com.android.settings",
        activity=".Settings",
        elements=[make_element(1, "搜索"), make_element(2, "WLAN")],
    )
    return FakeTransport([home, settings], "home")


class TestHappyPath:
    def test_multi_step_succeeded_and_trace(self, tmp_path):
        trace = RunTraceWriter("run_TEST01", sink_dir=str(tmp_path))
        decision = ScriptedDecision([
            GuiAction(kind="device.app.launch",
                      arguments={"package": "com.android.settings"},
                      label="打开设置"),
            GuiAction(kind="finish", done=True, reason="到达设置页"),
        ])
        loop = GuiAgentLoop(FakeTransport(*_screens(home_then_settings())))
        result = run(loop.run("dev1", goal="打开设置", decision=decision,
                              trace=trace))
        assert result.status == "succeeded"
        assert result.run_id == "run_TEST01"
        # trace：task_started → observation → action_started → observation →
        #       action_completed → task_completed
        kinds = [e["kind"] for e in trace.events]
        assert kinds[0] == "task_started" and kinds[-1] == "task_completed"
        assert "action_completed" in kinds
        seqs = [e["seq"] for e in trace.events]
        assert seqs == list(range(1, len(seqs) + 1))
        ts = [e["timestamp_ms"] for e in trace.events]
        assert ts == sorted(ts)
        # JSONL 落盘
        path = os.path.join(str(tmp_path), "run_TEST01.jsonl")
        lines = open(path, encoding="utf-8").read().strip().splitlines()
        assert len(lines) == len(kinds)
        assert json.loads(lines[0])["run_id"] == "run_TEST01"
        # act 后观察复用：observe 次数 = 初始 1 + act 后 1
        transport = loop.transport
        assert transport.observe_calls == 2

    def test_launch_verification_passed(self):
        decision = ScriptedDecision([
            GuiAction(kind="device.app.launch",
                      arguments={"package": "com.android.settings"}),
            GuiAction(kind="finish", done=True),
        ])
        loop = GuiAgentLoop(FakeTransport(*_screens(home_then_settings())))
        result = run(loop.run("dev1", goal="g", decision=decision))
        assert result.steps[0].verification == "passed"


def _screens(transport):
    return transport._screens.values(), transport._current


class TestStale:
    def test_stale_forces_reobserve_not_failure(self):
        transport = FakeTransport(*_screens(home_then_settings()))
        transport.force_stale_once = True
        decision = ScriptedDecision([
            GuiAction(kind="device.tap", arguments={"element_id": 1}),
            GuiAction(kind="device.tap", arguments={"element_id": 1}),
            GuiAction(kind="finish", done=True),
        ])
        loop = GuiAgentLoop(transport)
        result = run(loop.run("dev1", goal="g", decision=decision))
        stale_steps = [s for s in result.steps if s.outcome == "stale_observation"]
        assert len(stale_steps) == 1
        # stale 不计连续失败：第二次 tap 无 transition 只记 1 次失败，
        # 脚本继续走到 finish → succeeded
        failed = [s for s in result.steps if s.outcome == "failed"]
        assert len(failed) == 1
        assert result.status == "succeeded"
        assert result.steps[0].error_code == "stale_observation"


class TestLimits:
    def test_max_steps(self):
        transport = FakeTransport(*_screens(home_then_settings()))
        # 永远不 finish：device.wait 是安全动作（fake 无 transition → failed）
        # 用 observe 动作不计步；改用 wait + 注入 transition 同屏
        home = FakeScreen(
            "home", elements=[],
            transitions=[FakeTransition("device.wait", {}, "")])
        transport = FakeTransport([home], "home")
        decision = ScriptedDecision([])  # 队列空 → finish，不能用
        # 自定义决策：永远 wait
        class AlwaysWait:
            async def decide(self, **kw):
                return GuiAction(kind="device.wait", arguments={"ms": 100})
        loop = GuiAgentLoop(transport, max_steps=3)
        result = run(loop.run("dev1", goal="g", decision=AlwaysWait()))
        assert result.status == "stopped" and result.reason == "max-steps"
        assert len(result.steps) == 3

    def test_max_consecutive_failures(self):
        home = FakeScreen("home", elements=[])  # 无 transitions → 全 failed
        transport = FakeTransport([home], "home")

        class AlwaysTap:
            async def decide(self, **kw):
                return GuiAction(kind="device.tap", arguments={"x": 1, "y": 2})

        loop = GuiAgentLoop(transport, max_consecutive_failures=3)
        result = run(loop.run("dev1", goal="g", decision=AlwaysTap()))
        assert result.status == "failed"
        assert result.reason == "max-consecutive-failures"
        assert len(result.steps) == 3

    def test_task_timeout(self):
        home = FakeScreen("home", elements=[],
                          transitions=[FakeTransition("device.wait", {}, "")])
        transport = FakeTransport([home], "home")

        class AlwaysWait:
            async def decide(self, **kw):
                return GuiAction(kind="device.wait", arguments={"ms": 1})

        loop = GuiAgentLoop(transport, task_timeout_ms=1)  # 立即超时
        result = run(loop.run("dev1", goal="g", decision=AlwaysWait()))
        assert result.status == "failed" and result.reason == "task-timeout"


class TestApproval:
    def test_risky_without_port_denied(self):
        home = FakeScreen("home", elements=[make_element(1, "发送")])
        transport = FakeTransport([home], "home")
        decision = ScriptedDecision([
            GuiAction(kind="device.tap", arguments={"element_id": 1}),
            GuiAction(kind="finish", done=True),
        ])
        loop = GuiAgentLoop(transport)
        result = run(loop.run("dev1", goal="g", decision=decision))
        assert result.steps[0].outcome == "approval_denied"
        assert result.steps[0].error_code == "approval_denied"
        assert transport.execute_calls == 0  # 未执行

    def test_risky_with_port_approved(self):
        home = FakeScreen(
            "home", elements=[make_element(1, "发送")],
            transitions=[FakeTransition("device.tap", {"element_id": 1}, "home")])
        transport = FakeTransport([home], "home")

        class AllowAll:
            async def request(self, **kw):
                return True

        decision = ScriptedDecision([
            GuiAction(kind="device.tap", arguments={"element_id": 1}),
            GuiAction(kind="finish", done=True),
        ])
        loop = GuiAgentLoop(transport)
        result = run(loop.run("dev1", goal="g", decision=decision,
                              approval=AllowAll()))
        assert result.steps[0].outcome == "succeeded"
        assert transport.execute_calls == 1

    def test_risky_with_port_denied(self):
        home = FakeScreen("home", elements=[make_element(1, "删除")])
        transport = FakeTransport([home], "home")

        class DenyAll:
            async def request(self, **kw):
                return False

        decision = ScriptedDecision([
            GuiAction(kind="device.tap", arguments={"element_id": 1}),
            GuiAction(kind="finish", done=True),
        ])
        loop = GuiAgentLoop(transport)
        result = run(loop.run("dev1", goal="g", decision=decision,
                              approval=DenyAll()))
        assert result.steps[0].outcome == "approval_denied"
        assert transport.execute_calls == 0

    def test_classify_risk(self):
        obs = None
        assert classify_risk("device.app.install", {}, obs) is not None
        assert classify_risk("device.wait", {"ms": 100}, obs) is None
        assert classify_risk("device.tap", {"x": 1, "y": 2}, obs) is None


class TestTimeoutDiscipline:
    def test_side_effect_timeout_not_replayed(self):
        """侧效动作超时：不自动重放，重新观察后交决策层。"""
        t = _TimeoutTransport()
        calls = {"decide": 0}

        class DecideOnce:
            async def decide(self, **kw):
                calls["decide"] += 1
                if calls["decide"] == 1:
                    return GuiAction(kind="device.tap", arguments={"x": 1, "y": 2})
                return GuiAction(kind="finish", done=True)

        loop = GuiAgentLoop(t)
        result = run(loop.run("dev1", goal="g", decision=DecideOnce()))
        assert result.steps[0].outcome == "timeout"
        assert t.execute_calls == 1  # 没有重放
        assert result.status == "succeeded"  # 决策层选择 finish


class _TimeoutTransport(FakeTransport):
    def __init__(self):
        super().__init__([FakeScreen("home", elements=[])], "home")

    async def execute(self, device_id, action):
        from whale_core.devices.transports.base import (
            ActionReceipt,
            ReceiptStatus,
            now_ms,
        )
        self.execute_calls += 1
        return ActionReceipt(
            action_id=action.action_id, status=ReceiptStatus.TIMEOUT,
            summary="执行超时", error_code="timeout", completed_at=now_ms())


class TestHonesty:
    def test_verification_skipped_when_indeterminate(self):
        """wait 动作验证必须是 skipped。"""
        home = FakeScreen("home", elements=[],
                          transitions=[FakeTransition("device.wait", {}, "")])
        transport = FakeTransport([home], "home")
        decision = ScriptedDecision([
            GuiAction(kind="device.wait", arguments={"ms": 100}),
            GuiAction(kind="finish", done=True),
        ])
        loop = GuiAgentLoop(transport)
        result = run(loop.run("dev1", goal="g", decision=decision))
        assert result.steps[0].verification == "skipped"


class TestDecisionParsing:
    def test_unparseable_llm_output_stops(self):
        a = parse_action_json("我不知道怎么做")
        assert a.kind == "finish" and not a.success

    def test_bad_action_stops(self):
        a = parse_action_json('{"action": "device.shell", "arguments": {}}')
        assert a.kind == "finish" and not a.success

    def test_key_whitelist(self):
        a = parse_action_json('{"action": "device.key", "arguments": {"key": "power_off_evil"}}')
        assert a.kind == "finish" and not a.success
        b = parse_action_json('{"action": "device.key", "arguments": {"key": "back"}}')
        assert b.kind == "device.key"

    def test_llm_decision_end_to_end(self):
        async def fake_complete(prompt):
            return '```json\n{"action": "device.wait", "arguments": {"ms": 100}, "label": "等待"}\n```'

        d = LlmJsonDecision(fake_complete)
        action = run(d.decide(goal="g", observation=None, history=[], stuck_hint=""))
        assert action.kind == "device.wait" and action.arguments["ms"] == 100


class TestLockedGuard:
    def test_persistent_lock_fails_fast(self):
        """持续锁屏：解锁预算耗尽后 device-locked 快速失败（不烧步数）。"""
        locked = FakeScreen(
            "lock", foreground_package="com.android.systemui", locked=True,
            elements=[],
            transitions=[FakeTransition("device.swipe", {}, "")])  # 滑了也不解锁
        transport = FakeTransport([locked], "lock")

        class AlwaysSwipe:
            async def decide(self, **kw):
                return GuiAction(kind="device.swipe",
                                 arguments={"x1": 1, "y1": 100, "x2": 1, "y2": 50})

        loop = GuiAgentLoop(transport, max_steps=50)
        result = run(loop.run("dev1", goal="g", decision=AlwaysSwipe()))
        assert result.status == "failed" and result.reason == "device-locked"
        assert len(result.steps) == 3  # 解锁预算 3 次

    def test_unlock_then_proceed(self):
        """锁屏被滑开后正常推进（预算不阻正常路径）。"""
        lock = FakeScreen(
            "lock", locked=True, elements=[],
            transitions=[FakeTransition("device.swipe", {}, "home")])
        home = FakeScreen("home", foreground_package="com.example.home")
        transport = FakeTransport([lock, home], "lock")
        decision = ScriptedDecision([
            GuiAction(kind="device.swipe",
                      arguments={"x1": 1, "y1": 100, "x2": 1, "y2": 50}),
            GuiAction(kind="finish", done=True),
        ])
        loop = GuiAgentLoop(transport)
        result = run(loop.run("dev1", goal="g", decision=decision))
        assert result.status == "succeeded"
        assert result.steps[0].outcome == "succeeded"


class TestStuckHint:
    def test_repeated_action_hint_reaches_decision(self):
        home = FakeScreen(
            "home", elements=[],
            transitions=[FakeTransition("device.tap", {"x": 1}, "")])
        transport = FakeTransport([home], "home")

        class AlwaysTap:
            def __init__(self):
                self.hints = []

            async def decide(self, *, stuck_hint="", **kw):
                self.hints.append(stuck_hint)
                return GuiAction(kind="device.tap", arguments={"x": 1, "y": 1})

        decision = AlwaysTap()
        loop = GuiAgentLoop(transport, max_steps=5,
                            max_consecutive_failures=99)
        result = run(loop.run("dev1", goal="g", decision=decision))
        # 同一动作同屏三次成功但状态不变 → repeated-action 提示
        assert any("repeated-action" in h for h in decision.hints)
