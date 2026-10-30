"""User text stays distinct from model-only tool/status context."""
import json

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.memory.history import load_history
from whale_core.memory.store import InMemoryStore, MemoryKind
from whale_core.memory.sqlite_store import SqliteMemoryStore


class Recorder:
    def __init__(self): self.calls = []
    def reply(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return "收到"


class Planner:
    def __init__(self, **plan): self.plan_data = plan
    def plan(self, text, **kwargs): return json.dumps({"goal": text, **self.plan_data})


def test_memory_commands_survive_history_reload_without_model_calls(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = SqliteMemoryStore(path)
    brain = Recorder()
    runtime = ConversationOrchestrator(store, WhaleCompanion(store, brain))
    text = "记住：我喜欢从原理理解问题"
    result = runtime.handle_message("console", text)
    store.close()
    reopened = SqliteMemoryStore(path)
    try:
        assert load_history(reopened, "console") == [
            {"role": "user", "content": text},
            {"role": "assistant", "content": result.reply},
        ]
        assert brain.calls == []
    finally:
        reopened.close()


def test_tool_output_only_goes_to_current_model_input():
    store, brain, tools = InMemoryStore(), Recorder(), ToolRegistry()
    tools.register(ToolSpec(name="search", capability="web.search", handler=lambda args: "EXTERNAL_TOOL_PAYLOAD"))
    runtime = ConversationOrchestrator(store, WhaleCompanion(store, brain), tools=tools,
        intent_model=Planner(kind="task", capabilities=["web.search"]))
    runtime.handle_message("c", "搜索鲸鱼")
    assert "EXTERNAL_TOOL_PAYLOAD" in brain.calls[-1][0]
    assert load_history(store, "c")[0]["content"] == "搜索鲸鱼"
    episodes = store.list_by_kind(MemoryKind.EPISODE)
    assert "EXTERNAL_TOOL_PAYLOAD" not in str(episodes[0].content)


def test_system_clarification_does_not_become_user_history():
    store, brain = InMemoryStore(), Recorder()
    runtime = ConversationOrchestrator(store, WhaleCompanion(store, brain),
        intent_model=Planner(kind="clarify", clarification_question="INTERNAL_CLARIFICATION"))
    runtime.handle_message("c", "安排一下")
    assert "INTERNAL_CLARIFICATION" in brain.calls[-1][0]
    assert load_history(store, "c")[0]["content"] == "安排一下"
    assert "INTERNAL_CLARIFICATION" not in str(store.list_by_kind(MemoryKind.EPISODE)[0].content)


def test_recall_uses_user_query_without_augmented_tool_data():
    queries = []
    class Curator:
        def recall(self, query):
            queries.append(query)
            return []
    companion = WhaleCompanion(InMemoryStore(), Recorder(), curator=Curator())
    companion.chat("用户原话 + 系统提示 + 工具正文", conversation_id="c", user_text="用户原话")
    assert queries == ["用户原话"]
