"""core#58：安卓自主体编排循环 + 失败语义 + revoke/scope 停止。"""
from __future__ import annotations

from whale_core.devices.agent_failures import (
    FAILED_SCOPE_DENIED,
    FAILED_TIMEOUT,
    FAILED_UNAVAILABLE,
    UNKNOWN,
    classify_exception,
    classify_gateway_code,
    classify_receipt,
    should_abort_loop,
    should_auto_retry,
)
from whale_core.devices.agent_loop import (
    CAP_CONTROL,
    CAP_OBSERVE,
    CAP_OUTPUT_READ,
    CAP_SHELL,
    AgentDecision,
    DeviceAgentLoop,
    ScriptedDecisionPort,
    SimulatedOutputReadPort,
    device_agent_space_id,
)
from whale_core.devices.bridge import SimulatedActionPort
from whale_core.devices.registry import DeviceRegistry
from whale_core.life.autonomy import DeviceUnavailable
from whale_core.memory.store import InMemoryStore, MemoryKind


AGENT_SCOPES = (CAP_OBSERVE, CAP_CONTROL, CAP_SHELL, CAP_OUTPUT_READ)


def _active(scopes=AGENT_SCOPES):
    store = InMemoryStore()
    registry = DeviceRegistry(store)
    info = registry.request_pairing(
        label="agent-phone", pubkey_hex="ab" * 32, scopes=scopes)
    registry.approve(info["code"])
    return store, registry, info["device_id"]


def _observe_receipt(**kw):
    base = {
        "state": "executed",
        "action_id": "sim-obs-1",
        "executed_at": 1,
        "result": {
            "summary": "UI: Settings > Display",
            "output_ref": "agent:ui:latest",
        },
    }
    base.update(kw)
    return base


def _control_receipt(**kw):
    base = {
        "state": "executed",
        "action_id": "sim-ctl-1",
        "executed_at": 2,
        "result": {"summary": "tapped Display"},
    }
    base.update(kw)
    return base


# ---- failure semantics -----------------------------------------------------


def test_classify_gateway_codes():
    assert classify_gateway_code("expired") == FAILED_TIMEOUT
    assert classify_gateway_code("timeout") == FAILED_TIMEOUT
    assert classify_gateway_code("unavailable") == FAILED_UNAVAILABLE
    assert classify_gateway_code("device_revoked") == FAILED_UNAVAILABLE
    assert classify_gateway_code("permission_missing") == FAILED_UNAVAILABLE
    assert classify_gateway_code("scope_denied") == FAILED_SCOPE_DENIED
    assert classify_gateway_code("capability_unknown") == FAILED_SCOPE_DENIED
    assert classify_gateway_code("unknown") == UNKNOWN


def test_classify_receipt_failed_variants():
    assert classify_receipt({
        "state": "failed", "action_id": "a1", "error_code": "timeout",
    })[0] == FAILED_TIMEOUT
    assert classify_receipt({
        "state": "failed", "action_id": "a1", "error_code": "unavailable",
    })[0] == FAILED_UNAVAILABLE
    assert classify_receipt({
        "state": "failed", "action_id": "a1", "error_code": "scope_denied",
    })[0] == FAILED_SCOPE_DENIED
    assert classify_receipt({"state": "unknown", "action_id": "a1"})[0] == UNKNOWN
    assert classify_receipt({"state": "executed", "action_id": "a1"})[0] == "executed"


def test_unknown_never_auto_retries_and_aborts():
    assert not should_auto_retry(UNKNOWN)
    assert not should_auto_retry(UNKNOWN, capability=CAP_OBSERVE)
    assert should_abort_loop(UNKNOWN)
    # 仅只读 observe 的 timeout 可自动重试；control/shell/缺省一律否
    assert should_auto_retry(FAILED_TIMEOUT, capability=CAP_OBSERVE)
    assert not should_auto_retry(FAILED_TIMEOUT, capability=CAP_CONTROL)
    assert not should_auto_retry(FAILED_TIMEOUT, capability=CAP_SHELL)
    assert not should_auto_retry(FAILED_TIMEOUT)
    assert should_abort_loop(FAILED_SCOPE_DENIED)
    assert should_abort_loop(FAILED_UNAVAILABLE)
    assert classify_exception(DeviceUnavailable("expired", "t")) == FAILED_TIMEOUT
    assert classify_exception(RuntimeError("boom")) == UNKNOWN


# ---- happy path observe → control -----------------------------------------


def test_observe_decide_control_one_lap_and_private_memory():
    store, registry, device_id = _active()
    port = SimulatedActionPort(
        registry,
        online={device_id: True},
        receipts={
            CAP_OBSERVE: _observe_receipt(),
            CAP_CONTROL: _control_receipt(),
        },
    )
    decisions = [
        AgentDecision(kind="observe", args={"modality": "ui_tree"},
                      reason="look"),
        AgentDecision(kind="control", args={"action": "tap", "x": 10, "y": 20},
                      reason="open display"),
        AgentDecision(kind="stop", reason="goal-met", done=True),
    ]
    loop = DeviceAgentLoop(
        registry, port,
        decision=ScriptedDecisionPort(decisions),
        output_port=SimulatedOutputReadPort({
            "agent:ui:latest": {"status": "ok", "data": [{"ui_tree": "Settings>Display"}]},
        }),
        store=store,
        max_steps=5,
    )
    result = loop.run(device_id, goal="打开显示设置")
    assert result.status == "succeeded"
    caps = [s.capability for s in result.steps]
    assert caps == [CAP_OBSERVE, CAP_CONTROL]
    assert all(s.outcome == "executed" for s in result.steps)
    assert "Display" in result.last_summary or result.steps[0].summary
    # 设备私有记忆命名空间
    space = device_agent_space_id(device_id)
    episodes = store.list_by_kind(MemoryKind.EPISODE, space_ids=[space])
    assert episodes
    assert any("Display" in (r.content.get("summary") or "") for r in episodes)
    # 不污染默认/空 space
    assert not store.list_by_kind(MemoryKind.EPISODE, space_ids=[""])


# ---- stop: revoke / narrow scope ------------------------------------------


def test_revoke_stops_loop_without_replay():
    store, registry, device_id = _active()
    calls = {"n": 0}

    def observe_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _observe_receipt(action_id="obs-1")
        raise AssertionError("不得在吊销后继续下发动作（禁止重放）")

    port = SimulatedActionPort(
        registry, online={device_id: True},
        receipts={CAP_OBSERVE: observe_once, CAP_CONTROL: _control_receipt()},
    )

    class RevokeAfterObserve:
        def __init__(self):
            self.seen = 0

        def decide(self, *, goal, observation, history):
            self.seen += 1
            if self.seen == 1:
                return AgentDecision(kind="observe", args={}, reason="1st")
            # 观察完成后吊销：下一步 control 前应被 preflight 拦住
            registry.revoke(device_id)
            return AgentDecision(kind="control", args={"action": "tap"},
                                 reason="should-not-run")

    loop = DeviceAgentLoop(
        registry, port, decision=RevokeAfterObserve(), store=store, max_steps=5)
    result = loop.run(device_id, goal="x")
    assert result.status == "stopped"
    assert result.reason == FAILED_UNAVAILABLE
    assert len(result.steps) == 1
    assert calls["n"] == 1


def test_narrow_scope_stops_loop():
    store, registry, device_id = _active()
    port = SimulatedActionPort(
        registry, online={device_id: True},
        receipts={CAP_OBSERVE: _observe_receipt()},
    )

    class NarrowAfterObserve:
        def __init__(self):
            self.seen = 0

        def decide(self, *, goal, observation, history):
            self.seen += 1
            if self.seen == 1:
                return AgentDecision(kind="observe", args={}, reason="look")
            # 收窄：去掉 control/shell/observe —— 下一步 control 失败
            registry.set_scopes(device_id, [CAP_OUTPUT_READ])
            return AgentDecision(kind="control", args={"action": "tap"},
                                 reason="should-stop")

    loop = DeviceAgentLoop(
        registry, port, decision=NarrowAfterObserve(), store=store, max_steps=5)
    result = loop.run(device_id, goal="x")
    assert result.status == "stopped"
    assert result.reason == FAILED_SCOPE_DENIED
    assert len(result.steps) == 1


# ---- failure outcomes in loop ---------------------------------------------


def test_unknown_receipt_aborts_no_replay():
    store, registry, device_id = _active()
    port = SimulatedActionPort(
        registry, online={device_id: True},
        receipts={CAP_OBSERVE: {
            "state": "unknown", "action_id": "u1",
        }},
    )
    loop = DeviceAgentLoop(
        registry, port,
        decision=ScriptedDecisionPort([
            AgentDecision(kind="observe", args={}, reason="look"),
            AgentDecision(kind="control", args={}, reason="should-not"),
        ]),
        store=store, max_steps=4, timeout_retries=0,
    )
    result = loop.run(device_id, goal="x")
    assert result.status == "failed"
    assert result.reason == UNKNOWN
    assert len(result.steps) == 1


def test_timeout_may_retry_then_succeed():
    store, registry, device_id = _active()
    n = {"i": 0}

    def flaky(**kwargs):
        n["i"] += 1
        if n["i"] == 1:
            raise DeviceUnavailable("expired", "wait timeout")
        return _observe_receipt(action_id=f"obs-{n['i']}")

    port = SimulatedActionPort(
        registry, online={device_id: True},
        receipts={
            CAP_OBSERVE: flaky,
            CAP_CONTROL: _control_receipt(),
        },
    )
    loop = DeviceAgentLoop(
        registry, port,
        decision=ScriptedDecisionPort([
            AgentDecision(kind="observe", args={}, reason="look"),
            AgentDecision(kind="control", args={"action": "tap"}, reason="go"),
            AgentDecision(kind="stop", done=True, reason="ok"),
        ]),
        store=store, max_steps=6, timeout_retries=1,
    )
    result = loop.run(device_id, goal="x")
    assert result.status == "succeeded"
    assert any(s.outcome == FAILED_TIMEOUT for s in result.steps)
    assert any(s.capability == CAP_CONTROL and s.outcome == "executed"
               for s in result.steps)



def test_control_timeout_pending_no_redispatch():
    """control 超时且 status=pending：升 unknown，send_action 只调用 1 次。"""
    store, registry, device_id = _active()
    calls = {"n": 0}

    def control_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeviceUnavailable("expired", "wait timeout")
        raise AssertionError("control 超时 pending 时禁止用新幂等键再次下发")

    port = SimulatedActionPort(
        registry, online={device_id: True},
        receipts={
            CAP_OBSERVE: _observe_receipt(),
            CAP_CONTROL: control_once,
        },
    )
    loop = DeviceAgentLoop(
        registry, port,
        decision=ScriptedDecisionPort([
            AgentDecision(kind="observe", args={}, reason="look"),
            AgentDecision(kind="control", args={"action": "tap", "x": 1, "y": 2},
                          reason="buy"),
            AgentDecision(kind="stop", done=True, reason="should-not"),
        ]),
        store=store, max_steps=6, timeout_retries=2,
    )
    result = loop.run(device_id, goal="x")
    assert result.status == "failed"
    assert result.reason == UNKNOWN
    assert calls["n"] == 1
    control_steps = [s for s in result.steps if s.capability == CAP_CONTROL]
    assert len(control_steps) == 1
    assert control_steps[0].outcome == UNKNOWN
    assert "pending" in control_steps[0].detail or "reconcile" in control_steps[0].detail


def test_shell_timeout_unknown_no_redispatch():
    """shell 超时且 status 合成 unknown：失败且不再下发。"""
    store, registry, device_id = _active()
    clock = {"t": 1_000_000}

    def now():
        return clock["t"]

    calls = {"n": 0}

    def shell_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeviceUnavailable("expired", "wait timeout")
        raise AssertionError("shell 超时 unknown 时禁止再次下发")

    port = SimulatedActionPort(
        registry, online={device_id: True},
        receipts={
            CAP_OBSERVE: _observe_receipt(),
            CAP_SHELL: shell_once,
        },
        now_fn=now,
    )
    loop = DeviceAgentLoop(
        registry, port,
        decision=ScriptedDecisionPort([
            AgentDecision(kind="observe", args={}, reason="look"),
            AgentDecision(kind="shell", args={"cmd": "id"}, reason="probe"),
            AgentDecision(kind="stop", done=True, reason="should-not"),
        ]),
        store=store, max_steps=6, timeout_retries=2, timeout_s=1.0,
    )
    # 下发时 expires_at = 1_000_000 + 1000；对账前把时钟推过期 → 合成 unknown
    def _run():
        # 在第一次 shell 超时后、reconcile 前推进时钟：
        # DeviceAgentLoop 同步调用，故在 send 抛出前无法插入钩子；
        # 改为：超时脚本抛出前把时钟推过 expires（保留 pending 预约，status 合成 unknown）。
        return loop.run(device_id, goal="x")

    # 包装：第一次 shell 调用时先 reserve（port 内部），我们让 expires 立即过期
    orig_send = port.send_action

    def send_and_expire(device_id, **kwargs):
        # 先把 now 设在起点；send 内部 reserve 后 expires_at=now+1000
        out = None
        err = None
        try:
            out = orig_send(device_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            err = exc
        if kwargs.get("capability") == CAP_SHELL:
            clock["t"] = clock["t"] + 10_000  # 过期 → action_status 合成 unknown
        if err is not None:
            raise err
        return out

    port.send_action = send_and_expire  # type: ignore[method-assign]
    result = _run()
    assert result.status == "failed"
    assert result.reason == UNKNOWN
    assert calls["n"] == 1
    shell_steps = [s for s in result.steps if s.capability == CAP_SHELL]
    assert len(shell_steps) == 1
    assert shell_steps[0].outcome == UNKNOWN


def test_control_timeout_not_found_may_retry():
    """control 超时且 status=not_found：证明未落地，允许有界新键重试。"""
    store, registry, device_id = _active()
    calls = {"n": 0}

    def control_flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeviceUnavailable("expired", "wait timeout")
        return _control_receipt(action_id=f"ctl-{calls['n']}")

    port = SimulatedActionPort(
        registry, online={device_id: True},
        receipts={
            CAP_OBSERVE: _observe_receipt(),
            CAP_CONTROL: control_flaky,
        },
    )
    # 第一次超时后清掉幂等记录 → action_status 报 not_found
    orig_send = port.send_action

    def send_clear_on_timeout(device_id, **kwargs):
        try:
            return orig_send(device_id, **kwargs)
        except DeviceUnavailable as exc:
            if exc.code == "expired":
                port._idem_records.pop((device_id, kwargs["idempotency_key"]), None)
            raise

    port.send_action = send_clear_on_timeout  # type: ignore[method-assign]
    loop = DeviceAgentLoop(
        registry, port,
        decision=ScriptedDecisionPort([
            AgentDecision(kind="observe", args={}, reason="look"),
            AgentDecision(kind="control", args={"action": "tap"}, reason="go"),
            AgentDecision(kind="stop", done=True, reason="ok"),
        ]),
        store=store, max_steps=6, timeout_retries=1,
    )
    result = loop.run(device_id, goal="x")
    assert result.status == "succeeded"
    assert calls["n"] == 2
    assert any(s.capability == CAP_CONTROL and s.outcome == "executed"
               for s in result.steps)



def test_set_scopes_rejects_unknown_and_inactive():
    store, registry, device_id = _active()
    import pytest
    from whale_core.devices.registry import DeviceRegistryError
    with pytest.raises(DeviceRegistryError) as ei:
        registry.set_scopes(device_id, ["not.a.real.cap"])
    assert ei.value.code == "bad_request"
    registry.revoke(device_id)
    with pytest.raises(DeviceRegistryError) as ei:
        registry.set_scopes(device_id, [CAP_OBSERVE])
    assert ei.value.code == "conflict"
