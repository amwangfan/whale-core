"""Read-only exploration planning from externally supplied evidence; no scraping or posting."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from whale_core.life.initiative import ProactiveDraft
from whale_core.life.state import InnerState


@dataclass(frozen=True)
class DiscoveredContent:
    item_id: str
    title: str
    url: str
    source_name: str
    summary: str = ""
    inspected: bool = False
    # inspected=True must be backed by actual source adapter evidence, never assumed.


class DiscoveryPlanner:
    def __init__(self, *, allowed_hosts: frozenset[str], max_candidates: int = 2):
        self.allowed_hosts = allowed_hosts
        self.max_candidates = max(0, max_candidates)

    def drafts(self, items: list[DiscoveredContent], state: InnerState,
               *, now: datetime | None = None) -> tuple[ProactiveDraft, ...]:
        at = now or datetime.now(timezone.utc)
        ranked: list[tuple[int, DiscoveredContent]] = []
        for item in items:
            parsed = urlparse(item.url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or host not in self.allowed_hosts:
                continue
            if not item.item_id or not item.title or not item.source_name:
                continue
            overlap = sum(1 for interest in state.interests if interest.lower() in (item.title+" "+item.summary).lower())
            # A match can become a suggestion, not evidence that the AI watched content.
            if overlap == 0:
                continue
            ranked.append((overlap, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].item_id))
        result = []
        for _, item in ranked[:self.max_candidates]:
            # Source title and summary are treated as untrusted data, not instructions.
            message = f"发现一个可能和你最近的兴趣有关的内容：《{item.title[:80]}》。要看看吗？"
            result.append(ProactiveDraft(
                draft_id=f"discovery:{item.item_id}", text=message,
                reason=f"兴趣匹配；来源：{item.source_name[:80]}；已核查内容：{item.inspected}",
                evidence=(item.url,), channel="hud", importance=0.45, created_at=at))
        return tuple(result)
