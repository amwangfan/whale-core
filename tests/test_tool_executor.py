"""G1-C：多轮工具执行模型测试（离线替身，不触网）。

验证：模型逐轮提议、核心裁决执行；非法提议/未知工具计数失败；finish/fail；
审批与网关门控与固定链同路径；固定链（声明能力）仍是默认保底模式。
"""

from __future__ import annotations

import json

import pytest

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.core.tool_executor import (
    ExecutorStepError,
    ModelToolExecutor,
    parse_proposal,
)
from whale_core.memory.store import InMemoryStore


class StubBrain:
    def reply(self, text, *, persona="", memories=(), history=(), **kw):
        return "好的"


class ScriptExecutorModel:
    """按队列逐步输出提议的执行模型替身。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.prompts: list[str] = []

    def reply(self, text, *, persona="", memories=(), history=(), **kw):
        self.prompts.append(text)
        return self.steps.pop(0)


class NullIntent:
    """把任意输入变成 kind=task、无能力声明的 plan（触发执行器模式）。"""

    def plan(self, text, *, context=""):
        return json.dumps({"goal": text[:100], "kind": "task", "capabilities": []})


def _orc(executor_steps, tool_handlers, store=None):
    from whale_core.memory.store import InMemoryStore as _IMS

    store = store or _IMS()
    tools = ToolRegistry()
    for name, handler in tool_handlers.items():
        tools.register(ToolSpec(name=name, capability=f"test.{name}",
                                description=f"{name} 工具", handler=handler))
    companion = WhaleCompanion(store, StubBrain())
    return ConversationOrchestrator(
        store, companion, intent_model=NullIntent(), tools=tools,
        executor_model=ScriptExecutorModel(executor_steps))


# ---------- 提议解析 ----------


def test_parse_proposal_variants():
    p = parse_proposal('{"action":"call","tool":"search","args":{"q":"天气"},"rationale":"查一下"}')
    assert p.action == "call" and p.tool == "search" and p.args == {"q": "天气"}
    assert parse_proposal('```json\n{"action":"finish","rationale":"好了"}\n```').action == "finish"
    assert parse_proposal('前言 {"action":"fail","rationale":"做不到"} 后记').action == "fail"
    with pytest.raises(ExecutorStepError):
        parse_proposal("没有 JSON")
    with pytest.raises(ExecutorStepError):
        parse_proposal('{"action":"approve_all"}')  # 未知动作
    with pytest.raises(ExecutorStepError):
        parse_proposal('{"action":"call"}')  # 缺 tool
    with pytest.raises(ExecutorStepError):
        parse_proposal('{"action":"call","tool":"x","args":"不是映射"}')


def test_parse_proposal_args_size_guard():
    with pytest.raises(ExecutorStepError):
        parse_proposal(json.dumps({"action": "call", "tool": "x",
                                   "args": {"big": "y" * 5000}}))


# ---------- 执行器模式 ----------


def test_executor_two_round_call_then_finish():
    calls = []
    steps = [
        json.dumps({"action": "call", "tool": "search", "args": {"q": "北京天气"},
                    "rationale": "先查"}),
        json.dumps({"action": "call", "tool": "summarize", "args": {},
                    "rationale": "再整理"}),
        json.dumps({"action": "finish", "rationale": "齐了"}),
    ]
    orc = _orc(steps, {"search": lambda s: calls.append(("search", s)) or "晴 25°C",
                       "summarize": lambda s: calls.append(("summarize", s)) or "总结完毕"})
    result = orc.handle_message("c1", "北京今天天气怎么样，给我一句话")
    assert result.status == "answered"
    assert [c[0] for c in calls] == ["search", "summarize"]
    record = orc.store.get(f"task:{result.task_id}")
    assert record["phase"] == "succeeded"
    assert any(r == "executor:finish" for r in record["receipts"])
    # 回执可回放：每步一条 tool 回执
    assert sum(1 for r in record["receipts"] if r.startswith("tool:")) == 2


def test_executor_unknown_tool_then_recover():
    steps = [
        json.dumps({"action": "call", "tool": "nonexistent", "args": {}}),
        json.dumps({"action": "finish", "rationale": "改主意了"}),
    ]
    orc = _orc(steps, {"search": lambda s: "ok"})
    result = orc.handle_message("c1", "做点什么")
    assert result.status == "answered"
    record = orc.store.get(f"task:{result.task_id}")
    assert any(r.startswith("executor:unknown-tool:") for r in record["receipts"])


def test_executor_repeated_invalid_proposals_fail_closed():
    orc = _orc(["这不是 JSON", '{"action":"hack"}', '{"action":"finish"}'],
               {"search": lambda s: "ok"})
    result = orc.handle_message("c1", "干活")
    assert result.status == "failed"
    record = orc.store.get(f"task:{result.task_id}")
    assert record["phase"] == "failed"
    assert sum(1 for r in record["receipts"]
               if r.startswith(("executor:invalid-proposal", "executor:unknown-tool"))) == 2


def test_executor_fail_action():
    orc = _orc([json.dumps({"action": "fail", "rationale": "没有合适工具"})],
               {"search": lambda s: "ok"})
    result = orc.handle_message("c1", "造火箭")
    assert result.status == "failed"
    assert any(r.startswith("executor:gave-up:") for r in result.receipts)


def test_executor_catalog_visible_in_prompt():
    model = ScriptExecutorModel([json.dumps({"action": "finish", "rationale": "x"})])
    from whale_core.core.tool_executor import ModelToolExecutor
    from whale_core.core.runtime import ToolRegistry, ToolSpec
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search", capability="web.search",
                            description="网页搜索", handler=lambda s: "ok"))
    ModelToolExecutor(model=None)  # 构造不触模型
    executor = ModelToolExecutor(model)
    executor.propose(goal="查天气", catalog=tools.specs(), results=[], receipts=[])
    assert "search" in model.prompts[0] and "网页搜索" in model.prompts[0]


def test_fixed_chain_stays_default_when_capabilities_declared():
    """声明了能力链的 plan 走固定链，不触发执行器（保底模式不回归）。"""
    intent_plan = json.dumps({"goal": "查", "kind": "task",
                              "capabilities": ["test.search"]})

    class FixedIntent:
        calls = 0

        def plan(self, text, *, context=""):
            FixedIntent.calls += 1
            return intent_plan

    store = InMemoryStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search", capability="test.search",
                            handler=lambda s: "找到了"))
    executor = ScriptExecutorModel([json.dumps({"action": "finish"})])
    orc = ConversationOrchestrator(
        store, WhaleCompanion(store, StubBrain()), intent_model=FixedIntent(),
        tools=tools, executor_model=executor)
    result = orc.handle_message("c1", "查一下")
    assert result.status == "answered"
    assert executor.steps  # 执行模型从未被消费（固定链已解决）
    assert any(r.startswith("tool:search:ok:") for r in result.receipts)


def test_executor_mode_off_without_model():
    """未配置 executor_model：无能力声明的任务诚实失败（旧行为保留）。"""
    store = InMemoryStore()
    orc = ConversationOrchestrator(
        store, WhaleCompanion(store, StubBrain()), intent_model=NullIntent(),
        tools=ToolRegistry())
    result = orc.handle_message("c1", "做点没有工具的事")
    assert result.status == "failed"
    assert any("capability-unavailable" in r for r in result.receipts)
