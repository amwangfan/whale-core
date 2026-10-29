"""P0 端到端主线验收（roadmap §13.5 / §8 P0 关键验收）：

    多轮对话 → 记忆召回 → 搜索工具 → 带来源的总结

全离线：意图模型与搜索工具为替身；断言覆盖链路每个环节的证据。
"""

from __future__ import annotations

import json

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore, MemoryKind


class StoryBrain:
    """记录每轮收到的 persona/memories/history，并按内容给出可断言的回复。"""

    def __init__(self):
        self.calls: list[dict] = []

    def reply(self, text, *, persona="", memories=(), history=()):
        self.calls.append({"text": text, "persona": persona,
                           "memories": list(memories), "history": list(history)})
        if "[工具执行结果" in text:
            # 模拟「带来源的总结」：从工具结果块里抽来源行
            src_line = [l for l in text.splitlines() if "（http" in l]
            return f"查好了：{src_line[0] if src_line else '无来源'}"
        return "嗯嗯"


class RuleIntentModel:
    """规则意图替身：含「查/搜」→ task(web.search)；否则 chat。"""

    def plan(self, text, *, context=""):
        if "查" in text or "搜" in text:
            return json.dumps({"goal": f"检索 {text[:20]}", "kind": "task",
                               "capabilities": ["web.search"]}, ensure_ascii=False)
        return json.dumps({"goal": "闲聊", "kind": "chat"}, ensure_ascii=False)


def _fake_search(args):
    return "上海明天晴 26°C — 数据来源：中国天气网（https://weather.example.com/shanghai）"


def test_p0_end_to_end_chain():
    store = InMemoryStore()
    brain = StoryBrain()
    companion = WhaleCompanion(store, brain, curator=MemoryCurator(store))
    tools = ToolRegistry()
    tools.register(ToolSpec(name="search_web", capability="web.search",
                            handler=_fake_search))
    orch = ConversationOrchestrator(store, companion,
                                    intent_model=RuleIntentModel(), tools=tools)

    # 1) 多轮对话：第二轮能看到第一轮
    orch.handle_message("s1", "我叫小凡")
    orch.handle_message("s1", "我叫什么来着")
    history = brain.calls[-1]["history"]
    assert any(m["content"] == "我叫小凡" for m in history), "多轮历史缺失"

    # 2) 显式记忆 + 召回：记住偏好 → 后续对话召回进 prompt
    orch.handle_message("s1", "记住：我对香菜过敏")
    orch.handle_message("s1", "晚上吃香菜面怎么样")
    memories = brain.calls[-1]["memories"]
    assert any("香菜过敏" in m for m in memories), "curated 记忆未召回"

    # 3) 搜索工具 → 带来源的总结
    result = orch.handle_message("s1", "帮我查一下上海明天的天气")
    assert result.status == "answered"
    assert result.receipts and result.receipts[0].startswith("tool:search_web:ok:")
    assert "weather.example.com" in result.reply, "回复未带来源链接"

    # 4) 任务轨迹可审计（控制台数据源）
    record = store.get(f"task:{result.task_id}")
    assert record and record["status"] == "answered"
    assert record["capabilities"] == ["web.search"]

    # 5) 情节回写持续进行
    assert len(store.list_by_kind(MemoryKind.EPISODE)) >= 4
