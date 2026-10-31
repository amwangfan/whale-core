"""QqInboundWorkflow 可选编排器路径测试（意图模型接线，全离线）。"""

from __future__ import annotations

from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.core.companion import WhaleCompanion
from whale_core.life.channels import OwnerEvent
from whale_core.life.initiative import ProactivePrefs
from whale_core.life.qq_workflow import QqInboundWorkflow
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore, MemoryKind


class FakeBrain:
    def __init__(self):
        self.calls = []

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append(text)
        return "好嘞"


class ScriptedIntent:
    def __init__(self, plan_json):
        self.plan_json = plan_json

    def plan(self, text, *, context=""):
        return self.plan_json


def _event(text, mid="m1"):
    return OwnerEvent(owner_id="owner", channel="qq", conversation="private:c1",
                      message_id=text and text != "" and "m" + str(abs(hash(text))) or "m1",
                      text=text)


def _prefs():
    from whale_core.life.initiative import ProactivePrefs
    return ProactivePrefs(enabled=True,
                          allowed_channels=frozenset({"direct_message"}),
                          passive_daily_limit=500)


def test_workflow_without_orchestrator_legacy_path():
    store, brain = InMemoryStore(), FakeBrain()
    wf = QqInboundWorkflow(store, brain, prefs=_prefs())
    result = wf.handle_owner_event(_event("你好"))
    assert result["reply"] == "好嘞"
    assert len(brain.calls) == 1


def test_workflow_with_orchestrator_routes_task():
    store, brain = InMemoryStore(), FakeBrain()
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search_web", capability="web.search",
                            handler=lambda a: "晴天（https://x.com）"))
    orch = ConversationOrchestrator(
        store, companion, tools=tools,
        intent_model=ScriptedIntent(
            '{"goal":"查天气","kind":"task","capabilities":["web.search"]}'))
    wf = QqInboundWorkflow(store, brain, prefs=_prefs(), orchestrator=orch)
    result = wf.handle_owner_event(_event("查天气"))
    assert result["reply"] == "好嘞"
    # 工具结果被编排器注入陪伴模型的输入
    assert "不可信工具输出" in brain.calls[-1]
    assert "晴天" in brain.calls[-1]
    # 草稿照常进账本（生产语义不变）
    assert result["draft_id"].startswith("qq-reply:")


def test_memory_command_intercepts_before_orchestrator():
    store, brain = InMemoryStore(), FakeBrain()
    orch = ConversationOrchestrator(
        store, WhaleCompanion(store, brain, curator=MemoryCurator(store)),
        intent_model=ScriptedIntent('{"goal":"x","kind":"chat"}'))
    wf = QqInboundWorkflow(store, brain, prefs=_prefs(), orchestrator=orch)
    result = wf.handle_owner_event(_event("记住：我怕辣"))
    assert result["reply"].startswith("好，我记住啦")
    assert brain.calls == []  # 指令路径不调模型
    assert len(store.list_by_kind(MemoryKind.PREFERENCE)) == 1
