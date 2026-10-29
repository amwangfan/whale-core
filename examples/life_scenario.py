"""Offline demonstration: a real sourced item -> interest -> draft -> approval -> fake receipt.

No network calls, no user accounts, no live QQ / Bilibili / phone / glasses.
"""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

from whale_core.life.discovery import DiscoveredContent
from whale_core.life.initiative import ProactivePrefs
from whale_core.life.runtime import WhaleLifeRuntime
from whale_core.memory.store import InMemoryStore


class DisplayOnlyPort:
    def send(self, draft):
        print("模拟渠道受理:", draft.channel, draft.text, "来源:", draft.evidence)
        return True


def main():
    store = InMemoryStore()
    runtime = WhaleLifeRuntime(store, allowed_discovery_hosts=frozenset({"www.bilibili.com"}))
    runtime.state.update(style="curious", add_interest="FPGA", open_topic="模拟电路错题")
    runtime.memory.remember("最近在学习 FPGA", source="user-confirmed",
                            evidence_id="chat:demo", stable_fact=True, consent=True)

    now = datetime(2026, 10, 28, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    item = DiscoveredContent(item_id="demo-1", title="FPGA 入门课程",
                             url="https://www.bilibili.com/video/DEMO",
                             source_name="B站（仅示例，不代表确实存在）",
                             inspected=False)
    [draft] = runtime.consider_discoveries([item], now=now)
    print("提议:", draft.text, "原因:", draft.reason)
    print("默认门控:", runtime.outbox.inspect(draft.draft_id, ProactivePrefs(), now=now).status)

    allowed = ProactivePrefs(enabled=True, allowed_channels=frozenset({"hud"}))
    print("已授权渠道、仍待审核:", runtime.outbox.inspect(draft.draft_id, allowed, now=now).status)
    print("单次批准、模拟投递:",
          runtime.deliver(draft.draft_id, prefs=allowed, now=now,
                          port=DisplayOnlyPort(), approved=True).status)
    print("再次投递:", runtime.deliver(draft.draft_id, prefs=allowed, now=now,
                                      port=DisplayOnlyPort(), approved=True).status)
    print("检索到的可信记忆:", [h.record.content["summary"] for h in runtime.memory.recall("FPGA", now=now)])


if __name__ == "__main__":
    main()
