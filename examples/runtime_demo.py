"""Runtime 离线演示：多轮对话 + 意图规划 + 工具闭环（不触网、不需要任何密钥）。

    python examples/runtime_demo.py

演示三条路径：纯聊天 / 澄清反问 / 任务（带工具回执的总结）。
模型与搜索工具都是确定性替身——输出标注 [DEMO]，不冒充真实模型生成。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore


class DemoBrain:
    """规则替身：不回真模型，只回可见的拼装文本（标注 DEMO）。"""

    def reply(self, text, *, persona="", memories=(), history=()):
        if "[工具执行结果" in text:
            block = text.split("[工具执行结果", 1)[1]
            return f"[DEMO] 查到了：{block.splitlines()[1].strip()}（已附来源）"
        if memories:
            return f"[DEMO] 嗯嗯，我记得的（召回 {len(memories)} 条）：{text}"
        return f"[DEMO] 收到：{text}"


class DemoIntentModel:
    """关键词规则替身：真实意图模型是 LLM，这里只做确定性演示。"""

    def plan(self, text, *, context=""):
        has_city = any(c in text for c in ("上海", "北京", "广州", "深圳", "杭州"))
        if "天气" in text and not has_city:
            return json.dumps({
                "goal": "查天气", "kind": "clarify",
                "clarification_question": "想查哪个城市的呀？",
            }, ensure_ascii=False)
        if "查" in text or "搜" in text or "天气" in text:
            return json.dumps({
                "goal": f"检索：{text[:30]}", "kind": "task",
                "capabilities": ["web.search"],
                "slots": {"query": text[:30]},
            }, ensure_ascii=False)
        return json.dumps({"goal": "闲聊", "kind": "chat"}, ensure_ascii=False)


def main() -> None:
    store = InMemoryStore()
    companion = WhaleCompanion(store, DemoBrain(), curator=MemoryCurator(store))

    tools = ToolRegistry()
    tools.register(ToolSpec(
        name="search_web", capability="web.search",
        description="[DEMO] 确定性假搜索",
        handler=lambda args: "上海明天晴，26°C（来源：https://example.com/weather）",
    ))

    orch = ConversationOrchestrator(store, companion,
                                    intent_model=DemoIntentModel(), tools=tools)

    for line in ("你好呀", "帮我查查天气", "上海明天天气怎么样", "记住：我不吃香菜"):
        result = orch.handle_message("demo-session", line)
        print(f"\n> {line}")
        print(f"  [{result.status}] {result.reply}")
        if result.receipts:
            print(f"  receipts: {list(result.receipts)}")

    print("\n[DEMO] 结束——全部离线替身，未触网。")


if __name__ == "__main__":
    main()
