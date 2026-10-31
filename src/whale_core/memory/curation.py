"""Opt-in curated memories with evidence-aware, explainable retrieval.

Raw call transcripts in MemoryStore are NOT automatically promoted to facts.
This pure-Python implementation has no embeddings, LLM calls or hidden collection.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from whale_core.memory.store import MemoryKind, MemoryRecord, MemoryStore


def _terms(text: str) -> set[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text.lower())
    parts: set[str] = set()
    for tok in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", tok):
            # 中文词项 = 单字 + 二元组：单字查询（如「辣」）也要能命中
            # 多字事实（「我怕辣」）；召回靠交集，精确度交给打分排序。
            parts.update(tok)
            parts.update(tok[i:i + 2] for i in range(max(0, len(tok) - 1)))
        else:
            parts.add(tok)
    return parts


@dataclass(frozen=True)
class RecallHit:
    record: MemoryRecord
    score: float
    reasons: tuple[str, ...]


class MemoryCurator:
    """Explicit candidate promotion and retrieval; never consumes audio or raw logs."""

    def __init__(self, store: MemoryStore, half_life_days: float = 30.0):
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        self.store = store
        self.half_life_days = half_life_days

    def remember(self, summary: str, *, source: str, evidence_id: str,
                 importance: float = 0.5, confidence: float = 1.0,
                 stable_fact: bool = False, consent: bool = False,
                 key: str | None = None, now: datetime | None = None) -> MemoryRecord:
        if not consent:
            raise PermissionError("Explicit memory consent required")
        if not summary.strip() or not source.strip() or not evidence_id.strip():
            raise ValueError("summary, source and evidence_id are required")
        if not 0 <= importance <= 1 or not 0 <= confidence <= 1:
            raise ValueError("importance and confidence must be within [0,1]")
        kind = MemoryKind.PREFERENCE if stable_fact else MemoryKind.EPISODE
        record = MemoryRecord(
            kind=kind,
            key=key or f"curated:{uuid4().hex}",
            content={"summary": summary[:1500], "source": source[:120],
                     "evidence_id": evidence_id[:160], "importance": importance,
                     "confidence": confidence, "stable_fact": stable_fact},
            created_at=now or datetime.now(timezone.utc),
            tags=("curated", "stable" if stable_fact else "episode"),
        )
        self.store.put_record(record)
        return record

    def recall(self, query: str, *, limit: int = 5,
               now: datetime | None = None) -> tuple[RecallHit, ...]:
        if limit <= 0:
            return ()
        current = now or datetime.now(timezone.utc)
        query_terms = _terms(query)
        matches: list[RecallHit] = []
        records = (self.store.list_by_kind(MemoryKind.EPISODE) +
                   self.store.list_by_kind(MemoryKind.PREFERENCE))
        for rec in records:
            if "curated" not in rec.tags:
                continue
            content = rec.content
            stable = bool(content.get("stable_fact"))
            terms = _terms(str(content.get("summary", "")))
            common = terms & query_terms
            if query_terms and not common:
                continue
            if not query_terms and not stable:
                continue
            relevance = len(common) / max(1, len(query_terms))
            age = max(0.0, (current - rec.created_at).total_seconds() / 86400)
            freshness = 1.0 if stable else math.pow(0.5, age / self.half_life_days)
            importance = float(content.get("importance", 0.5))
            confidence = float(content.get("confidence", 1.0))
            score = confidence * (0.65 * relevance + 0.25 * importance + 0.10 * freshness)
            reasons = ("词项匹配" if common else "稳定事实",
                       "长期事实不衰减" if stable else f"距今{age:.0f}天",
                       f"来源:{content.get('source', 'unknown')}",
                       f"证据:{content.get('evidence_id', 'unknown')}")
            matches.append(RecallHit(rec, score, reasons))
        matches.sort(key=lambda hit: (hit.score, hit.record.created_at), reverse=True)
        return tuple(matches[:limit])

    def forget(self, key: str) -> bool:
        record = self.store.get_record(key)
        if record is None or "curated" not in record.tags:
            return False
        self.store.delete(key)
        return True
