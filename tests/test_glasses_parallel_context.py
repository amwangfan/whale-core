"""Request facts survive both runtime background threads without crossing turns."""
import json
import threading

from whale_core.api.glasses_context import REQUEST_FACTS, render_context
from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, InputEnvelope, ToolRegistry, ToolSpec
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.history import load_history
from whale_core.memory.store import InMemoryStore


def test_parallel_turns_keep_their_fact_snapshot_after_request_reset():
    store = InMemoryStore()
    calls = []
    intents = []
    gates = {"查询甲方安排": threading.Event(), "查询乙方安排": threading.Event()}
    done = {name: threading.Event() for name in gates}

    class Brain:
        def reply(self, text, **kwargs):
            calls.append((text, kwargs.get("persona", "")))
            return "收到"

    class Intent:
        def plan(self, text, **kwargs):
            intents.append((text, dict(REQUEST_FACTS.get() or {})))
            assert gates[text].wait(5)
            return json.dumps({"goal": text, "kind": "task", "capabilities": ["web.search"],
                               "slots": {"query": text}})

    companion = WhaleCompanion(store, Brain(), curator=MemoryCurator(store))
    companion.planning_hint = lambda: render_context(REQUEST_FACTS.get())
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search", capability="web.search", handler=lambda _: "查询完成"))
    orch = ConversationOrchestrator(store, companion, intent_model=Intent(), tools=tools)
    followups = {}
    for name, session, fact in (("查询甲方安排", "glasses:a", "甲方专属事实"),
                                ("查询乙方安排", "glasses:b", "乙方专属事实")):
        token = REQUEST_FACTS.set({"明日日程": fact})
        try:
            def settled(route, result, name=name):
                followups[name] = (route, result)
                done[name].set()
            orch.handle_envelope_parallel(
                InputEnvelope(conversation_id=session, text=name, source="glasses:phone"),
                on_settled=settled)
        finally:
            REQUEST_FACTS.reset(token)
    assert REQUEST_FACTS.get() is None
    for event in gates.values():
        event.set()
    assert all(event.wait(5) for event in done.values())
    assert all(route == "task" for route, _ in followups.values())
    assert dict(intents)["查询甲方安排"]["明日日程"] == "甲方专属事实"
    assert dict(intents)["查询乙方安排"]["明日日程"] == "乙方专属事实"
    for name, session, own, other in (("查询甲方安排", "glasses:a", "甲方专属事实", "乙方专属事实"),
                                     ("查询乙方安排", "glasses:b", "乙方专属事实", "甲方专属事实")):
        turns = [(text, persona) for text, persona in calls if name in text]
        assert len(turns) == 2  # first reply and tool followup
        assert all(own in persona and other not in persona for _, persona in turns)
        assert own not in json.dumps(load_history(store, session), ensure_ascii=False)
