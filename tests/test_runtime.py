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


def test_clarify_plan_voiced_through_persona():
    """澄清回复必须经陪伴人格产出（不直发意图模型原文——owner 报告的 AI 味 bug）。"""
    store, brain = InMemoryStore(), FakeBrain()
    orch = _orch(store, brain, intent_model=ScriptedIntentModel(
        _plan_text(kind="clarify", clarification_question="你想查哪个城市的天气？")))
    result = orch.handle_message("c", "帮我查天气")
    assert result.status == "clarify"
    assert result.reply == "好的呀"  # 回复来自陪伴模型（替身）
    assert brain.calls, "澄清必须调陪伴模型"
    sent = brain.calls[-1]["text"]
    assert "你想查哪个城市的天气？" in sent   # 澄清要点作为数据注入
    assert "人格语气" in sent                 # 带人格化指令
    assert "系统提示" in sent
    # 澄清轮写入历史，后续对话有上下文
    orch2 = _orch(store, brain)  # 无意图模型走纯对话
    orch2.handle_message("c", "上海")
    history = brain.calls[-1]["history"]
    assert "帮我查天气" in history[0]["content"]


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
    assert result.reply == "好的呀"  # 人格化回复来自陪伴模型
    assert "需要他批准" in brain.calls[-1]["text"]  # 系统提示如实说明待批


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


# ---------- 意图上下文与澄清克制（owner 反馈修复） ----------

def test_intent_model_receives_context_with_history_and_memory():
    """意图模型不再瞎问：上下文摘要含近期历史与相关记忆。"""
    store, brain = InMemoryStore(), FakeBrain()
    captured = {}

    class CapturingIntent:
        def plan(self, text, *, context=""):
            captured["context"] = context
            return '{"goal":"闲聊","kind":"chat"}'

    orch = _orch(store, brain, intent_model=CapturingIntent())
    orch.handle_message("c1", "我在成都")          # 写入历史
    orch.handle_message("c1", "明天有雨吗")        # 第二轮应带上轮上下文
    ctx = captured["context"]
    assert "我在成都" in ctx          # 近期历史进入意图上下文
    assert "用户：" in ctx            # 角色前缀

    # 记忆也进入
    orch2 = _orch(store, brain, intent_model=CapturingIntent())
    store_curator = MemoryCurator(store)
    store_curator.remember("用户在成都", source="t", evidence_id="e",
                           stable_fact=True, consent=True)
    orch2.handle_message("c1", "明天天气怎么样")
    assert "用户在成都" in captured["context"]


def test_clarify_prompt_guides_defaults():
    """路由器的 LLM 提示词包含「默认优先、澄清兜底」规矩。"""
    from whale_core.core.intent_router import SearchIntentRouter

    class CaptureModel:
        def __init__(self):
            self.prompts = []

        def reply(self, text, *, persona="", memories=(), history=()):
            self.prompts.append(text)
            return '{"goal":"x","kind":"chat"}'

    model = CaptureModel()
    router = SearchIntentRouter(model=model, enable_llm=True)
    router.plan("明天有雨吗", context="记忆：用户在成都")
    prompt = model.prompts[0]
    assert "默认用户所在地" in prompt
    assert "确实无法推断才" in prompt
    assert "用户在成都" in prompt  # 上下文进提示词


def test_async_slow_task_ack_then_complete():
    """两段式：慢能力任务 → 立即回 ack（status=ack）+ 后台执行完成回调。"""
    import threading
    from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
    from whale_core.core.intent_router import SearchIntentRouter
    from whale_core.memory.store import InMemoryStore

    class _FakeIntent:
        def plan(self, text, context=""):
            return ('{"goal":"画一张图","kind":"task","slots":{"prompt":"鲸鱼"},'
                    '"capabilities":["media.image.generate"]}')
    store = InMemoryStore()
    tools = ToolRegistry()
    done = threading.Event()
    tools.register(ToolSpec(
        name="generate_image", capability="media.image.generate",
        plugin_id="media_image",
        description="生图", handler=lambda args: '{"urls":["https://x/y.png"]}'))
    companion = WhaleCompanion(store, FakeBrain("好嘞，在画了"), curator=MemoryCurator(store))
    orc = ConversationOrchestrator(store, companion, intent_model=_FakeIntent(), tools=tools)
    got = []
    result = orc.handle_message_async("c1", "画个鲸鱼", on_complete=lambda r: (got.append(r), done.set()))
    assert result.status == "ack"
    assert result.reply  # 占位 ack 立即返回
    assert done.wait(timeout=10)  # 后台完成
    assert got and got[0].reply  # 补发结果有内容


def test_async_fast_path_stays_sync():
    """chat 类消息不受两段式影响：同步返回，on_complete 不触发。"""
    from whale_core.core.runtime import ConversationOrchestrator
    from whale_core.memory.store import InMemoryStore

    class _FakeIntent:
        def plan(self, text, context=""):
            return '{"goal":"聊天","kind":"chat"}'
    store = InMemoryStore()
    companion = WhaleCompanion(store, FakeBrain("普通回复"), curator=MemoryCurator(store))
    orc = ConversationOrchestrator(store, companion, intent_model=_FakeIntent())
    fired = []
    result = orc.handle_message_async("c1", "你好", on_complete=lambda r: fired.append(r))
    assert result.status == "answered"
    assert result.reply == "普通回复"
    assert not fired  # 快路径不触发回调


def test_failure_reply_uses_intent_judgment():
    """失败回复：意图模型判读回执为短结论，回复模型收到判读而非回执原文。"""
    from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
    from whale_core.memory.store import InMemoryStore
    from whale_core.core.companion import WhaleCompanion
    from whale_core.memory.curation import MemoryCurator

    class _JudgeModel:
        def reply(self, text, *, persona="", memories=(), history=()):
            return "生图服务超时"
    class _Intent:
        model = _JudgeModel()
        def plan(self, text, context=""):
            return ('{"goal":"画图","kind":"task","slots":{"prompt":"x"},'
                    '"capabilities":["media.image.generate"]}')
    store = InMemoryStore()
    tools = ToolRegistry()
    tools.register(ToolSpec(
        name="generate_image", capability="media.image.generate",
        plugin_id="media_image", description="生图",
        handler=lambda args: (_ for _ in ()).throw(TimeoutError())))
    brain = FakeBrain("超时啦，平台还在处理")
    orc = ConversationOrchestrator(
        store, WhaleCompanion(store, brain, curator=MemoryCurator(store)),
        intent_model=_Intent(), tools=tools)
    result = orc.handle_message("c1", "画个图")
    assert result.status == "failed"
    # 回复模型收到的是判读（「生图服务超时」），不是回执原文
    sent = brain.calls[-1]["text"]
    assert "生图服务超时" in sent
    assert "tool-fail" not in sent  # 回执原文不进回复模型上下文


def test_recover_interrupted_untouched_task_reruns():
    """重启恢复：未派发过工具的 running 任务安全重跑并回调。"""
    import threading
    store = InMemoryStore()
    tools = ToolRegistry()
    done = threading.Event()
    tools.register(ToolSpec(
        name="generate_image", capability="media.image.generate",
        plugin_id="media_image", description="生图",
        handler=lambda args: '{"ok":true}'))
    companion = WhaleCompanion(store, FakeBrain("好"), curator=MemoryCurator(store))
    orc = ConversationOrchestrator(store, companion, tools=tools)
    # 造一个「上次进程死掉」的在途任务：journal 存在、未派发、状态 running
    from whale_core.core.intent_plan import IntentPlan
    from dataclasses import asdict
    plan = IntentPlan(goal="画图", kind="task", slots={"prompt": "x"},
                      capabilities=("media.image.generate",), task_id="t-recover-1")
    store.put("runtime:task:t-recover-1", {
        "plan": asdict(plan), "transient_keys": [], "conversation_id": "c1",
        "source": "test", "text": "画图", "next_step": 0, "results": [],
        "receipts": [], "tool_name": ""})
    store.put("task:t-recover-1", {"task_id": "t-recover-1", "status": "running",
                                   "goal": "画图"})
    got = []
    recovered = orc.recover_interrupted_tasks(
        on_complete=lambda cid, reply, status: (got.append((cid, status)), done.set()))
    assert recovered == ["t-recover-1"]
    assert done.wait(timeout=10)
    assert got[0][0] == "c1"


def test_recover_dispatched_task_marks_unknown_not_rerun():
    """已派发过工具的任务不重跑（防重复扣费/副作用）——标 unknown 并告知。"""
    store = InMemoryStore()
    companion = WhaleCompanion(store, FakeBrain("上次被打断了"), curator=MemoryCurator(store))
    orc = ConversationOrchestrator(store, companion)
    from whale_core.core.intent_plan import IntentPlan
    from dataclasses import asdict
    plan = IntentPlan(goal="画图", kind="task", slots={"prompt": "x"},
                      capabilities=("media.image.generate",), task_id="t-recover-2")
    store.put("runtime:task:t-recover-2", {
        "plan": asdict(plan), "transient_keys": [], "conversation_id": "c1",
        "source": "test", "text": "画图", "next_step": 0, "results": [],
        "receipts": ["tool:generate_image:ok:..."], "tool_name": ""})
    store.put("task:t-recover-2", {"task_id": "t-recover-2", "status": "running",
                                   "goal": "画图"})
    got = []
    recovered = orc.recover_interrupted_tasks(
        on_complete=lambda cid, reply, status: got.append(status))
    assert recovered == ["t-recover-2"]
    # 标 unknown（非重跑），通知已回调
    assert store.get("task:t-recover-2")["status"] == "unknown"
    assert got == ["unknown"]


def test_attachment_images_read_local_file(tmp_path):
    """附件字节读取：本机缓存路径优先（QQ 同机），bytes 进 workflow。"""
    from whale_core.life.qq_workflow import QqInboundWorkflow
    from whale_core.life.channels import OwnerEvent
    from whale_core.life.initiative import ProactivePrefs
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")
    ev = OwnerEvent(owner_id="o", channel="qq", conversation="c1",
                    message_id="m1", text="参考这张图",
                    attachments=({"kind": "pic", "file_path": str(img), "url": ""},))
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"direct_message"}))
    wf = QqInboundWorkflow(InMemoryStore(), FakeBrain("ok"), prefs=prefs)
    imgs = wf._attachment_images(ev)
    assert imgs == (b"\x89PNG fake",)


def test_attachment_images_missing_file_skips():
    """路径不存在且无可下载 URL → 静默跳过（不报错）。"""
    from whale_core.life.qq_workflow import QqInboundWorkflow
    from whale_core.life.channels import OwnerEvent
    from whale_core.life.initiative import ProactivePrefs
    ev = OwnerEvent(owner_id="o", channel="qq", conversation="c1",
                    message_id="m2", text="x",
                    attachments=({"kind": "pic", "file_path": "/nonexistent/x.png", "url": ""},))
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"direct_message"}))
    wf = QqInboundWorkflow(InMemoryStore(), FakeBrain("ok"), prefs=prefs)
    assert wf._attachment_images(ev) == ()
