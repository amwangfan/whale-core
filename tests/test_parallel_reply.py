"""2026-11-06 并行链路：DS 第一条不等意图；意图后台判定，需要工具才跟进第二条。"""
from __future__ import annotations

import json
import threading
import time

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, InputEnvelope, ToolRegistry, ToolSpec
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.history import load_history
from whale_core.memory.store import InMemoryStore


class _Brain:
    def __init__(self):
        self.calls: list[str] = []

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append(text)
        if "不可信工具输出开始" in text:
            return "找到了，明天小雨，记得带伞"
        return "我还不知道唉，我去看看" if "天气" in text else "晚上好呀"


class _SlowIntent:
    def __init__(self, plan: str, delay: float = 0.0, gate: threading.Event | None = None):
        self.plan_text, self.delay, self.gate = plan, delay, gate
        self.calls = 0

    def plan(self, text, *, context=""):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        return self.plan_text


def _task(cap="weather.now"):
    return json.dumps({"goal": "查天气", "kind": "task", "capabilities": [cap],
                       "slots": {"query": "郫都"}}, ensure_ascii=False)


def _chat():
    return json.dumps({"goal": "对话", "kind": "chat"}, ensure_ascii=False)


def _orch(intent, tools=None, gateway=None):
    store, brain = InMemoryStore(), _Brain()
    comp = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    orch = ConversationOrchestrator(store, comp, intent_model=intent, tools=tools,
                                    gateway=gateway)
    return orch, store, brain


def _run(orch, text, **kw):
    done = threading.Event()
    box: dict = {}

    def settled(route, out):
        box["route"], box["out"] = route, out
        done.set()

    follow: list = []
    first = orch.handle_envelope_parallel(
        InputEnvelope(conversation_id="private:1", text=text, source="qq:owner",
                      channel="qq"),
        on_followup=follow.append, on_settled=settled, **kw)
    return first, follow, box, done


def _weather_tools(calls):
    tools = ToolRegistry()

    def handler(args):
        calls.append(args)
        return "郫都 明天 小雨 12-16°C（和风）"
    tools.register(ToolSpec(name="weather_now", capability="weather.now", handler=handler))
    return tools


def test_first_reply_does_not_wait_for_slow_intent():
    gate = threading.Event()
    orch, _, _ = _orch(_SlowIntent(_chat(), gate=gate))
    t0 = time.perf_counter()
    first, follow, box, done = _run(orch, "晚上好")
    elapsed = time.perf_counter() - t0
    assert first.reply == "晚上好呀"
    assert elapsed < 1.0, elapsed  # 意图被闸住也不影响第一条
    gate.set()
    assert done.wait(5)
    assert box["route"] == "chat" and follow == []


def test_chat_intent_produces_no_followup_and_history_single_turn():
    orch, store, brain = _orch(_SlowIntent(_chat(), delay=0.05))
    first, follow, box, done = _run(orch, "晚上好")
    assert done.wait(5)
    assert follow == [] and box["out"] is None
    assert len(brain.calls) == 1  # 不传信息给回复模型
    hist = load_history(store, "private:1")
    assert [m["content"] for m in hist] == ["晚上好", "晚上好呀"]


def test_task_intent_runs_tool_and_followup_merges_history():
    calls: list = []
    orch, store, brain = _orch(_SlowIntent(_task(), delay=0.05),
                               tools=_weather_tools(calls))
    first, follow, box, done = _run(orch, "天气怎么样")
    assert first.reply == "我还不知道唉，我去看看"
    assert done.wait(5)
    assert box["route"] == "task" and len(follow) == 1
    assert calls, "工具应已执行"
    assert follow[0].reply == "找到了，明天小雨，记得带伞"
    prompt = brain.calls[-1]
    assert "小雨 12-16" in prompt and "我还不知道唉，我去看看" in prompt  # 第一条 + 结果 + 备注
    assert "别重复第一句" in prompt
    hist = load_history(store, "private:1")
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[1]["content"] == "我还不知道唉，我去看看\n找到了，明天小雨，记得带伞"
    assert follow[0].intent_ms >= 40


def test_acl_denied_task_does_not_execute_tool():
    calls: list = []
    tools = ToolRegistry()
    tools.register(ToolSpec(name="run_shell", capability="shell.safe",
                            risk_class="communicate", plugin_id="shell_sandbox",
                            action="run_command", handler=lambda a: calls.append(a) or "x"))

    class DenyGateway:
        def dispatch(self, intent):
            return "plugin-denied:shell_sandbox:run_command:not-owner"

    orch, _, _ = _orch(_SlowIntent(_task("shell.safe")), tools=tools, gateway=DenyGateway())
    first, follow, box, done = _run(orch, "执行一下 ls")
    assert done.wait(5)
    assert calls == []
    assert box["out"].status == "denied"


def test_empty_or_broken_intent_is_safe():
    for raw in ("", "not json"):
        orch, _, brain = _orch(_SlowIntent(raw))
        first, follow, box, done = _run(orch, "帮我看看")
        assert done.wait(5)
        assert follow == [] and box["route"] == "chat"
        assert len(brain.calls) == 1


def test_intent_timeout_means_no_followup():
    gate = threading.Event()
    orch, _, _ = _orch(_SlowIntent(_task(), gate=gate), tools=_weather_tools([]))
    orch.PARALLEL_INTENT_TIMEOUT_S = 0.1
    first, follow, box, done = _run(orch, "天气怎么样")
    assert done.wait(5)
    gate.set()
    assert box["route"] == "chat" and follow == []


def test_first_note_injected_as_system_data_not_history():
    orch, store, brain = _orch(_SlowIntent(_chat()))
    first, follow, box, done = _run(orch, "晚上好", first_note="实时信息稍后补上")
    assert done.wait(5)
    assert "实时信息稍后补上" in brain.calls[0]
    assert load_history(store, "private:1")[0]["content"] == "晚上好"


def test_memory_command_still_sync_and_skips_intent():
    intent = _SlowIntent(_task())
    orch, _, brain = _orch(intent)
    first, follow, box, done = _run(orch, "记住：我不吃香菜")
    assert first.reply.startswith("好，我记住啦")
    assert intent.calls == 0 and box["route"] == "shortcut"
