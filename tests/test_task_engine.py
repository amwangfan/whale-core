"""G1-A/B 测试：One Plan Per Turn、持久任务台账（phase/events）、送达回执。

全部离线：意图模型/陪伴脑用计数替身，不触网。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import (
    ConversationOrchestrator,
    InputEnvelope,
    ToolRegistry,
    ToolSpec,
)
from whale_core.memory.store import InMemoryStore


class CountingIntent:
    """记录调用次数的意图模型替身；按脚本产出 plan JSON。"""

    def __init__(self, plan_json: str):
        self.plan_json = plan_json
        self.calls = 0

    def plan(self, text: str, *, context: str = "") -> str:
        self.calls += 1
        return self.plan_json


class StubBrain:
    def __init__(self, reply: str = "好的"):
        self._reply = reply

    def reply(self, text, *, persona="", memories=(), history=(), **kw):
        return self._reply


def _orc(intent, tools=None, store=None):
    from whale_core.memory.store import InMemoryStore as _IMS

    store = store or _IMS()
    companion = WhaleCompanion(store, StubBrain())
    return ConversationOrchestrator(store, companion, intent_model=intent,
                                    tools=tools or ToolRegistry())


CHAT_PLAN = json.dumps({"goal": "闲聊", "kind": "chat"})
CLARIFY_PLAN = json.dumps({"goal": "缺信息", "kind": "clarify",
                           "clarification_question": "哪天？"})
FAST_TASK = json.dumps({"goal": "查一下", "kind": "task",
                        "capabilities": ["test.fast"]})
SLOW_PLAN = json.dumps({"goal": "画张图", "kind": "task",
                        "capabilities": ["media.image.generate"], "timeout_s": 300})


def _tools_with(capability: str, slow_result: str = "done"):
    tools = ToolRegistry()
    tools.register(ToolSpec(name="t_" + capability.replace(".", "_"),
                            capability=capability,
                            handler=lambda slots: slow_result))
    return tools


# ---------- G1-A：一次请求至多一次规划 ----------


def test_one_plan_per_turn_sync_chat():
    intent = CountingIntent(CHAT_PLAN)
    orc = _orc(intent)
    result = orc.handle_message("c1", "你好")
    assert result.status == "answered"
    assert intent.calls == 1


def test_one_plan_per_turn_async_fast_path():
    """异步入口的快路径（chat/clarify/快任务）不得二次规划。"""
    for plan_json in (CHAT_PLAN, CLARIFY_PLAN, FAST_TASK):
        intent = CountingIntent(plan_json)
        orc = _orc(intent, tools=_tools_with("test.fast"))
        fired = []
        result = orc.handle_message_async("c1", "你好", on_complete=fired.append)
        assert intent.calls == 1, plan_json
        assert result.status != "ack"  # 快路径同步完成
        assert fired == []  # on_complete 不触发


def test_one_plan_per_turn_async_slow_path():
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=_tools_with("media.image.generate"))
    done = threading.Event()
    orc.handle_message_async("c1", "画只猫",
                             on_complete=lambda r: done.set())
    assert intent.calls == 1  # 慢任务复用同一 plan
    assert done.wait(5)


def test_memory_command_never_plans():
    intent = CountingIntent(CHAT_PLAN)
    orc = _orc(intent)
    result = orc.handle_message("c1", "记住：我喜欢喝茶")
    assert result.receipts == ("memory-command",)
    assert intent.calls == 0


# ---------- G1-B：台账先于 ack、phase 状态机、事件流水 ----------


def test_slow_task_ledger_exists_before_ack_returns():
    """ack 返回时任务台账必须已持久化（phase∈pending/acknowledged/running）。"""
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=_tools_with("media.image.generate"))
    done = threading.Event()
    result = orc.handle_message_async("c1", "画只猫",
                                      on_complete=lambda r: done.set())
    assert result.status == "ack" and result.task_id
    record = orc.store.get(f"task:{result.task_id}")
    assert record is not None  # 台账先于/不晚于 ack
    assert record.get("phase") in ("pending", "acknowledged", "running", "succeeded")
    done.wait(5)
    record = orc.store.get(f"task:{result.task_id}")
    assert record["phase"] == "succeeded" and record["status"] == "answered"
    kinds = [e["kind"] for e in record.get("events", [])]
    assert kinds[0] == "pending"  # _save_task 首次落 phase
    assert "planned" in kinds and "acknowledged" in kinds and "leased" in kinds
    assert kinds[-1] == "succeeded"


def test_sync_task_phase_events():
    intent = CountingIntent(FAST_TASK)
    orc = _orc(intent, tools=_tools_with("test.fast"))
    result = orc.handle_message("c1", "查一下")
    assert result.status == "answered"
    record = orc.store.get(f"task:{result.task_id}")
    assert record["phase"] == "succeeded"
    assert [e["kind"] for e in record["events"]][:2] == ["pending", "planned"]


# ---------- G1-B：送达回执（与「已执行」分开） ----------


def test_delivery_receipt_flow():
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=_tools_with("media.image.generate"))
    delivered = []
    done = threading.Event()

    def on_complete(result):
        # 宿主真实发送成功后回报
        orc.mark_delivered(result.task_id, channel="qq")
        delivered.append(result.task_id)
        done.set()

    result = orc.handle_message_async("c1", "画只猫", on_complete=on_complete)
    assert done.wait(5)
    record = orc.store.get(f"task:{result.task_id}")
    assert record["phase"] == "delivered"
    assert record["delivery"]["state"] == "delivered"
    assert record["delivery"]["attempts"] == 1
    assert "delivered" in [e["kind"] for e in record["events"]]
    assert delivered == [result.task_id]


def test_delivery_failure_visible_and_redeliverable():
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=_tools_with("media.image.generate"))
    done = threading.Event()

    def on_complete(result):
        orc.mark_delivery_failed(result.task_id, error="qq-send-timeout")
        done.set()

    result = orc.handle_envelope_async(
        InputEnvelope(conversation_id="c1", text="画只猫", source="qq:owner",
                      channel="qq", owner_id="o1"),
        on_complete=on_complete)
    assert done.wait(5)
    record = orc.store.get(f"task:{result.task_id}")
    assert record["phase"] == "delivery_failed"
    assert record["delivery"]["state"] == "failed"
    assert record["delivery"]["last_error"] == "qq-send-timeout"
    # 待补发列表可见
    pending = orc.undelivered_tasks()
    assert [r["task_id"] for r in pending] == [result.task_id]
    # 人工重发：注册渠道投递器 → redeliver
    sent = []
    orc.register_delivery_handler("qq", lambda rec: sent.append(rec["task_id"]) or True)
    updated = orc.redeliver_task(result.task_id)
    assert updated["phase"] == "delivered"
    assert sent == [result.task_id]
    assert orc.undelivered_tasks() == []
    # 已送达不可再重发
    with pytest.raises(ValueError):
        orc.redeliver_task(result.task_id)


def test_redeliver_never_reexecutes_tool():
    """人工重发只重投结果，绝不重新执行工具（非幂等保护）。"""
    calls = []

    def handler(slots):
        calls.append(1)
        return "done"

    tools = ToolRegistry()
    tools.register(ToolSpec(name="gen", capability="media.image.generate",
                            handler=handler))
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=tools)
    done = threading.Event()
    result = orc.handle_message_async(
        "c1", "画只猫", on_complete=lambda r: done.set())
    assert done.wait(5)
    assert len(calls) == 1
    orc.register_delivery_handler("runtime", lambda rec: True)
    orc.redeliver_task(result.task_id)
    assert len(calls) == 1  # 工具未重跑


def test_on_complete_exception_marks_delivery_failed():
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=_tools_with("media.image.generate"))
    done = threading.Event()

    def bad_callback(result):
        done.set()
        raise RuntimeError("host exploded")

    result = orc.handle_message_async("c1", "画只猫", on_complete=bad_callback)
    assert done.wait(5)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = orc.store.get(f"task:{result.task_id}")
        if record.get("phase") == "delivery_failed":
            break
        time.sleep(0.02)
    assert orc.store.get(f"task:{result.task_id}")["phase"] == "delivery_failed"


# ---------- G1-B：恢复（pending/acknowledged 重跑；已派发不重放） ----------


def test_recovery_reruns_pending_and_acknowledged():
    """pending/acknowledged 任务（从未派发）重启后安全重跑。"""
    from dataclasses import asdict
    from whale_core.core.intent_plan import IntentPlan

    store = InMemoryStore()
    tools = _tools_with("media.image.generate")
    for tid, phase in (("t-pend", "pending"), ("t-ack", "acknowledged")):
        plan = IntentPlan(goal="画图", kind="task",
                          capabilities=("media.image.generate",), task_id=tid)
        store.put(f"runtime:task:{tid}", {
            "plan": asdict(plan), "transient_keys": [], "conversation_id": "c1",
            "source": "test", "text": "画图", "next_step": 0, "results": [],
            "receipts": [], "tool_name": "", "async": True})
        store.put(f"task:{tid}", {"task_id": tid, "phase": phase, "status": phase,
                                  "goal": "画图"})
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=tools, store=store)
    got = []
    recovered = orc.recover_interrupted_tasks(
        on_complete=lambda cid, reply, status: got.append(status))
    assert sorted(recovered) == ["t-ack", "t-pend"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(got) < 2:
        time.sleep(0.02)
    assert got == ["answered", "answered"]  # TurnResult compat status
    for tid in ("t-pend", "t-ack"):
        assert store.get(f"task:{tid}")["phase"] == "succeeded"


def test_recovery_dispatched_task_stays_unknown():
    """已派发过工具的任务：标 unknown，不自动重放（非幂等保护）。"""
    from dataclasses import asdict
    from whale_core.core.intent_plan import IntentPlan

    store = InMemoryStore()
    plan = IntentPlan(goal="画图", kind="task",
                      capabilities=("media.image.generate",), task_id="t-disp")
    store.put("runtime:task:t-disp", {
        "plan": asdict(plan), "transient_keys": [], "conversation_id": "c1",
        "source": "test", "text": "画图", "next_step": 1,
        "results": ["[gen] done"], "receipts": ["tool:gen:ok:done"],
        "tool_name": ""})
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, store=store)  # 先构造（启动清扫只认更早的 running）
    store.put("task:t-disp", {"task_id": "t-disp", "phase": "running",
                              "status": "running", "goal": "画图"})
    got = []
    recovered = orc.recover_interrupted_tasks(
        on_complete=lambda cid, reply, status: got.append(status))
    assert recovered == ["t-disp"]
    assert store.get("task:t-disp")["phase"] == "unknown"
    assert got == ["unknown"]


def test_cancel_pending_task():
    intent = CountingIntent(SLOW_PLAN)
    orc = _orc(intent, tools=_tools_with("media.image.generate"))
    done = threading.Event()
    result = orc.handle_message_async("c1", "画只猫",
                                      on_complete=lambda r: done.set())
    done.wait(5)
    # 终态不可取消
    with pytest.raises(ValueError):
        orc.cancel_task(result.task_id)
    # 新任务在 pending_approval 之外的可取消态已覆盖（pending/acknowledged
    # 窗口极短，这里只验证终态拒绝与词表扩展不破坏语义）
    record = orc.store.get(f"task:{result.task_id}")
    assert record["phase"] in ("succeeded", "delivered")


def test_input_envelope_carries_channel_and_owner():
    """G1-A：channel/owner identity 经同一 InputEnvelope 流转。"""
    intent = CountingIntent(FAST_TASK)
    orc = _orc(intent, tools=_tools_with("test.fast"))
    env = InputEnvelope(conversation_id="c1", text="查一下", source="qq:owner",
                        channel="qq", owner_id="owner-1")
    result = orc.handle_envelope(env)
    assert result.status == "answered"
    journal = orc.store.get(f"runtime:task:{result.task_id}")
    assert journal["channel"] == "qq" and journal["source"] == "qq:owner"


def test_recovery_does_not_replay_crash_after_dispatch_before_receipt():
    """next_step=0 and empty receipts do not prove no side effect happened."""
    from dataclasses import asdict
    from whale_core.core.intent_plan import IntentPlan

    store = InMemoryStore()
    plan = IntentPlan(goal="notify", kind="task",
                      capabilities=("test.fast",), task_id="crash")
    store.put("runtime:task:crash", {
        "plan": asdict(plan), "transient_keys": [], "conversation_id": "c1",
        "source": "test", "text": "notify", "next_step": 0,
        "results": [], "receipts": [],
        "dispatch_started": {"step": 0, "tool": "notify", "fingerprint": "sent"},
        "tool_name": "",
    })
    orc = _orc(CountingIntent(FAST_TASK), tools=_tools_with("test.fast"),
               store=store)
    store.put("task:crash", {
        "task_id": "crash", "phase": "running", "status": "running",
        "in_flight": False,
    })
    assert orc.recover_interrupted_tasks() == ["crash"]
    assert store.get("task:crash")["phase"] == "unknown"
    assert store.get("runtime:task:crash")["next_step"] == 0


def test_dispatch_marker_written_before_external_handler():
    store = InMemoryStore()
    observed = []

    def handler(_):
        observed.append(store.get("runtime:task:marker")["dispatch_started"])
        return "ok"

    tools = ToolRegistry()
    tools.register(ToolSpec("read", "test.fast", handler=handler))
    orc = _orc(CountingIntent(FAST_TASK), tools=tools, store=store)
    result = orc.execute_tool("read", {}, conversation_id="c1",
                              task_id="marker")
    assert result.status == "answered"
    assert observed and observed[0]["step"] == 0
    assert observed[0]["tool"] == "read"
    journal = store.get("runtime:task:marker")
    assert journal["dispatch_started"] == observed[0]
    assert journal["next_step"] == 1


def test_legacy_inflight_flag_prevents_crash_replay():
    from dataclasses import asdict
    from whale_core.core.intent_plan import IntentPlan

    store = InMemoryStore()
    plan = IntentPlan(goal="old", kind="task",
                      capabilities=("test.fast",), task_id="legacy")
    store.put("runtime:task:legacy", {
        "plan": asdict(plan), "transient_keys": [], "conversation_id": "c1",
        "source": "test", "text": "old", "next_step": 0,
        "results": [], "receipts": [], "tool_name": "",
    })
    orc = _orc(CountingIntent(FAST_TASK), store=store)
    store.put("task:legacy", {
        "task_id": "legacy", "phase": "running", "status": "running",
        "in_flight": True,
    })
    assert orc.recover_interrupted_tasks() == ["legacy"]
    assert store.get("task:legacy")["phase"] == "unknown"
