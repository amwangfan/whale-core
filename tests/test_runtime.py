"""core/runtime.py（ConversationOrchestrator）离线测试：全 fake，不触网。"""

from __future__ import annotations

import json

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import (
    ConversationOrchestrator,
    ToolRegistry,
    ToolSpec,
)
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore, MemoryKind


class FakeBrain:
    def __init__(self, reply_text: str = "好的呀"):
        self.calls: list[dict] = []
        self.reply_text = reply_text

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append({"text": text, "memories": list(memories),
                           "history": list(history), "persona": persona})
        return self.reply_text


class ScriptedIntentModel:
    """按脚本返回意图文本的替身（真实意图模型是 LLM，此处确定性）。"""

    def __init__(self, script):
        # script: callable(text) -> str 或 str
        self.script = script
        self.calls: list[str] = []

    def plan(self, text, *, context=""):
        self.calls.append(text)
        return self.script(text) if callable(self.script) else self.script


def _orch(store, brain, *, intent_model=None, tools=None, gateway=None):
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    return ConversationOrchestrator(
        store, companion, intent_model=intent_model, tools=tools, gateway=gateway
    )


def _plan_text(**kw):
    base = {"goal": "测试目标", "kind": "task", "capabilities": []}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# ---------- 纯对话与捷径 ----------

def test_plain_chat_without_intent_model():
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain)
    result = orch.handle_message("c1", "你好")
    assert result.status == "answered" and result.reply == "好的呀"
    assert result.plan is None


def test_memory_command_short_circuits_models():
    store, brain = InMemoryStore(), FakeBrain()
    intent = ScriptedIntentModel(_plan_text())
    orch = _orch(store, brain, intent_model=intent)
    result = orch.handle_message("c1", "记住：我不吃香菜")
    assert result.status == "answered"
    assert result.reply.startswith("好，我记住啦")
    assert result.receipts == ("memory-command",)
    assert brain.calls == [] and intent.calls == []
    assert len(store.list_by_kind(MemoryKind.PREFERENCE)) == 1


def test_empty_message():
    orch = _orch(InMemoryStore(), FakeBrain())
    assert orch.handle_message("c", "   ").reply == "（空消息）"


# ---------- 意图分流 ----------

def test_chat_plan_goes_plain_chat():
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(_plan_text(kind="chat")))
    result = orch.handle_message("c", "聊聊")
    assert result.status == "answered"
    assert result.plan is not None and result.plan.kind == "chat"


def test_clarify_plan_asks_and_records_history():
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(kind="clarify", clarification_question="你想查哪个城市的天气？")))
    result = orch.handle_message("c", "帮我查天气")
    assert result.status == "clarify"
    assert result.reply == "你想查哪个城市的天气？"
    assert brain.calls == []  # 澄清不调陪伴模型
    # 澄清轮写入历史，后续对话有上下文
    orch2 = _orch(store, brain)  # 无意图模型走纯对话
    orch2.handle_message("c", "上海")
    history = brain.calls[-1]["history"]
    assert history[0]["content"] == "帮我查天气"
    assert history[1]["content"] == "你想查哪个城市的天气？"


def test_intent_parse_failure_falls_back_to_chat():
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain, intent_model=ScriptedIntentModel("不是JSON"))
    result = orch.handle_message("c", "你好")
    assert result.status == "answered" and result.reply == "好的呀"
    assert result.receipts == ("intent:parse-failed→plain-chat",)


def test_intent_model_error_falls_back_to_chat():
    def boom(text):
        raise RuntimeError("模型挂了")
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(boom))
    result = orch.handle_message("c", "你好")
    assert result.status == "answered"
    assert result.receipts == ("intent:model-error→plain-chat",)


# ---------- 任务执行 ----------

def test_task_with_read_only_tool_executes_and_summarizes():
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    tools.register(ToolSpec(
        name="search_web", capability="web.search",
        handler=lambda args: "上海明天晴，26°C（来源：example.com）",
    ))
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["web.search"])), tools=tools)
    result = orch.handle_message("c", "查一下上海明天天气")
    assert result.status == "answered"
    assert result.receipts[0].startswith("tool:search_web:ok:")
    # 工具结果作为数据回流陪伴模型
    assert "不可信工具输出开始" in brain.calls[-1]["text"]
    assert "上海明天晴" in brain.calls[-1]["text"]


def test_unknown_capability_is_honest_failure():
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["media.video.generate"])))
    result = orch.handle_message("c", "给我生成视频")
    assert result.status == "failed"
    assert result.receipts == ("capability-unavailable:media.video.generate",)
    assert "工具当前不可用" in brain.calls[-1]["text"]


def test_tool_handler_exception_does_not_crash():
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    def bad(args):
        raise OSError("网络断了")
    tools.register(ToolSpec(name="search_web", capability="web.search", handler=bad))
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["web.search"])), tools=tools)
    result = orch.handle_message("c", "查天气")
    assert result.status == "failed"
    assert result.receipts[0] == "tool-fail:search_web:OSError"


def test_risky_tool_without_plugin_mapping_denied():
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="send_mail", capability="email.send",
                            risk_class="communicate", handler=lambda a: "sent"))
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["email.send"])), tools=tools)
    result = orch.handle_message("c", "帮我发邮件")
    assert result.status == "denied"
    assert result.receipts == ("tool-denied:send_mail:risk-without-plugin-mapping",)


def test_risky_tool_via_gateway_hold_becomes_pending_approval():
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="send_mail", capability="email.send",
                            risk_class="communicate",
                            plugin_id="email", action="send_email"))
    class HoldGateway:
        def dispatch(self, intent):
            assert intent.kind == "plugin_action"
            return "plugin-hold:email:send_email"
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["email.send"])), tools=tools, gateway=HoldGateway())
    result = orch.handle_message("c", "发邮件给老板")
    assert result.status == "pending_approval"
    assert "批准" in result.reply


def test_risky_tool_via_gateway_ok():
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="send_mail", capability="email.send",
                            risk_class="communicate",
                            plugin_id="email", action="send_email"))
    class OkGateway:
        def dispatch(self, intent):
            return "plugin:email:send_email:ok:已投递"
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["email.send"])), tools=tools, gateway=OkGateway())
    result = orch.handle_message("c", "发邮件")
    assert result.status == "answered"
    assert "plugin:email:send_email:ok" in result.receipts[0]


def test_model_self_approval_field_is_dropped_and_ignored():
    """模型输出 approved=true 不得产生任何授权效果（roadmap §6.1）。"""
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="pay", capability="shop.pay", risk_class="transaction",
                            plugin_id="shop", action="pay"))
    class DenyGateway:
        def dispatch(self, intent):
            return "plugin-denied:shop:pay:not-approved"
    plan = _plan_text(capabilities=["shop.pay"], approved=True, risk_hint="safe")
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(plan),
                 tools=tools, gateway=DenyGateway())
    result = orch.handle_message("c", "买下这个")
    assert result.plan is not None
    assert not hasattr(result.plan, "approved")  # 未定义字段被丢弃
    assert result.status == "denied"  # 自批无效，网关裁决为准


def test_tool_output_truncated():
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="fetch", capability="web.fetch",
                            handler=lambda a: "x" * 5000))
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["web.fetch"])), tools=tools)
    result = orch.handle_message("c", "读这个网页")
    assert result.status == "answered"
    assert len(result.receipts[0]) <= 1200 + len("tool:fetch:ok:")


def test_task_id_propagates():
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(task_id="task-42")))
    result = orch.handle_message("c", "做事")
    assert result.task_id == "task-42"


def test_duplicate_tool_registration_rejected():
    tools = ToolRegistry()
    tools.register(ToolSpec(name="t", capability="c"))
    try:
        tools.register(ToolSpec(name="t", capability="c2"))
    except ValueError:
        return
    raise AssertionError("duplicate tool registration must fail")


def test_invalid_tool_risk_rejected():
    try:
        ToolSpec(name="t", capability="c", risk_class="yolo")
    except ValueError:
        return
    raise AssertionError("invalid risk class must fail")


def test_task_turn_is_recorded_for_console():
    """任务轨迹落库（roadmap §5.2-6：区分模型提出与实际执行）。"""
    store, brain = InMemoryStore(), FakeBrain()
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search_web", capability="web.search",
                            handler=lambda a: "结果文本"))
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(capabilities=["web.search"], task_id="t-99")), tools=tools)
    result = orch.handle_message("c", "查一下")
    assert result.task_id == "t-99"
    record = store.get("task:t-99")
    assert record["goal"] == "测试目标"
    assert record["status"] == "answered"
    assert record["receipts"][0].startswith("tool:search_web:ok:")
    assert store.get("tasks:recent") == ["t-99"]
