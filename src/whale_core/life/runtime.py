"""Composition root for previewable companion behavior. No default side effects."""
from __future__ import annotations
from datetime import datetime
from whale_core.life.discovery import DiscoveryPlanner, DiscoveredContent
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs, OutboundPort, DispatchVerdict
from whale_core.life.state import InnerStateStore
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import MemoryStore


class WhaleLifeRuntime:
    def __init__(self, store: MemoryStore, *, allowed_discovery_hosts: frozenset[str]):
        self.memory = MemoryCurator(store)
        self.state = InnerStateStore(store)
        self.outbox = ProactiveOutbox(store)
        self.discovery = DiscoveryPlanner(allowed_hosts=allowed_discovery_hosts)

    def consider_discoveries(self, items: list[DiscoveredContent],
                             *, now: datetime) -> tuple[ProactiveDraft, ...]:
        drafts = self.discovery.drafts(items, self.state.load(), now=now)
        for draft in drafts:
            self.outbox.propose(draft)
        return drafts

    def propose_unfinished_topic(self, topic: str, *, evidence_id: str,
                                 now: datetime) -> ProactiveDraft:
        if topic not in self.state.load().unfinished_topics:
            raise ValueError("topic must already be an explicitly recorded unfinished topic")
        draft = ProactiveDraft(draft_id=f"unfinished:{evidence_id}", text=f"之前说到「{topic[:100]}」，有需要再接着聊吗？",
                               reason="未尽话题；不等于用户现在想聊天", evidence=(evidence_id,),
                               channel="hud", importance=0.4, created_at=now)
        self.outbox.propose(draft)
        return draft

    def deliver(self, draft_id: str, *, prefs: ProactivePrefs, now: datetime,
                port: OutboundPort, approved: bool = False, busy: bool = False) -> DispatchVerdict:
        return self.outbox.dispatch(draft_id, prefs, now=now,
                                    port=port, busy=busy, explicitly_approved=approved)
